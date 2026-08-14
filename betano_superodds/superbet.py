"""Superbet — fonte de REFERÊNCIA para o consenso, não fonte de oferta.

Por que esta casa, e por que só como referência
───────────────────────────────────────────────
O consenso mede independência por FAMÍLIA DE FEED (`consenso._familia_da_casa`),
não por casa. Hoje o pool `mercados_casa` é escrito por duas famílias apenas:
as 11 casas Altenar (que são um feed só — ~90% das seleções com preço idêntico)
e a Betano. Uma 12ª casa Altenar somaria zero. A Superbet roda plataforma
própria, então entra como TERCEIRA família — que é o que torna
`CONSENSO_MIN_FAMILIAS=2` finalmente ligável (ver a nota em `value/config.py`
sobre a tentativa revertida em 2026-08-13).

Ela está em `CASAS-PENDENTES.md` desde 2026-08-04 como "plataforma mapeada,
rota da oferta turbinada não achada". Isso continua verdade e **não bloqueia
este módulo**: aqui não se quer o boost dela, se quer o livro de mercados —
e esse responde.

    GET /v2/pt-BR/struct                     esportes, categorias, torneios
    GET /v2/pt-BR/events/by-date             listagem (3.4k eventos)
    GET /v2/pt-BR/events/{eventId}           evento inteiro, ~5.4k seleções

Pública: `200 application/json` sem cookie, sem login, sem header não-óbvio —
só `impersonate="chrome"` do `curl_cffi`, igual às outras fontes. Confere com o
critério de entrada do projeto: API pública sem login.

⚠️ O que esta fonte NÃO resolve — prop de jogador
─────────────────────────────────────────────────
A Superbet publica prop de jogador em volume (`Jogador - Chutes no Gol`,
`Jogador - Marcar Gol`, `Jogador - Receber Cartão`), inclusive artilheiro puro,
que o feed Altenar não tem em grafia nenhuma. Só que **todo mercado
`Jogador - *` vem com UM LADO SÓ** — só o preço do "sim"/"mais de", nunca o
outro lado (medido: 715 grupos de `Jogador - Chutes no Gol` num evento, todos
com uma seleção). Sem os dois lados não há como medir a margem, e
`fair_odds.remover_vig` recusa — corretamente, é a mesma guarda que já tira do
consenso a casa com perna suspensa.

Então prop de JOGADOR continua sem referência aqui. O que a Superbet entrega,
de-vigável e com os dois lados, é o nível de JOGO e de EQUIPE:

    Total de Cartões          Total de Chutes no Gol      Total de Escanteios
    Total de Cartões Vermelhos   Total de Defesas do Goleiro
    Cada Equipe Mais de X Cartões    <Time> - Total de Escanteios

`cartões` é a maior família do balde sem cobertura (132 pernas medidas), então
o ganho é real — só não é o que uma leitura otimista do catálogo prometia.

⚠️ Futebol VIRTUAL com nome de time real
────────────────────────────────────────
`sportId` 75 é "E-Sport Futebol" e 190 é "Futebol Virtual" — juntos, 563 dos
3.450 eventos da listagem. Os nomes são de times REAIS: "Granada (V)·Valencia
(V)", "Sporting Lisboa (JKey)·Vitória Guimarães (JKey)". Normalizados viram
`granada v` / `lisboa jkey`, que o matcher casaria com o jogo de verdade —
injetando preço de partida simulada como referência de partida real.

Por isso o filtro é ALLOWLIST (`ESPORTES_REAIS`), nunca denylist: esporte novo
que a Superbet inventar nasce fora, e não dentro.
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import MercadoCasa, Offer, minutos_ate_inicio, to_utc_iso
from .scraper import ScraperError
from .value import config as vconfig
from .value.matcher import _parse_data, encontrar_evento, split_times
from .value.models import Matchup

log = logging.getLogger(__name__)

CASA = "Superbet"
API = "https://production-superbet-offer-br.freetls.fastly.net/v2/pt-BR"
SITE = "https://superbet.bet.br"

# ALLOWLIST — ver o aviso sobre futebol virtual no cabeçalho. Só os três
# esportes que a Pinnacle carrega do nosso lado (`PinnacleScraper.
# jogos_normalizados`: soccer, tennis, basketball): sondar um esporte que não
# temos como precificar seria pagar request por dado que ninguém lê.
ESPORTES_REAIS: dict[int, str] = {5: "Futebol", 2: "Tênis", 4: "Basquete"}

# A Superbet separa os dois times com "·" (U+00B7), não com " vs. ". O matcher
# espera " - ", igual ao resto do projeto.
SEPARADOR_TIMES = "·"


class SuperbetScraper:
    """Livro de mercados da Superbet para o pool de consenso.

    Não devolve `Offer`: `scrape()` retorna sempre lista vazia, de propósito.
    A rota da oferta turbinada da Superbet nunca foi achada (`CASAS-PENDENTES.md`),
    e inventar uma oferta a partir de `matchTags=price_boost` sem saber qual
    seleção foi turbinada produziria oferta fantasma. O produto deste módulo é
    `mercados_vistos`.
    """

    def __init__(self, session: "curl_requests.AsyncSession | None" = None,
                 alvos: list[dict] | None = None) -> None:
        # Mesmo padrão de `esportiva.EsportivaScraper`: acumulado no objeto
        # porque o retorno de `coletar` é `list[Offer]` em todos os scrapers, e
        # mudar essa assinatura obrigaria a mexer em todos.
        self.mercados_vistos: list[MercadoCasa] = []
        # Eventos que a avaliação precisa precificar (ver `main.alvos_da_fila`).
        # Aqui eles não dividem espaço com caça a boost — este scraper não
        # procura boost —, então o orçamento INTEIRO vai pra fila, e só o que
        # sobra é gasto por kickoff mais próximo.
        self.alvos = list(alvos or [])
        self._session = session
        self._owns = session is None
        self._torneios: dict[str, str] = {}

    async def __aenter__(self) -> "SuperbetScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers={"accept": "application/json",
                         "origin": SITE, "referer": SITE + "/"},
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get(self, path: str, **params: Any) -> dict:
        assert self._session is not None, "use o scraper como context manager"
        # Jitter pelo mesmo motivo das outras fontes: volume constante entrega
        # bot, e perder esta fonte custaria a única terceira família de feed.
        await asyncio.sleep(random.uniform(*config.REQUEST_JITTER))
        try:
            r = await self._session.get(f"{API}{path}", params=params or None)
        except RequestException as exc:
            raise ScraperError(f"falha de rede em {path}: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"{path} respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"{path} não devolveu JSON: {exc}") from exc

    # ------------------------------------------------------------------

    async def scrape(self) -> list[Offer]:
        """Enche `mercados_vistos`. Devolve `[]` — não é fonte de oferta."""
        try:
            eventos = await self._listagem()
        except ScraperError as exc:
            log.warning("%s: listagem falhou: %s", CASA, exc)
            return []

        if not eventos:
            # Zero evento não é um estado normal desta fonte (3.4k na
            # listagem): logar alto, porque zero silencioso é como bug de
            # cobertura se esconde.
            log.warning("%s: listagem veio sem evento nenhum nos esportes "
                        "reais — investigar antes de assumir que é normal", CASA)
            return []

        alvo_ids = self._eventos_da_fila(eventos)
        resto = [e["eventId"] for e in self._por_kickoff(eventos)
                 if e["eventId"] not in set(alvo_ids)]
        ordem = alvo_ids + resto

        n_ok = 0
        for ev_id in ordem[: config.SUPERBET_MAX_DETALHES]:
            try:
                await self._do_detalhe(ev_id, eventos)
                n_ok += 1
            except ScraperError as exc:
                log.debug("%s: detalhe do evento %s falhou: %s", CASA, ev_id, exc)
                continue

        log.info("%s: %d mercado(s) de %d evento(s) (%d pela fila de avaliação)",
                 CASA, len(self.mercados_vistos), n_ok, len(alvo_ids))
        return []

    async def _listagem(self) -> list[dict]:
        """Eventos prematch dos esportes REAIS, com identidade já resolvida."""
        if not self._torneios:
            await self._carregar_struct()

        agora = datetime.now(timezone.utc).astimezone()
        dados = await self._get(
            "/events/by-date",
            currentStatus="active", offerState="prematch",
            startDate=agora.strftime("%Y-%m-%d %H:%M:%S"),
        )
        saida: list[dict] = []
        for ev in dados.get("data") or []:
            if ev.get("sportId") not in ESPORTES_REAIS:
                continue   # ver ESPORTES_REAIS: virtual tem nome de time real
            ident = self._identidade_evento(ev)
            if ident["evento"] is None or ident["inicio_evento"] is None:
                continue
            saida.append({"eventId": ev.get("eventId"), **ident})
        return [e for e in saida if e["eventId"] is not None]

    async def _carregar_struct(self) -> None:
        """`tournamentId -> nome` — é o que preenche `liga` no pool.

        Não é enfeite: sem `liga` do lado do pool, `matcher.genero_conflita`
        não enxerga o marcador feminino e a ponte de evento recusa o jogo (ver
        `test_genero_sem_liga_no_pool_recusa`). Falha fechada, mas é cobertura
        perdida de graça.
        """
        try:
            st = (await self._get("/struct")).get("data") or {}
        except ScraperError as exc:
            log.warning("%s: /struct falhou (%s) — o pool vai sem `liga`, e "
                        "jogo feminino não vai casar", CASA, exc)
            self._torneios = {"": ""}   # marca "tentado", não retenta no ciclo
            return
        self._torneios = {
            str(t.get("id")): (t.get("localNames") or {}).get("pt-BR") or ""
            for t in st.get("tournaments") or []
        }

    def _identidade_evento(self, ev: dict) -> dict:
        """Nome, kickoff (ISO UTC) e liga — a identidade que casa o evento
        contra o resto do pool e contra a oferta.

        Um só lugar monta isto, pelo mesmo motivo do
        `esportiva._identidade_evento`: duas normalizações diferentes do mesmo
        nome não dão erro, só casam errado.
        """
        bruto = ev.get("matchName") or ""
        evento = bruto.replace(SEPARADOR_TIMES, " - ") if bruto else None
        if evento and split_times(evento) is None:
            # Nome que não separa em dois times não serve pra casar com nada —
            # e entraria no pool como linha morta.
            evento = None
        return {
            "evento": evento,
            "inicio_evento": to_utc_iso(ev.get("utcDate")
                                        or ev.get("unixDateMillis")),
            "liga": self._torneios.get(str(ev.get("tournamentId"))) or None,
        }

    @staticmethod
    def _por_kickoff(eventos: list[dict]) -> list[dict]:
        """Mesma política de `esportiva._por_kickoff`: jogo já iniciado e jogo
        além do horizonte não valem request."""
        horizonte = config.ALTENAR_HORIZONTE_HORAS * 60
        com_data: list[tuple[float, dict]] = []
        for ev in eventos:
            faltam = minutos_ate_inicio({"inicio_evento": ev["inicio_evento"]})
            if faltam is not None and 0 < faltam <= horizonte:
                com_data.append((faltam, ev))
        return [ev for _, ev in sorted(com_data, key=lambda p: p[0])]

    def _eventos_da_fila(self, eventos: list[dict]) -> list[int]:
        """Ids que correspondem aos alvos da fila de avaliação.

        Mesmo casador e mesmas guardas de `esportiva._eventos_da_fila` — e o
        `_por_kickoff` no fim pelo mesmo motivo: a tolerância do matcher mede a
        concordância entre as duas datas, não a distância até agora.
        """
        if not self.alvos:
            return []

        candidatos: list[Matchup] = []
        por_id: dict[int, dict] = {}
        for ev in eventos:
            times = split_times(ev["evento"])
            inicio = _parse_data(ev["inicio_evento"])
            if times is None or inicio is None:
                continue
            candidatos.append(Matchup(id=ev["eventId"], league=ev["liga"] or "",
                                      home_team=times[0], away_team=times[1],
                                      commence_time=inicio))
            por_id[ev["eventId"]] = ev
        if not candidatos:
            return []

        achados: list[dict] = []
        for alvo in self.alvos:
            m = encontrar_evento(alvo.get("evento") or "",
                                 alvo.get("inicio_evento"), candidatos,
                                 min_score=vconfig.CONSENSO_MATCH_MIN_SCORE,
                                 max_horas=vconfig.CONSENSO_MATCH_MAX_HORAS)
            if m is not None and por_id.get(m.matchup.id) not in achados:
                achados.append(por_id[m.matchup.id])
        return [ev["eventId"] for ev in self._por_kickoff(achados)]

    async def _do_detalhe(self, ev_id: int, eventos: list[dict]) -> None:
        dados = await self._get(f"/events/{ev_id}")
        d = dados.get("data")
        if isinstance(d, list):
            d = d[0] if d else None
        if not isinstance(d, dict):
            raise ScraperError(f"evento {ev_id} sem bloco `data`")
        ident = next((e for e in eventos if e["eventId"] == ev_id), None)
        self.mercados_vistos += self._mercados_do_detalhe(d, ev_id, ident)

    def _mercados_do_detalhe(self, d: dict, ev_id: int,
                             ident: dict | None) -> list[MercadoCasa]:
        """Um `MercadoCasa` por seleção, agrupado por instância de mercado.

        O payload é ACHATADO: `odds` é uma lista única de seleções, e o que
        diz quais pertencem ao mesmo mercado é o `marketUuid` — não o
        `marketId`, que é o TIPO do mercado e se repete entre linhas (as 7
        linhas de "Total de Gols" compartilham `marketId`, cada uma com seu
        `marketUuid`). Agrupar por `marketId` juntaria over 0.5 com over 3.5
        num mercado só e `remover_vig` recusaria tudo, silenciosamente.

        Mercado com menos de dois lados é descartado — sem os dois não há
        margem a remover. É o que corta todo o catálogo `Jogador - *` desta
        casa (ver o aviso no cabeçalho do módulo).
        """
        ident = ident or {"evento": None, "inicio_evento": None, "liga": None}
        grupos: dict[str, list[dict]] = {}
        for o in d.get("odds") or []:
            uuid = o.get("marketUuid")
            nome = o.get("marketName")
            selecao = o.get("name")
            preco = o.get("price")
            if not uuid or not nome or not selecao:
                continue
            if o.get("status") != "active":
                # Seleção suspensa tem preço velho; deixá-la no grupo faria a
                # margem parecer outra coisa.
                continue
            try:
                preco = float(preco)
            except (TypeError, ValueError):
                continue
            if preco <= 1.0:
                continue
            grupos.setdefault(str(uuid), []).append(
                {"nome": str(nome), "selecao": str(selecao), "preco": preco})

        saida: list[MercadoCasa] = []
        for uuid, selecoes in grupos.items():
            if len(selecoes) < 2:
                continue
            for s in selecoes:
                saida.append(MercadoCasa(
                    casa=CASA,
                    evento_id=str(ev_id),
                    # `marketUuid` como `market_id`: é ele que identifica a
                    # INSTÂNCIA, e `consenso._mercados_por_casa` usa
                    # `(casa, market_id)` como unidade de de-vig.
                    market_id=uuid,
                    market_nome=s["nome"],
                    selecao=s["selecao"],
                    preco=s["preco"],
                    evento=ident["evento"],
                    inicio_evento=ident["inicio_evento"],
                    liga=ident["liga"],
                ))
        return saida


async def scrape_mercados(alvos: list[dict] | None = None) -> list[MercadoCasa]:
    """Atalho pra uso avulso (script, teste manual)."""
    async with SuperbetScraper(alvos=alvos) as sc:
        await sc.scrape()
        return sc.mercados_vistos
