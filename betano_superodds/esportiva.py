"""Scraper da plataforma **Altenar** — serve várias casas com o mesmo código.

Etapa 0 — Reconhecimento (resultado)
────────────────────────────────────
A Esportiva Bet roda o sportsbook da **Altenar**, cuja API de widget é pública:
sem login, sem token, sem quota. A integração dela se chama `esportiva`.

O levantamento de 2026-08-03 (ver `CASAS-MAPEADAS.md`) mostrou que a API é
**multi-tenant**: o mesmo endpoint serve 10 das casas monitoradas, e quem
seleciona a marca é o parâmetro `integration`. Por isso este módulo recebe uma
`config.CasaAltenar` e o resto do pipeline não muda — casa nova é uma linha em
`config.CASAS_ALTENAR`, não um parser novo.

    GET /api/widget/GetEvents        listagem por esporte (~2,3 MB, 889 eventos)
    GET /api/widget/GetEventDetails  detalhe de um evento, com `boosts`

A resposta é **relacional**, não aninhada: vêm listas separadas de `events`,
`markets`, `odds`, `competitors` e `champs`, ligadas por id. Por isso o parse
monta índices antes de qualquer coisa.

As duas vias de oferta turbinada — espelham a Betano
─────────────────────────────────────────────────────
**1. Mercado `1x2 - Odds Aumentadas`** (equivalente ao `MR12`)

Aparece na listagem junto dos mercados normais. Só traz o preço turbinado; a
odd original viria do detalhe do evento, que custa 1 request por jogo.

**2. Array `boosts` no detalhe do evento** (equivalente ao `smart-picks`)

Combos de bet-builder turbinados, e aqui os DOIS preços vêm juntos:

    {"id": 15216009, "eventId": 16630676,
     "odds": [{"marketId": …, "selectionId": …}, …],   # as pernas
     "price": 1.3,                                      # odd ORIGINAL
     "boostInfo": {"price": 1.5, "endDate": …,
                   "isLimitedTime": false, "isBetOfTheDay": false,
                   "isWelcome": false, "betsLimit": 3330}}

⚠️ **`isWelcome` separa dois produtos diferentes no mesmo array.** `false` é o
boost recorrente, com ganho na casa de 5-20% (4.75→6, 1.625→1.75). `true` é
promoção de captação de conta nova, com ganho absurdo e teto baixo — medidos em
2026-08-04: 1.71→50.00 (`betsLimit` 50), 1.077→22.00, 1.95→19.00. Todo ganho
acima de +500% no banco era `isWelcome`, e nenhum boost normal chegou perto.

Elas são descartadas aqui. Não servem pra quem já tem conta (é uma vez por CPF)
e, pior, a fila de avaliação ordena por `boost_pct`: deixá-las entrar faz a
promoção inútil furar a fila na frente da oferta real.

⚠️ **`betsLimit` vem no payload** — ao contrário do que este arquivo afirmava
até 2026-08-04. O `limite_aposta` do `Offer` é gravado quando aparece, e hoje
71 de 859 ofertas ativas têm teto. Nem toda oferta traz o campo.

⚠️ `price` é a original e `boostInfo.price` a turbinada — o boost sobe a odd,
então a maior das duas é a turbinada. O código confere isso em vez de confiar
na convenção, porque inverter as duas faria todo edge sair invertido.
"""

from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import MercadoCasa, Offer, millis_to_iso, minutos_ate_inicio, to_utc_iso
from .scraper import ScraperError

log = logging.getLogger(__name__)

API = "https://sb2frontend-altenar2.biahosted.com/api/widget"

# Casa padrão, para quem instancia o scraper sem escolher — mantém o
# comportamento de quando a Altenar era uma casa só.
CASA_PADRAO = config.CASAS_ALTENAR[0]
CASA = CASA_PADRAO.nome

# Parâmetros fixos da plataforma — descobertos no bundle do site. O
# `integration` NÃO entra aqui: é o único campo que varia por casa.
PARAMS_BASE = {
    "culture": "pt-BR",
    "countryCode": "BR",
    "deviceType": 1,
    "numFormat": "en-GB",
    "timezoneOffset": -180,
    "langId": 1,
}

# sportId da Altenar (≠ dos ids da Betano).
ESPORTES = {66: "Futebol", 67: "Basquete", 68: "Tênis"}

# Como cada casa batiza o mercado turbinado de resultado final. Não é uma só:
# a Esportiva usa "1x2 - Odds Aumentadas", a BateuBet "Vencedor do encontro -
# Odds Aumentadas" e a EstrelaBet/vupi "Vencedor do encontro - Super Odds".
# Casar só "aumentad" fazia as duas últimas devolverem zero oferta.
MARCAS_TURBINADO = ("aumentad", "super odd", "superodd", "turbinad", "boost")


def _e_turbinado(nome: str | None) -> bool:
    if not nome:
        return False
    n = nome.lower()
    return any(marca in n for marca in MARCAS_TURBINADO)


class EsportivaScraper:
    """Busca as ofertas turbinadas de UMA casa na plataforma Altenar."""

    def __init__(self, casa: config.CasaAltenar | None = None,
                 session: "curl_requests.AsyncSession | None" = None,
                 offset: int = 0) -> None:
        self.casa = casa or CASA_PADRAO
        # De onde começar a sondar a listagem nesta visita. Quem administra o
        # avanço é o `main`, que persiste um cursor por casa.
        self.offset = max(0, int(offset))
        # Eventos cujo DETALHE foi pedido nesta visita. O diff precisa disso:
        # combo que não foi sondado não pode ser tratado como expirado.
        self.eventos_sondados: set[str] = set()
        # Mercados COMPLETOS vistos nesta visita, para a referência de consenso.
        # Acumulados aqui, e não devolvidos por `coletar`, porque o retorno de
        # `coletar` é `list[Offer]` em todos os scrapers e mudar essa assinatura
        # obrigaria a mexer em todos — mesma solução de `eventos_sondados`.
        self.mercados_vistos: list[MercadoCasa] = []
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "EsportivaScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers={"accept": "application/json",
                         "origin": self.casa.site, "referer": self.casa.site + "/"},
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
        # Jitter pelo mesmo motivo da Betano: volume constante entrega bot.
        await asyncio.sleep(random.uniform(*config.REQUEST_JITTER))
        try:
            r = await self._session.get(
                f"{API}{path}",
                params={**PARAMS_BASE, "integration": self.casa.slug, **params},
            )
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
        """Todas as ofertas turbinadas, de todos os esportes vigiados."""
        ofertas: list[Offer] = []
        for sport_id, nome in ESPORTES.items():
            try:
                ofertas += await self._scrape_esporte(sport_id)
            except ScraperError as exc:
                # Um esporte que quebra não derruba os outros — mesma política
                # das fontes da Betano.
                log.warning("%s/%s falhou: %s", self.casa.slug, nome, exc)
        log.info("%s: %d oferta(s) total", self.casa.slug, len(ofertas))
        return ofertas

    async def _scrape_esporte(self, sport_id: int) -> list[Offer]:
        dados = await self._get("/GetEvents", sportId=sport_id, champIds="", period=0)

        eventos = {e["id"]: e for e in dados.get("events") or []}
        mercados = {m["id"]: m for m in dados.get("markets") or []}
        odds = {o["id"]: o for o in dados.get("odds") or []}
        times = {c["id"]: c.get("name", "") for c in dados.get("competitors") or []}
        ligas = {c["id"]: c.get("name", "") for c in dados.get("champs") or []}

        ofertas: list[Offer] = []
        com_boost: list[int] = []

        for ev in eventos.values():
            turbinados = [mercados[mid] for mid in (ev.get("marketIds") or [])
                          if mid in mercados and _e_turbinado(mercados[mid].get("name"))]
            if turbinados:
                com_boost.append(ev["id"])
            for m in turbinados:
                ofertas += self._do_mercado(ev, m, odds, times, ligas)

        # O detalhe traz os combos (`boosts`) e a odd original — 1 request por
        # evento. O teto é por casa: com o rodízio de `CASAS_POR_CICLO`, o custo
        # do ciclo é `ALTENAR_MAX_DETALHES × casas do ciclo`, não × casas ligadas.
        #
        # Não dá pra olhar só quem tem mercado turbinado na listagem: em várias
        # casas (4Play, BetGorillas) o boost existe SÓ no array `boosts` do
        # detalhe, sem nenhum mercado turbinado aparecendo na listagem. Filtrar
        # por `com_boost` fazia essas casas devolverem zero oferta sempre.
        #
        # Então: quem tem mercado turbinado vem primeiro (é pista concreta), e o
        # resto do orçamento sonda os demais — mas girando o ponto de partida a
        # cada visita, porque o boost nem sempre está no topo da listagem (a
        # VaiDeBet só tem a partir do índice 20). Ver ALTENAR_JANELA_SONDA.
        #
        # Os dois grupos passam por `_por_kickoff` antes: a ordem em que a API
        # devolve não tem relação nenhuma com o horário do jogo, e o orçamento
        # é pequeno (12 por casa, e a casa só volta a cada ~12 min). Gastar num
        # jogo de daqui a três dias — ou num que já começou — era o que fazia a
        # oferta desta tarde ser descoberta em cima da hora.
        vistos = set(com_boost)
        outros = [e["id"] for e in eventos.values() if e["id"] not in vistos]
        n_mercados_turb = len(com_boost)   # antes do corte: é o que a casa exibe

        com_boost = self._por_kickoff(com_boost, eventos)
        janela = self._por_kickoff(outros, eventos)[: config.ALTENAR_JANELA_SONDA]
        if janela:
            ini = self.offset % len(janela)
            janela = janela[ini:] + janela[:ini]

        n_detalhes_pedidos = 0
        n_boosts_achados = 0

        for ev_id in (com_boost + janela)[: config.ALTENAR_MAX_DETALHES]:
            try:
                detail_offers = await self._do_detalhe(ev_id, ligas)
                ofertas += detail_offers
                n_detalhes_pedidos += 1
                n_boosts_achados += len(detail_offers)
            except ScraperError as exc:
                # Não entra em `eventos_sondados`: a request falhou, então não
                # dá pra afirmar que os combos daquele evento sumiram.
                log.debug("detalhe do evento %s falhou: %s", ev_id, exc)
                continue
            self.eventos_sondados.add(str(ev_id))

        if n_mercados_turb or n_boosts_achados:
            log.info("%s/%s: %d mercado(s) turb. na listagem, %d boost(s) em %d detalhe(s)",
                     self.casa.slug, ESPORTES.get(sport_id, sport_id),
                     n_mercados_turb, n_boosts_achados, n_detalhes_pedidos)

        return ofertas

    # ------------------------------------------------------------------

    @staticmethod
    def _por_kickoff(ids: list[int], eventos: dict[int, dict]) -> list[int]:
        """Ordena por quem começa antes e corta o que não vale request.

        Fora: jogo já iniciado (não vira alerta nenhum) e jogo além de
        `ALTENAR_HORIZONTE_HORAS` (a Pinnacle quase não publica mercado tão
        cedo, então a avaliação falharia de qualquer jeito).

        Evento sem `startDate` vai pro fim em vez de sumir: não dá pra afirmar
        que está fora da janela, e sumir seria perder cobertura de casa que não
        publica horário na listagem.
        """
        horizonte = config.ALTENAR_HORIZONTE_HORAS * 60
        com_data: list[tuple[float, int]] = []
        sem_data: list[int] = []
        for ev_id in ids:
            faltam = minutos_ate_inicio(
                {"inicio_evento": (eventos.get(ev_id) or {}).get("startDate")})
            if faltam is None:
                sem_data.append(ev_id)
            elif 0 < faltam <= horizonte:
                com_data.append((faltam, ev_id))
        return [ev_id for _, ev_id in sorted(com_data)] + sem_data

    def _nome_evento(self, ev: dict, times: dict[int, str]) -> str:
        nome = ev.get("name")
        if nome:
            # A Altenar usa "A vs. B"; o matcher espera o separador " - ".
            return str(nome).replace(" vs. ", " - ").replace(" vs ", " - ")
        ids = ev.get("competitorIds") or []
        return " - ".join(times.get(i, "?") for i in ids[:2])

    def _do_mercado(self, ev: dict, mercado: dict, odds: dict,
                    times: dict[int, str], ligas: dict[int, str]) -> list[Offer]:
        """Mercado "1x2 - Odds Aumentadas": uma oferta por seleção."""
        evento = self._nome_evento(ev, times)
        liga = ligas.get(ev.get("champId"), "")
        saida: list[Offer] = []
        for oid in mercado.get("oddIds") or []:
            o = odds.get(oid)
            if not o or not o.get("price"):
                continue
            saida.append(Offer(
                casa=self.casa.nome,
                fonte=self.casa.fonte_1x2,
                evento_id=str(ev["id"]),
                evento=evento,
                mercado=f"Resultado Final: {o.get('name', '?')}",
                odd_original=None,   # só vem no detalhe do evento
                odd_boost=float(o["price"]),
                url=f"{self.casa.site}/sports/event/{ev['id']}",
                liga=liga or None,
                valido_ate=_iso(ev.get("startDate")),
                inicio_evento=_iso(ev.get("startDate")),
            ))
        return saida

    async def _do_detalhe(self, ev_id: int, ligas: dict[int, str]) -> list[Offer]:
        """Combos turbinados (`boosts`), que já trazem as duas odds."""
        d = await self._get("/GetEventDetails", eventId=ev_id)
        # Colhe os mercados completos ANTES de filtrar por boost: é a matéria
        # prima do consenso, e vale mesmo em evento que não tem boost nenhum.
        self.mercados_vistos += self._mercados_do_detalhe(d, ev_id, ligas)
        return self._ofertas_do_detalhe(d, ev_id, ligas)

    @staticmethod
    def _identidade_evento(d: dict, ligas: dict[int, str]
                           ) -> tuple[str | None, str | None, str | None]:
        """Nome, kickoff (ISO UTC) e liga do evento — do MESMO payload `d`.

        Compartilhado por `_ofertas_do_detalhe` e `_mercados_do_detalhe` de
        propósito: se as duas normalizassem o nome de jeitos diferentes (por
        exemplo uma tratando " vs. " como separador e a outra não), a ponte
        fuzzy de uma etapa futura compararia texto que a tabela `offers` nunca
        produz — e isso não dá erro nenhum, só casa errado.
        """
        nome = d.get("name")
        evento = (str(nome).replace(" vs. ", " - ").replace(" vs ", " - ")
                  if nome else None)
        liga = (d.get("champ") or {}).get("name") or ligas.get(d.get("champId")) or None
        inicio_evento = to_utc_iso(d.get("startDate"))
        return evento, inicio_evento, liga

    def _mercados_do_detalhe(self, d: dict, ev_id: int,
                             ligas: dict[int, str]) -> list[MercadoCasa]:
        """Todos os mercados do evento, com todas as seleções e preços.

        O payload já traz isso e o parser jogava fora: só as seleções citadas
        pelos boosts eram lidas. Sem o mercado inteiro não há de-vig
        (`value.models.Market.completo` exige todos os lados), e sem de-vig não
        há referência possível pros props — a Pinnacle não publica nenhum.

        Duas armadilhas do payload real, ambas conferidas na API:

        - o campo é `desktopOddIds`, **lista de listas** (`[[1],[2],[3]]`), não
          `oddIds`. `oddIds` existe na LISTAGEM (`_do_mercado`), não aqui;
        - metade dos mercados mora em `childMarkets`, não em `markets`. É onde
          ficam escanteios e cartões — justamente os que interessam.

        Mercado com menos de dois lados é descartado: não serve pra de-vig.
        """
        odds = {o["id"]: o for o in d.get("odds") or []}
        evento, inicio_evento, liga = self._identidade_evento(d, ligas)
        saida: list[MercadoCasa] = []

        for src in ("markets", "childMarkets"):
            for mkt in d.get(src) or []:
                nome_mkt = mkt.get("name") or mkt.get("shortName")
                if not nome_mkt:
                    continue

                brutos = (mkt.get("desktopOddIds") or mkt.get("mobileOddIds")
                          or mkt.get("oddIds") or [])
                ids = [i for sub in brutos
                       for i in (sub if isinstance(sub, list) else [sub])]

                selecoes = []
                for oid in ids:
                    o = odds.get(oid)
                    preco = _num((o or {}).get("price"))
                    # `> 1.0`: odd decimal válida paga mais que o apostado.
                    # 0/None é seleção suspensa, e entrar no de-vig como
                    # probabilidade infinita destruiria a margem do mercado.
                    if not o or preco is None or preco <= 1.0:
                        continue
                    selecoes.append((str(o.get("name") or "?"), preco))

                if len(selecoes) < 2:
                    continue

                saida += [
                    MercadoCasa(
                        casa=self.casa.nome,
                        evento_id=str(ev_id),
                        # O `market_id` importa tanto quanto o nome: a casa
                        # publica "Total de escanteios" uma vez por linha (9.5,
                        # 10.5...). Agrupar só pelo nome juntaria as linhas num
                        # mercado de 4 lados, a margem sairia ~100% e o de-vig
                        # descartaria tudo em silêncio.
                        market_id=str(mkt.get("id")),
                        market_nome=str(nome_mkt),
                        selecao=nome,
                        preco=preco,
                        evento=evento,
                        inicio_evento=inicio_evento,
                        liga=liga,
                    )
                    for nome, preco in selecoes
                ]
        return saida

    def _ofertas_do_detalhe(self, d: dict, ev_id: int,
                            ligas: dict[int, str]) -> list[Offer]:
        """Parse puro do detalhe. Separado do fetch porque a revalidação
        precisa da MESMA montagem de rótulo — comparar odd por um rótulo
        construído de outro jeito é como a linha 9.5→9 passaria batido."""
        boosts = d.get("boosts") or []
        if not boosts:
            return []

        evento, inicio_evento, liga = self._identidade_evento(d, ligas)
        evento = evento or ""
        mercados = {m["id"]: m for m in d.get("markets") or []}
        odds = {o["id"]: o for o in d.get("odds") or []}

        saida: list[Offer] = []
        for b in boosts:
            info = b.get("boostInfo") or {}
            # Promoção de conta nova: ganho absurdo, teto baixo, uma vez por CPF.
            # Ver a nota sobre `isWelcome` no topo do módulo.
            if info.get("isWelcome"):
                continue
            precos = [p for p in (b.get("price"), info.get("price")) if p]
            if len(precos) < 2:
                continue
            # O boost SOBE a odd: a maior é a turbinada. Confiar na ordem dos
            # campos aqui inverteria o edge inteiro se a API mudasse.
            original, turbinada = min(precos), max(precos)
            # O array também lista combos sem ganho real: `price` e
            # `boostInfo.price` idênticos. Visto ao vivo em 2026-08-05 na
            # VaiDeBet (eventos 16201772 e 16201756: 2.6 -> 2.6) e na
            # BateuBet (evento 16633614: 3.0 -> 3.0), ambos com
            # isWelcome=false. Mesma invariante que novibet.py/
            # casadeaposta.py/sportingtech.py já aplicam (`odd_boost <=
            # odd_original` é ruído, não oferta) — só a Altenar deixava
            # passar.
            if turbinada <= original:
                continue

            pernas = []
            for perna in b.get("odds") or []:
                sel = odds.get(perna.get("selectionId")) or {}
                mkt = mercados.get(perna.get("marketId")) or {}
                rotulo = sel.get("name") or "?"
                nome_mkt = mkt.get("name") or mkt.get("shortName")
                pernas.append(f"{nome_mkt}: {rotulo}" if nome_mkt else rotulo)
            if not pernas:
                continue

            saida.append(Offer(
                casa=self.casa.nome,
                fonte=self.casa.fonte_boost,
                evento_id=str(ev_id),
                evento=evento,
                mercado=" + ".join(pernas),
                odd_original=float(original),
                odd_boost=float(turbinada),
                url=f"{self.casa.site}/sports/event/{ev_id}",
                liga=liga,
                valido_ate=_iso(info.get("endDate") or d.get("startDate")),
                # Só `startDate`. O `endDate` do boost é o fim da PROMOÇÃO e
                # pode cair antes ou depois do apito — usá-lo como kickoff
                # erra nos dois sentidos.
                inicio_evento=inicio_evento,
                boost_pct=round((turbinada / original - 1) * 100, 1),
                limite_aposta=_num(info.get("betsLimit")),
            ))
        return saida

    async def revalidar(self, evento_id: str, mercado: str) -> float | None:
        """Odd turbinada AGORA para `mercado`, ou None se não existe mais.

        Um `GetEventDetails` cobre as duas fontes da casa: o array `boosts` e
        o mercado turbinado da listagem, que também vem no detalhe. Compara
        pelo rótulo montado pelo próprio parser — se a casa mexeu na linha
        (escanteios 9.5 → 9), o rótulo não bate e a resposta é None, que é
        exatamente o desejado: não é a mesma aposta.
        """
        d = await self._get("/GetEventDetails", eventId=int(evento_id))

        for o in self._ofertas_do_detalhe(d, int(evento_id), {}):
            if o.mercado == mercado:
                return o.odd_boost

        # Fonte `*_1x2`: a seleção turbinada da listagem também está aqui.
        odds = {o["id"]: o for o in d.get("odds") or []}
        for m in d.get("markets") or []:
            if not _e_turbinado(m.get("name")):
                continue
            for oid in m.get("oddIds") or []:
                o = odds.get(oid) or {}
                if o.get("price") and f"Resultado Final: {o.get('name', '?')}" == mercado:
                    return float(o["price"])

        return None


def _num(valor: Any) -> float | None:
    try:
        return float(valor) if valor is not None else None
    except (TypeError, ValueError):
        return None


def _iso(valor: Any) -> str | None:
    """A Altenar manda ISO com Z; alguns campos vêm em epoch ms."""
    if valor is None:
        return None
    if isinstance(valor, (int, float)):
        return millis_to_iso(int(valor))
    return str(valor)
