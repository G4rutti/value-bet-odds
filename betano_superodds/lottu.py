"""Scraper da Lottu.

Etapa 0 — Reconhecimento (resultado)
────────────────────────────────────
Nunca tinha sido levantada antes (não está em `LEVANTAMENTO-CASAS.md`,
achada só porque apareceu no histórico de apostas do usuário). Plataforma
**ngbras** (Angular first-party + API própria em `alpha-sb.ngbras.com`).

A primeira pista — o banner "Acelerador de Odds" da home — era falsa: é
`bonus_type: "ODDS_BOOST"` num programa de fidelidade
(`/promotion/available-sportsbook`), um multiplicador automático por
tamanho de múltipla, sem odd original/turbinada fixa por seleção. Não cabe
no schema `Offer` (ver `ADAPTER_CONTRACT.md` §1) e foi descartada.

A oferta de verdade mora numa seção separada do menu, "Odds Turbinadas"
(`/s/CLE?group_type=GROUP&identifier=oddsturbinadas`), achada só pelo
usuário passando a URL. A extração de rede via browser (extensão) não
conseguiu flagrar a chamada nem uma vez nessa página nesta sessão — o
endpoint só foi achado testando variações de rota vizinhas a
`/event/live-summary` (que essa página TAMBÉM chama, mas só para a sidebar
"Partidas Populares", sem boost nenhum), usando 404 ("Cannot GET ...", rota
inexistente) vs outro erro pra distinguir rota real de chute errado — e
descartando o `/widget/{identifier}` que devolveu `401` (existe, exige auth):

    GET https://alpha-sb.ngbras.com/event/highlights

Pública — sem cookie, sem login — mas **exige o header `Origin:
https://www.lottu.bet.br`**; sem ele, `400
{"code":3004,"message":"Missing parameters","info":{"parameter":"origin"}}`
(tem que ser header HTTP, mandar como query param não resolve).

Devolve TODOS os "highlights" ativos num array plano, sem paginação nem
filtro por evento (32 na sondagem de 2026-08-20) — cada item já é um combo
pronto com odd original (`old_value`) e turbinada (`value`) prontas, sem
precisar de heurística pra decidir qual é qual:

    {
      "_id": "6a85eb9e1d02191929637786",
      "date": "2026-08-20T22:29:50.000Z",
      "championship": "Odds Turbinadas - Brasileiro Série B",
      "question": "Athletic x CRB",
      "group": "oddsturbinadas",
      "odds": {"answers": [{
        "answer_list": ["Athletic Para Ganhar Um Dos Tempos",
                         "Athletic Para Ter o Maior Número de Escanteios"],
        "value": 4.75, "old_value": 3.97
      }]}
    }

`group` distingue dois tipos vistos na sondagem — 28 `"oddsturbinadas"` e 4
`"CasadinhasTurbinadas"` (same-game combo, mesmo formato de odds) — tratados
aqui como a mesma família de oferta; nenhum outro valor de `group` foi
observado. `question` já vem no formato `"Time A x Time B"`, um dos
separadores que `matcher.split_times` reconhece nativamente — não precisa
de normalização, diferente da Altenar (`"A vs. B"`).
"""

from __future__ import annotations

import logging
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import Offer
from .scraper import ScraperError
from .value.market_parser import SEPARADOR_PERNAS

log = logging.getLogger(__name__)

CASA = "Lottu"
API_URL = "https://alpha-sb.ngbras.com/event/highlights"
HUB_URL = "https://www.lottu.bet.br/s/CLE?group_type=GROUP&identifier=oddsturbinadas&name=Odds%20Turbinadas"

_HEADERS = {
    "accept": "application/json",
    "origin": "https://www.lottu.bet.br",
    "referer": "https://www.lottu.bet.br/",
}


class LottuScraper:
    """Busca os combos da seção "Odds Turbinadas" da Lottu."""

    def __init__(self, session: "curl_requests.AsyncSession | None" = None) -> None:
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "LottuScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers=_HEADERS,
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self) -> Any:
        assert self._session is not None, "use o scraper como context manager"
        try:
            r = await self._session.get(API_URL)
        except RequestException as exc:
            raise ScraperError(f"falha de rede em {CASA}: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"{CASA} respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"{CASA} não devolveu JSON: {exc}") from exc

    async def scrape(self) -> list[Offer]:
        highlights = await self._get_json()

        ofertas: list[Offer] = []
        for item in highlights or []:
            ofertas.extend(self._parse_highlight(item))

        log.info("%s: %d oferta(s)", CASA, len(ofertas))
        return ofertas

    @staticmethod
    def _parse_highlight(item: dict[str, Any]) -> list[Offer]:
        evento_id = item.get("_id")
        pergunta = item.get("question")
        if not evento_id or not pergunta:
            # Sem id ou sem os times não dá pra montar `offer_id` estável nem
            # casar contra a Pinnacle — mesma regra do resto dos adapters.
            return []

        liga = item.get("championship")
        data = item.get("date")
        url = HUB_URL

        ofertas: list[Offer] = []
        for resposta in (item.get("odds") or {}).get("answers") or []:
            oferta = LottuScraper._parse_resposta(
                resposta, evento_id=evento_id, evento=pergunta, liga=liga,
                data=data, url=url,
            )
            if oferta is not None:
                ofertas.append(oferta)
        return ofertas

    @staticmethod
    def _parse_resposta(
        resposta: dict[str, Any], *, evento_id: str, evento: str,
        liga: str | None, data: str | None, url: str,
    ) -> Offer | None:
        original, boosted = resposta.get("old_value"), resposta.get("value")
        try:
            odd_original, odd_boost = float(original), float(boosted)
        except (TypeError, ValueError):
            return None
        # Invariante do contrato: `odd_boost` tem que ser estritamente maior
        # que a original — nunca confiar cegamente nos rótulos dos campos.
        if odd_boost <= odd_original:
            return None

        # `answer_list` já vem com as pernas separadas pela própria API — a
        # string `answer` é só a junção delas com "&", não usar como fonte
        # (mesma armadilha documentada em ADAPTER_CONTRACT.md §2.3 pra
        # SportingTech: separador da casa != SEPARADOR_PERNAS do projeto).
        pernas = resposta.get("answer_list") or (
            [resposta["answer"]] if resposta.get("answer") else []
        )
        if not pernas:
            return None

        return Offer(
            casa=CASA,
            fonte="lottu_highlight",
            evento_id=str(evento_id),
            evento=evento,
            mercado=SEPARADOR_PERNAS.join(pernas),
            odd_original=odd_original,
            odd_boost=odd_boost,
            url=url,
            liga=liga,
            # Só uma data no payload (kickoff) — replicada nos dois campos,
            # mesma limitação já documentada pra novibet/casadeaposta/
            # sportingtech em ADAPTER_CONTRACT.md §2.4.
            valido_ate=data,
            inicio_evento=data,
            boost_pct=round((odd_boost / odd_original - 1) * 100, 1),
        )


async def scrape_offers() -> list[Offer]:
    async with LottuScraper() as scraper:
        return await scraper.scrape()
