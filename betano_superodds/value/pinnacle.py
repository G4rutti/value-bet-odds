"""Scraper da Pinnacle — fonte da odd justa.

A Pinnacle expõe a API que alimenta o próprio site em
`guest.api.arcadia.pinnacle.com`, com uma `x-api-key` estática que vem no bundle
JS do frontend. É pública: sem login, sem cookie de sessão.

Endpoints usados:
    /sports/29/matchups                     todos os jogos de futebol (1 request)
    /matchups/{id}/related                  mercados derivados (BTTS, escanteios, 1ºT...)
    /matchups/{id}/markets/related/straight  os preços de tudo isso

Por que a Pinnacle e não a The Odds API: cobre 161 ligas (incl. Copa do Brasil,
Colômbia, Equador, Escócia — que faltavam), tem escanteios / BTTS / placar exato
/ 1º tempo, e não tem quota mensal.

⚠️ Preços vêm em formato americano e são convertidos pra decimal.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .fair_odds import _lado_do_time
from .models import Market, Matchup, american_para_decimal

log = logging.getLogger(__name__)

SOCCER_SPORT_ID = 29

# units de prop de jogador no basquete -> sufixo da chave interna.
STATS_JOGADOR: dict[str, str] = {
    "Points": "pontos",
    "Rebounds": "rebotes",
    "Assists": "assistencias",
    "Threes Made": "triplos",
    "Pts & Rebs & Asts": "pra",
}


# "{Time} Goals" / "{Time} Goals 1st Half" — gols exatos de UM time só.
# `config.SPECIAL_KEYS` é dict de match exato e não representa isto: o nome do
# time faz parte da descrição, então tem uma entrada por time por liga. Sondado
# ao vivo em `guest.api.arcadia.pinnacle.com` (2026-08): a forma é sempre
# "<Time> Goals" (jogo inteiro) ou "<Time> Goals 1st Half" — nunca "2nd Half"
# (a Pinnacle não publica 2º tempo em nada, mesma limitação do resto do
# projeto). Ancorado no fim ($, via `match` + fim de string) pra NÃO casar
# "<Time> Goals Odd/Even", que é um mercado completamente diferente (par/ímpar
# do total de gols do time, não gols exatos).
_TEAM_GOALS_RE = re.compile(r"^(?P<time>.+?)\s+Goals(?P<primeiro_tempo>\s+1st\s+Half)?$")


class PinnacleError(RuntimeError):
    pass


def _parse_time(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _por_designation(precos: list[dict]) -> dict[str, float]:
    """Lados que a API nomeia direto: home/away/draw/over/under."""
    saida: dict[str, float] = {}
    for p in precos:
        lado = p.get("designation")
        dec = american_para_decimal(p.get("price"))
        if lado and dec:
            saida[lado] = dec
    return saida


def _linha(precos: list[dict]) -> float | None:
    """A linha (`points`) do bloco. No spread é sempre a do mandante."""
    for p in precos:
        if p.get("designation") == "home" and p.get("points") is not None:
            return float(p["points"])
    for p in precos:
        if p.get("points") is not None:
            return float(p["points"])
    return None


class PinnacleScraper:
    """Busca odds de referência na Pinnacle."""

    def __init__(self, session: "curl_requests.Session | None" = None) -> None:
        self._session = session
        self._owns = session is None
        self._matchups_cache: dict[int, list[dict]] | None = None

    def __enter__(self) -> "PinnacleScraper":
        if self._session is None:
            self._session = curl_requests.Session(
                headers=config.PINNACLE_HEADERS,
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT,
            )
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            self._session.close()
            self._session = None

    def _get(self, path: str):
        assert self._session is not None, "use o scraper como context manager"
        try:
            r = self._session.get(f"{config.PINNACLE_BASE}{path}")
        except RequestException as exc:
            raise PinnacleError(f"falha de rede em {path}: {exc}") from exc
        if r.status_code != 200:
            raise PinnacleError(f"{path} respondeu HTTP {r.status_code}")
        return r.json()

    # ------------------------------------------------------------------

    def listar_jogos(self, sport_id: int = SOCCER_SPORT_ID, *,
                     use_cache: bool = True) -> list[dict]:
        """Os jogos raiz de um esporte. Um request cobre todas as ligas dele."""
        if self._matchups_cache is None:
            self._matchups_cache = {}
        if use_cache and sport_id in self._matchups_cache:
            return self._matchups_cache[sport_id]
        dados = self._get(f"/sports/{sport_id}/matchups")
        if not isinstance(dados, list):
            raise PinnacleError(f"resposta inesperada em /sports/{sport_id}/matchups")
        # Só os jogos "raiz": os derivados (escanteios, props) vêm por /related.
        raiz = [m for m in dados if not m.get("parentId") and m.get("participants")]
        self._matchups_cache[sport_id] = raiz
        log.debug("pinnacle: %d jogos no sportId %s", len(raiz), sport_id)
        return raiz

    def jogos_normalizados(self, sports: dict[int, str] | None = None) -> list[Matchup]:
        """Os jogos sem os mercados — só o suficiente pro matcher casar nomes.

        Os preços são caros de buscar (1 request por jogo), então só são
        carregados depois, pro jogo que realmente casou.

        Carrega todos os esportes de `config.SPORTS`: uma super odd de tênis ou
        basquete não tem como casar se a lista só tiver futebol. Um esporte que
        falha não derruba os outros — a lista sai menor, não vazia.
        """
        sports = config.SPORTS if sports is None else sports
        saida: list[Matchup] = []
        for sport_id, nome in sports.items():
            try:
                jogos = self.listar_jogos(sport_id)
            except PinnacleError as exc:
                log.warning("sportId %s (%s) falhou: %s", sport_id, nome, exc)
                continue
            for m in jogos:
                casa = next((p for p in m["participants"] if p.get("alignment") == "home"), None)
                fora = next((p for p in m["participants"] if p.get("alignment") == "away"), None)
                if not casa or not fora:
                    continue
                saida.append(Matchup(
                    id=m["id"],
                    league=(m.get("league") or {}).get("name", ""),
                    home_team=casa.get("name", ""),
                    away_team=fora.get("name", ""),
                    commence_time=_parse_time(m.get("startTime")),
                    sport=nome,
                ))
        return saida

    # ------------------------------------------------------------------

    def carregar_mercados(self, matchup: Matchup) -> Matchup:
        """Preenche `matchup.markets` com tudo que soubermos mapear (2 requests)."""
        try:
            relacionados = self._get(f"/matchups/{matchup.id}/related")
            blocos = self._get(f"/matchups/{matchup.id}/markets/related/straight")
        except PinnacleError as exc:
            log.warning("sem mercados para %s: %s", matchup.display_name, exc)
            return matchup

        # matchupId -> metadados, pra saber o que cada bloco de preço representa.
        contexto: dict[int, dict] = {}
        for r in relacionados or []:
            contexto[r["id"]] = {
                "units": r.get("units"),
                "descricao": (r.get("special") or {}).get("description"),
                # Specials usam participantId no preço em vez de designation.
                # Nem todo participante traz id (jogos derivados sem preço próprio).
                "participantes": {
                    p["id"]: p.get("name", "")
                    for p in r.get("participants", []) or []
                    if p.get("id") is not None
                },
            }

        for bloco in blocos or []:
            ctx = contexto.get(bloco.get("matchupId"))
            if ctx is None:
                continue
            market = self._mapear(bloco, ctx, matchup)
            if market and market.completo:
                matchup.markets[market.key] = market

        log.debug("%s: %d mercados mapeados", matchup.display_name, len(matchup.markets))
        return matchup

    def _mapear(self, bloco: dict, ctx: dict, matchup: Matchup) -> Market | None:
        """Traduz um bloco de preços da Pinnacle num Market nosso.

        A Pinnacle reusa os mesmos `type` em todos os esportes — `total` é gol
        no futebol, game no tênis e ponto no basquete —, então o despacho é por
        esporte antes de qualquer outra coisa.
        """
        if matchup.sport == "tennis":
            return self._mapear_tenis(bloco, ctx)
        if matchup.sport == "basketball":
            return self._mapear_basquete(bloco, ctx)
        return self._mapear_futebol(bloco, ctx, matchup)

    # ------------------------------------------------------------------
    # futebol
    # ------------------------------------------------------------------

    def _mapear_futebol(self, bloco: dict, ctx: dict, matchup: Matchup) -> Market | None:
        tipo = bloco.get("type")
        periodo = bloco.get("period", 0)
        unidade = ctx.get("units")
        descricao = ctx.get("descricao")
        precos = bloco.get("prices") or []
        if periodo not in (0, 1):
            return None

        sufixo = "" if periodo == 0 else "_1t"

        # --- jogo principal e escanteios --------------------------------
        # Os dois têm a mesma forma (moneyline/total/spread/team_total); só
        # muda o prefixo da chave e o rótulo.
        if descricao is None and unidade in ("Regular", "Corners"):
            escanteio = unidade == "Corners"
            lados = _por_designation(precos)
            if not lados:
                return None
            linha = _linha(precos)

            if tipo == "moneyline" and not escanteio:
                return Market(key=f"h2h{sufixo}", label="Resultado Final",
                              outcomes=lados, period=periodo)

            if tipo == "total":
                if linha is None:
                    return None
                # Escanteios mantêm a chave histórica `corners:{linha}`.
                chave = (f"corners{sufixo}:{linha}" if escanteio
                         else f"totals{sufixo}:{linha}")
                rotulo = (f"Escanteios {linha}" if escanteio
                          else f"Total de Gols {linha}")
                return Market(key=chave, label=rotulo, outcomes=lados, period=periodo)

            # Handicap asiático — o mercado mais líquido da Pinnacle, e o que
            # estava sendo descartado por não ter branch aqui.
            if tipo == "spread":
                if linha is None:
                    return None
                prefixo = "corners_spread" if escanteio else "spread"
                return Market(key=f"{prefixo}{sufixo}:{linha}",
                              label=f"Handicap {linha}", outcomes=lados, period=periodo)

            # Total de uma equipe só. `side` diz de quem é a linha.
            if tipo == "team_total":
                lado = bloco.get("side")
                if linha is None or lado not in ("home", "away"):
                    return None
                prefixo = "corners_team_total" if escanteio else "team_total"
                return Market(key=f"{prefixo}{sufixo}:{lado}:{linha}",
                              label=f"Total {lado} {linha}", outcomes=lados, period=periodo)
            return None

        # --- specials nomeados (BTTS, dupla chance, placar exato...) --------
        if descricao:
            return self._mapear_special(bloco, ctx, descricao, periodo, matchup)

        return None

    def _mapear_special(self, bloco: dict, ctx: dict, descricao: str,
                        periodo: int, matchup: Matchup) -> Market | None:
        """Special cujos lados vêm por participantId em vez de designation."""
        chave = config.SPECIAL_KEYS.get(descricao)
        if chave is None:
            chave = self._resolver_team_goals(descricao, matchup)
        if chave is None:
            return None
        participantes = ctx.get("participantes") or {}
        outcomes: dict[str, float] = {}
        for p in bloco.get("prices") or []:
            nome = participantes.get(p.get("participantId"))
            dec = american_para_decimal(p.get("price"))
            if nome and dec:
                outcomes[nome] = dec
        if not outcomes:
            return None
        return Market(key=chave, label=descricao, outcomes=outcomes, period=periodo)

    def _resolver_team_goals(self, descricao: str, matchup: Matchup) -> str | None:
        """"Internacional Goals 1st Half" -> "team_exact_goals_1t:away".

        Reusa `_lado_do_time` de `fair_odds.py` — mesmo fuzzy-match que resolve
        nome de time em qualquer outro lugar do projeto, pra não duplicar a
        lógica nem o limiar de confiança (70).

        ⚠️ Partição incompleta: os buckets deste special vêm `0..4` SEM
        terminal "4+" (conferido na sondagem ao vivo — `Exact Total Goals`
        tem `4+`, este não). `remover_vig` normaliza pra 1 do jeito que está,
        o que infla cada probabilidade um pouco; pra gols de UM time a cauda
        omitida é ~0.5%, tolerável mas registrado aqui porque é onde o dado
        nasce incompleto.
        """
        m = _TEAM_GOALS_RE.match(descricao)
        if not m:
            return None
        lado = _lado_do_time(m.group("time").strip(), matchup)
        if lado is None:
            return None
        sufixo = "_1t" if m.group("primeiro_tempo") else ""
        return f"team_exact_goals{sufixo}:{lado}"

    # ------------------------------------------------------------------
    # tênis
    # ------------------------------------------------------------------

    def _mapear_tenis(self, bloco: dict, ctx: dict) -> Market | None:
        """Tênis: `units` separa contagem de games de contagem de sets.

        Os períodos são 0 = partida, 1..5 = set N. A API também publica
        moneyline de game individual (períodos 14+); esses são ignorados.
        """
        tipo = bloco.get("type")
        periodo = bloco.get("period", 0)
        unidade = ctx.get("units")
        precos = bloco.get("prices") or []
        if periodo not in (0, 1, 2, 3, 4, 5):
            return None

        lados = _por_designation(precos)
        if not lados:
            return None
        linha = _linha(precos)
        sufixo = "" if periodo == 0 else f"_s{periodo}"

        if unidade == "Games":
            if tipo == "total" and linha is not None:
                return Market(key=f"games{sufixo}:{linha}",
                              label=f"Total de Games {linha}", outcomes=lados, period=periodo)
            if tipo == "spread" and linha is not None:
                return Market(key=f"games_spread{sufixo}:{linha}",
                              label=f"Handicap de Games {linha}", outcomes=lados, period=periodo)
            return None

        if unidade == "Sets":
            if tipo == "moneyline":
                # Período 0 é o vencedor da PARTIDA — mesma coisa que o `h2h` do
                # futebol e do basquete, e é assim que "Vencedor <jogador>" chega
                # do parser. Períodos 1..5 são o vencedor do set N.
                chave = "h2h" if periodo == 0 else f"sets_h2h{sufixo}"
                return Market(key=chave, label="Vencedor",
                              outcomes=lados, period=periodo)
            if tipo == "total" and linha is not None:
                return Market(key=f"sets_total{sufixo}:{linha}",
                              label=f"Total de Sets {linha}", outcomes=lados, period=periodo)
            if tipo == "spread" and linha is not None:
                return Market(key=f"sets_spread{sufixo}:{linha}",
                              label=f"Handicap de Sets {linha}", outcomes=lados, period=periodo)
        return None

    # ------------------------------------------------------------------
    # basquete
    # ------------------------------------------------------------------

    def _mapear_basquete(self, bloco: dict, ctx: dict) -> Market | None:
        """Basquete: jogo (`Regular`) e props de jogador (`units` = a estatística).

        Nos props os lados não vêm por designation: são participantes chamados
        "Over"/"Under", e o jogador está na `description` ("Awa Fam Total
        Points"). São normalizados pra over/under, igual ao resto.
        """
        tipo = bloco.get("type")
        periodo = bloco.get("period", 0)
        unidade = ctx.get("units")
        descricao = ctx.get("descricao")
        precos = bloco.get("prices") or []

        if unidade == "Regular" and descricao is None:
            if periodo not in (0, 1):
                return None
            sufixo = "" if periodo == 0 else "_1t"
            lados = _por_designation(precos)
            if not lados:
                return None
            linha = _linha(precos)
            if tipo == "moneyline":
                return Market(key=f"h2h{sufixo}", label="Vencedor",
                              outcomes=lados, period=periodo)
            if tipo == "total" and linha is not None:
                return Market(key=f"totals{sufixo}:{linha}",
                              label=f"Total de Pontos {linha}", outcomes=lados, period=periodo)
            if tipo == "spread" and linha is not None:
                return Market(key=f"spread{sufixo}:{linha}",
                              label=f"Handicap {linha}", outcomes=lados, period=periodo)
            if tipo == "team_total":
                lado = bloco.get("side")
                if linha is None or lado not in ("home", "away"):
                    return None
                return Market(key=f"team_total{sufixo}:{lado}:{linha}",
                              label=f"Total {lado} {linha}", outcomes=lados, period=periodo)
            return None

        # --- prop de jogador -------------------------------------------
        stat = STATS_JOGADOR.get(unidade or "")
        if stat is None or not descricao or tipo != "total":
            return None
        # "Awa Fam Total Points" - " Total Points" -> "Awa Fam"
        jogador = descricao[: -len(f" Total {unidade}")] if descricao.endswith(
            f" Total {unidade}") else None
        if not jogador:
            return None

        participantes = ctx.get("participantes") or {}
        outcomes: dict[str, float] = {}
        linha = None
        for p in precos:
            nome = (participantes.get(p.get("participantId")) or "").lower()
            dec = american_para_decimal(p.get("price"))
            if nome in ("over", "under") and dec:
                outcomes[nome] = dec
                if p.get("points") is not None:
                    linha = float(p["points"])
        if linha is None or not outcomes:
            return None

        from .matcher import normalizar  # normalização única dos dois lados
        return Market(key=f"player:{stat}:{normalizar(jogador)}:{linha}",
                      label=f"{jogador} — {unidade} {linha}",
                      outcomes=outcomes, period=periodo)
