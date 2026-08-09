"""Scraper da Novibet.

Etapa 0 — Reconhecimento (resultado)
────────────────────────────────────
App Angular (`products.novi.web`), tudo lazy-loaded — o bundle inicial não
tem nenhuma referência à API de odds, então não dava pra achar por grep de
bundle (tentado em 2026-08-05, sem sucesso). O caminho que resolveu foi
captura de XHR em browser real navegando até o hub "Odds Turbinadas"
(`/apostas-esportivas/popular/6685003/...`), feita pelo usuário — ver
`CASAS-PENDENTES.md` seção 0.

O levantamento original (2026-08-04) já tinha achado a rota
`GET /spt/feed/marketviews/location/v2/<id1>/<id2>/` respondendo `200` sem
cookie, mas `/0/0/` dava `500 {"code":5000,"message":"Object reference not
set to an instance of an object."}` — e essa mesma mensagem genérica voltava
pra **qualquer** par de IDs testado às cegas em 2026-08-05, inclusive pares
plausíveis extraídos da URL do hub. A causa não eram os IDs: faltavam os
headers de contexto `x-gw-*` (`x-gw-country-sysname`, `x-gw-currency-sysname`,
`x-gw-language-sysname` etc.) que o app sempre manda e que o backend .NET
aparentemente precisa pra nem começar a processar a rota — sem eles, quebra
com `NullReferenceException` genérica antes de olhar pros IDs.

Com os headers certos, dois endpoints resolvem o fluxo completo, nenhum dos
dois pede cookie:

1. `GET /spt/feed/navigation/coupon/v2/4324/6685003/[<sportIds separados por vírgula>]`
   — árvore de navegação do hub de turbinadas: esportes → competições, cada
   nó com seu próprio `marketViewGroupId`. `4324` é um id fixo da árvore de
   navegação do site (o mesmo prefixo aparece em
   `/spt/feed/navigation/menu/4324`); `6685003` é o `marketViewGroupId` do
   próprio hub "Odds Turbinadas" (`marketViewGroupSysname: "SUPER_OOST"` na
   resposta — confirma que é o local certo). **Pegadinha:** sem os ids dos
   esportes no final da URL, a resposta lista os esportes só com
   `subItems: []` — as competições só aparecem quando os ids são ecoados de
   volta. Por isso o scraper faz 2 chamadas: uma sem sufixo pra descobrir
   quais esportes estão no hub agora, outra repassando esses ids pra pegar a
   árvore completa com as competições.

2. `GET /spt/feed/marketviews/location/v2/4324/<marketViewGroupId-da-competição>/`
   — as ofertas de verdade, em `prebuiltTickets`. Cada ticket já vem com
   `price` (odd original) e `boostedPrice` (odd turbinada) prontos, igual ao
   padrão do `casadeaposta.py` — não precisa de heurística pra decidir o que
   é boost, é o próprio par de campos.

Headers `x-gw-*` fixos (não mudam por competição) mais os query params
`lang`/`timeZ`/`oddsR`/`usrGrp` que o app sempre manda. As respostas ficam em
cache no Cloudflare por 5 min (`cache-control: public, max-age=300,
immutable`) — o browser contorna isso anexando `&timestamp=<epoch>`; o
scraper faz o mesmo, senão o ciclo de scraping (bem mais curto que 5 min)
ficaria vendo dado velho.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import Offer
from .scraper import ScraperError

log = logging.getLogger(__name__)

CASA = "Novibet"
BASE_URL = "https://www.novibet.bet.br"
ROOT_MENU_ID = 4324
TURBINADAS_LOCATION_ID = 6685003
HUB_URL = f"{BASE_URL}/apostas-esportivas/popular/{TURBINADAS_LOCATION_ID}/"

_GW_HEADERS = {
    "accept": "application/json, text/plain, */*",
    "accept-language": "pt-BR,pt;q=0.9",
    "referer": f"{HUB_URL}competitions?ids=",
    "x-gw-application-name": "NoviBR",
    "x-gw-channel": "WebPC",
    "x-gw-client-layout": "Desktop",
    "x-gw-client-timezone": "America/Sao_Paulo",
    "x-gw-cms-key": "_BR",
    "x-gw-country-sysname": "BR",
    "x-gw-currency-sysname": "BRL",
    "x-gw-domain-key": "_BR",
    "x-gw-language-sysname": "pt-BR",
    "x-gw-odds-representation": "Decimal",
}
_BASE_PARAMS = {
    "lang": "pt-BR",
    "timeZ": "E. South America Standard Time",
    "oddsR": "1",
    "usrGrp": "BR",
}


def _bust_cache(params: dict[str, str]) -> dict[str, str]:
    return {**params, "timestamp": str(int(time.time() * 1000))}


# Emoji decorativo que a própria Novibet injeta no rótulo de mercado pra
# marcar qual perna está turbinada (ex. "Resultado Final 🚀"), confirmado
# byte a byte no payload real em 2026-08-05 — não é mojibake local. Sem
# removê-lo, o rótulo mais comum da casa (1X2) quebra
# `value.market_parser._parse_resultado` (regex `resultado\s+final\s*:`, que
# não prevê nada entre "Final" e ":"), derrubando a perna mais simples e mais
# frequente do feed inteiro em "mercado não reconhecido".
_EMOJI_RE = re.compile("[\U0001F300-\U0001FAFF☀-➿]+")


def _limpar_rotulo(texto: str | None) -> str:
    return _EMOJI_RE.sub("", texto or "").strip()


def _ofertas_da_selecao(selecao: dict[str, Any]) -> list[dict[str, Any]]:
    """Tickets "BetBuilder" trazem as pernas em `offers` (lista); tickets
    "Single" (ex. "Múltiplo resultado correto") trazem em `offer` (dict
    único). Sem este fallback, um ticket "Single" vira `pernas` vazio e é
    descartado em silêncio — confirmado ao vivo em 2026-08-05: dos 6
    `prebuiltTickets` reais de uma competição, o único "Single" sumiu das
    ofertas do scrape por essa causa; os 5 "BetBuilder" saíram normalmente.
    """
    offers = selecao.get("offers")
    if offers:
        return offers
    oferta_unica = selecao.get("offer")
    return [oferta_unica] if oferta_unica else []


def _leaf_market_view_ids(items: list[dict[str, Any]]) -> list[int]:
    """Percorre a árvore de navegação e junta os ids-folha (sem `subItems`).

    Normalmente são 2 níveis (esporte → competição), mas percorre
    recursivamente pra não quebrar se algum dia vier mais fundo.
    """
    leaves: list[int] = []
    for item in items:
        sub_items = item.get("subItems") or []
        if sub_items:
            leaves.extend(_leaf_market_view_ids(sub_items))
        else:
            mvg_id = item.get("marketViewGroupId")
            if mvg_id is not None:
                leaves.append(mvg_id)
    return leaves


class NovibetScraper:
    """Busca as ofertas do hub "Odds Turbinadas" da Novibet."""

    def __init__(self, session: "curl_requests.AsyncSession | None" = None) -> None:
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "NovibetScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers=_GW_HEADERS,
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str) -> Any:
        assert self._session is not None, "use o scraper como context manager"
        try:
            r = await self._session.get(url, params=_bust_cache(_BASE_PARAMS))
        except RequestException as exc:
            raise ScraperError(f"falha de rede em {url}: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"{url} respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"{url} não devolveu JSON: {exc}") from exc

    async def scrape(self) -> list[Offer]:
        coupon_url = f"{BASE_URL}/spt/feed/navigation/coupon/v2/{ROOT_MENU_ID}/{TURBINADAS_LOCATION_ID}"

        # A árvore só vem com `subItems` (competições) preenchido quando os
        # ids dos esportes são ecoados de volta na URL — sem eles, a resposta
        # lista os esportes mas com `subItems: []`. Por isso são 2 chamadas:
        # a primeira descobre quais esportes estão no hub agora, a segunda
        # pede a árvore completa passando esses ids.
        esportes = await self._get_json(f"{coupon_url}/")
        sport_ids = [
            it["marketViewGroupId"]
            for it in (esportes.get("items") or [])
            if it.get("marketViewGroupId")
        ]
        if not sport_ids:
            log.info("%s: 0 oferta(s) (hub sem esportes no momento)", CASA)
            return []

        ids_param = ",".join(str(i) for i in sport_ids)
        arvore = await self._get_json(f"{coupon_url}/{ids_param}")
        leaf_ids = _leaf_market_view_ids(arvore.get("items") or [])

        grupos_por_id = await asyncio.gather(
            *(
                self._get_json(
                    f"{BASE_URL}/spt/feed/marketviews/location/v2/{ROOT_MENU_ID}/{leaf_id}/"
                )
                for leaf_id in leaf_ids
            ),
            return_exceptions=True,
        )

        ofertas: list[Offer] = []
        for leaf_id, grupos in zip(leaf_ids, grupos_por_id):
            if isinstance(grupos, BaseException):
                log.warning("%s: competição %s falhou: %s", CASA, leaf_id, grupos)
                continue
            for grupo in grupos or []:
                for ticket in grupo.get("prebuiltTickets") or []:
                    oferta = self._parse_ticket(ticket, liga_fallback=grupo.get("caption"))
                    if oferta is not None:
                        ofertas.append(oferta)

        log.info("%s: %d oferta(s)", CASA, len(ofertas))
        return ofertas

    @staticmethod
    def _parse_ticket(ticket: dict[str, Any], liga_fallback: str | None) -> Offer | None:
        if ticket.get("isSuspended"):
            return None

        price, boosted = ticket.get("price") or {}, ticket.get("boostedPrice") or {}
        odd_original, odd_boost = price.get("value"), boosted.get("value")
        if not odd_original or not odd_boost or odd_boost <= odd_original:
            return None

        header = ticket.get("header") or {}
        selecoes = ticket.get("selections") or []
        primeira_selecao = selecoes[0] if selecoes else {}

        evento_id = header.get("betContextId") or primeira_selecao.get("betContextId")
        if not evento_id:
            # Sem id de evento não dá pra montar `offer_id` estável — mesma
            # regra do `casadeaposta.py`.
            return None

        captions = header.get("additionalCaptions") or {}
        c1, c2 = captions.get("competitor1"), captions.get("competitor2")
        evento_primeiro_jogo = (
            primeira_selecao.get("betContextCaption")
            or (f"{c1} - {c2}" if c1 and c2 else None)
            or str(evento_id)
        )

        # Um `prebuiltTicket` pode ser um combo de jogos DIFERENTES (ex.:
        # promo "Festival de Gols" da Novibet: "Mais de 2,5" em 3 partidas
        # distintas). Cada `selecao` carrega o SEU `betContextId` e
        # `betContextCaption` — quando eles divergem entre si, o ticket é
        # multi-jogo. Detectar isso aqui é o que evita, rio abaixo, casar as
        # 3 pernas contra o mesmo jogo da Pinnacle e multiplicar a mesma
        # probabilidade 3x (edge fantasma).
        jogos_distintos = {
            s.get("betContextId"): s.get("betContextCaption")
            for s in selecoes if s.get("betContextId")
        }
        multi_jogo = len(jogos_distintos) > 1

        pernas = []
        for selecao in selecoes:
            jogo_da_perna = selecao.get("betContextCaption")
            prefixo = f"[{jogo_da_perna}] " if multi_jogo and jogo_da_perna else ""
            for oferta in _ofertas_da_selecao(selecao):
                mercado_nome = oferta.get("marketInstanceCaption")
                selecao_nome = oferta.get("betInstanceCaption")
                if not mercado_nome or not selecao_nome:
                    continue
                pernas.append(f"{prefixo}{_limpar_rotulo(mercado_nome)}: {selecao_nome}")
        if not pernas:
            return None

        if multi_jogo:
            # Rótulo agregado honesto: não deixar o nome do primeiro jogo
            # representar os outros. `evento_id` continua sendo o do ticket
            # inteiro (`offer_id` precisa de identidade estável) — o jogo de
            # cada perna vai junto no texto da perna, não aqui.
            evento = " | ".join(
                cap for cap in jogos_distintos.values() if cap
            ) or evento_primeiro_jogo
        else:
            evento = evento_primeiro_jogo

        liga = None
        for path_item in (header.get("fullLocationPath") or {}).get("locationPathItems") or []:
            if path_item.get("level") == "Competition":
                liga = path_item.get("caption")
                break
        liga = liga or liga_fallback

        return Offer(
            casa=CASA,
            fonte="novibet_prebuilt_ticket",
            evento_id=str(evento_id),
            evento=evento,
            mercado=" + ".join(pernas),
            odd_original=float(odd_original),
            odd_boost=float(odd_boost),
            url=HUB_URL,
            liga=liga,
            valido_ate=header.get("expireTimeUTC"),
            # Continua sendo o único horário que o payload expõe — o
            # `prebuiltTicket` não carrega kickoff por seleção, só
            # `header.expireTimeUTC` (fim da promoção/ticket). Num multi-jogo
            # ele já é conservador o bastante: se a promoção ainda vale, os
            # jogos dela ainda não começaram todos.
            inicio_evento=header.get("expireTimeUTC"),
            boost_pct=ticket.get("boostPercentage"),
        )


async def scrape_offers() -> list[Offer]:
    async with NovibetScraper() as scraper:
        return await scraper.scrape()
