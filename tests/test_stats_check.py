"""Testes de `stats_check` — a camada de estatística do SofaScore.

Sem rede: um `FakeClient` (mesma interface pública de `SofaScoreClient` —
`buscar`/`jogos_time`/`lineups`) é injetado via `cliente=` em `checar_stats`.
Os formatos dos payloads (chaves, tipos, `missingPlayers`, `homeScore.period1`
etc.) são os observados na sondagem ao vivo de 2026-08 contra
`api.sofascore.com` (ver a seção "Achado real — SofaScore" na skill
`network-endpoint-recon`) — trimados aos campos que o módulo lê, não uma
cópia byte a byte do HTTP.

Um teste de integração de rede de verdade fica de fora de propósito (é o que
a skill do agente pede): bateria contra um jogo ao vivo quebra sozinha quando
o SofaScore muda o payload, e não é isso que a suíte principal precisa
detectar em CI.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from betano_superodds.value.stats_check import (
    N_JOGOS_ESTATISTICAS_EXTRA,
    N_JOGOS_MINIMO,
    JogoHistorico,
    SofaScoreError,
    _CandidatoEvento,
    _desfalques,
    _enriquecer_estatisticas_extra,
    _historico_time,
    _predicado_da_perna,
    checar_stats,
)
from betano_superodds.value.market_parser import parse_leg
from betano_superodds.value.models import Matchup

HOJE = datetime.now(timezone.utc)


def _ts(dias: float) -> int:
    return int((HOJE + timedelta(days=dias)).timestamp())


# --- fixtures: times e o jogo casado -----------------------------------

TIME_CASA = {"id": 9001, "name": "Real Time",
            "sport": {"slug": "football"}}
TIME_FORA = {"id": 9002, "name": "Visitante FC",
            "sport": {"slug": "football"}}

EVENTO_ID = 5001


def _resposta_busca(entidade: dict) -> dict:
    return {"results": [{"type": "team", "entity": entidade}]}


def _proximo_jogo() -> dict:
    return {
        "id": EVENTO_ID,
        "startTimestamp": _ts(2),
        "tournament": {"name": "Liga Teste"},
        "homeTeam": {"id": TIME_CASA["id"], "name": TIME_CASA["name"]},
        "awayTeam": {"id": TIME_FORA["id"], "name": TIME_FORA["name"]},
    }


# --- fixture: histórico do time da casa ---------------------------------
#
# 8 jogos encerrados, escolhidos a dedo pra que "marca nos 2 tempos" e
# "vence o 1º tempo" tenham uma frequência conjunta CONHECIDA e diferente do
# produto das marginais — dá pra conferir `diverge_da_estimativa_independente`
# na mão, não só olhar que saiu um número.
#
# (gols_pro, gols_contra, ht_pro, ht_contra, jogou_em_casa)
_JOGOS_CASA = (
    (2, 0, 1, 0, True),   # marca 2T: sim | vence 1T: sim  -> conjunto
    (1, 1, 0, 0, False),  # marca 2T: não | vence 1T: não
    (3, 1, 2, 0, True),   # marca 2T: sim | vence 1T: sim  -> conjunto
    (0, 0, 0, 0, False),  # marca 2T: não | vence 1T: não
    (2, 2, 1, 1, True),   # marca 2T: sim | vence 1T: não
    (1, 0, 1, 0, False),  # marca 2T: não | vence 1T: sim
    (4, 0, 2, 0, True),   # marca 2T: sim | vence 1T: sim  -> conjunto
    (1, 2, 0, 1, False),  # marca 2T: não | vence 1T: não
)
# marginal "marca nos 2 tempos"  = 4/8 = 0.50
# marginal "vence o 1º tempo"    = 4/8 = 0.50
# conjunta observada             = 3/8 = 0.375
# produto das marginais          = 0.25  -> diferença 0.125 (acima do limiar 0.12)


def _historico_casa_eventos() -> list[dict]:
    eventos = []
    for i, (gp, gc, hp, hc, casa) in enumerate(_JOGOS_CASA):
        gh, ga = (gp, gc) if casa else (gc, gp)
        hh, ha = (hp, hc) if casa else (hc, hp)
        home = {"id": TIME_CASA["id"]} if casa else {"id": 8000 + i}
        away = {"id": 8000 + i} if casa else {"id": TIME_CASA["id"]}
        eventos.append({
            "id": 7000 + i,
            "status": {"type": "finished"},
            "startTimestamp": _ts(-60 + i * 5),
            "homeTeam": home,
            "awayTeam": away,
            "homeScore": {"normaltime": gh, "period1": hh},
            "awayScore": {"normaltime": ga, "period1": ha},
        })
    return eventos


# --- fixture: escalação/desfalques (formato real, trimado) --------------
#
# Campos e valores tirados da sondagem ao vivo (recon 2026-08, evento futuro
# do Flamengo): `missingPlayers` responde mesmo com `confirmed: false`, com
# `type` ("missing"/"doubtful"), `reason` (11 = suspensão por cartão
# acumulado, 1 = lesão) e `proposedMarketValueRaw.value` — usado aqui como
# proxy de "é titular".
LINEUPS_COM_DESFALQUE = {
    "confirmed": False,
    "home": {
        "missingPlayers": [
            {
                "player": {"name": "Nicolás de la Cruz",
                          "proposedMarketValueRaw": {"value": 8_400_000}},
                "type": "missing", "reason": 11,
                "description": "yellow_card_accumulation_suspension",
            },
            {
                "player": {"name": "Luiz Araújo",
                          "proposedMarketValueRaw": {"value": 7_200_000}},
                "type": "missing", "reason": 1,
                "description": "Knee Injury",
            },
            {
                # Reserva de fim de banco — abaixo do piso de "titular".
                "player": {"name": "Fulano Reserva",
                          "proposedMarketValueRaw": {"value": 300_000}},
                "type": "missing", "reason": 1,
                "description": "Thigh Injury",
            },
        ],
    },
    "away": {
        "missingPlayers": [
            {
                "player": {"name": "Léo Pereira",
                          "proposedMarketValueRaw": {"value": 9_000_000}},
                "type": "doubtful", "reason": 1,
                "description": "Unknown",
            },
        ],
    },
}

LINEUPS_SEM_DESFALQUE = {"confirmed": True, "home": {"missingPlayers": []},
                        "away": {"missingPlayers": []}}


class FakeClient:
    """Mesma interface pública de `SofaScoreClient`, sem rede nenhuma."""

    def __init__(self, *, jogos_next=None, jogos_last=None, lineups=None,
                busca=None, estatisticas=None, explode_em: set[str] = frozenset()):
        self._next = jogos_next or {}
        self._last = jogos_last or {}
        self._lineups = lineups or {}
        self._busca = busca or {}
        self._estatisticas = estatisticas or {}
        self._explode_em = explode_em
        self.chamadas: list[tuple] = []

    def buscar(self, termo: str) -> dict:
        self.chamadas.append(("buscar", termo))
        if "buscar" in self._explode_em:
            raise SofaScoreError("boom")
        return self._busca.get(termo, {"results": []})

    def jogos_time(self, team_id: int, *, direcao: str, pagina: int = 0,
                   ttl_horas: float | None = None) -> list[dict]:
        # `ttl_horas` é ignorado aqui (não há cache no fake), mas precisa estar
        # na assinatura: o fake existe pra espelhar o cliente real, e uma
        # divergência vira TypeError em produção, não no teste.
        self.chamadas.append(("jogos_time", team_id, direcao, pagina))
        if "jogos_time" in self._explode_em:
            raise SofaScoreError("boom")
        alvo = self._next if direcao == "next" else self._last
        return alvo.get(team_id, {}).get(pagina, []) if isinstance(
            alvo.get(team_id), dict) else (alvo.get(team_id, []) if pagina == 0 else [])

    def lineups(self, event_id: int) -> dict | None:
        self.chamadas.append(("lineups", event_id))
        if "lineups" in self._explode_em:
            raise SofaScoreError("boom")
        return self._lineups.get(event_id)

    def estatisticas(self, event_id: int) -> dict | None:
        self.chamadas.append(("estatisticas", event_id))
        if "estatisticas" in self._explode_em:
            raise SofaScoreError("boom")
        return self._estatisticas.get(event_id)


def _client_padrao(**overrides) -> FakeClient:
    kwargs = dict(
        busca={
            "Real Time": _resposta_busca(TIME_CASA),
            "Visitante FC": _resposta_busca(TIME_FORA),
        },
        jogos_next={TIME_CASA["id"]: [_proximo_jogo()],
                   TIME_FORA["id"]: [_proximo_jogo()]},
        jogos_last={TIME_CASA["id"]: _historico_casa_eventos()},
        lineups={EVENTO_ID: LINEUPS_COM_DESFALQUE},
    )
    kwargs.update(overrides)
    return FakeClient(**kwargs)


def _payload_estatisticas(*, escanteios: tuple[int, int] | None = None,
                          amarelos: tuple[int, int] | None = None,
                          vermelhos: tuple[int, int] | None = None) -> dict:
    """Payload no formato real de `/event/{id}/statistics` (sondagem de
    2026-08-13), trimado aos itens que `liquidacao._parse_estatisticas` lê."""
    itens = []
    if escanteios is not None:
        itens.append({"name": "Corner kicks",
                      "home": str(escanteios[0]), "away": str(escanteios[1])})
    if amarelos is not None:
        itens.append({"name": "Yellow cards",
                      "home": str(amarelos[0]), "away": str(amarelos[1])})
    if vermelhos is not None:
        itens.append({"name": "Red cards",
                      "home": str(vermelhos[0]), "away": str(vermelhos[1])})
    return {"statistics": [{"period": "ALL",
                            "groups": [{"groupName": "Match overview",
                                       "statisticsItems": itens}]}]}


def evento_oferta(nome="Real Time - Visitante FC") -> dict:
    return {"evento": nome, "inicio_evento": (HOJE + timedelta(days=2)).isoformat()}


class TestFreqConjunta(unittest.TestCase):
    def test_mercado_simples_nao_calcula_conjunta(self):
        """Uma perna só não tem o que correlacionar — `None`, não `0`."""
        r = checar_stats(evento_oferta(), ["Ambas Equipes Marcam: Sim"],
                         cliente=_client_padrao())
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["diverge_da_estimativa_independente"])

    def test_combo_do_mesmo_time_calcula_conjunta_e_diverge(self):
        """Frequência batida na mão no comentário da fixture: 3/8 observada
        contra 0.25 do produto das marginais — diferença de 0.125, acima do
        limiar de divergência (0.12)."""
        r = checar_stats(
            evento_oferta(),
            ["Real Time Marcar em Ambos os Tempos", "Resultado do 1º Tempo Real Time"],
            cliente=_client_padrao(),
        )
        self.assertAlmostEqual(r["freq_conjunta_historica"], 0.375, places=4)
        self.assertTrue(r["diverge_da_estimativa_independente"])
        self.assertEqual(r["_n_jogos_amostra"], 8)

    def test_combo_de_times_diferentes_nao_calcula_conjunta(self):
        """Sem endpoint de H2H detalhado, não dá pra medir a conjunta entre
        um fato do time A e um fato do time B — `None`, honesto."""
        r = checar_stats(
            evento_oferta(),
            ["Resultado do 1º Tempo Real Time", "Visitante FC Marcar em Ambos os Tempos"],
            cliente=_client_padrao(),
        )
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["diverge_da_estimativa_independente"])

    def test_pernas_de_jogos_diferentes_nao_calculam_conjunta(self):
        """Regressão do "Festival de Gols" (Novibet, 2026-08-07): combo
        multi-jogo media a "conjunta" contra o histórico de UM time só (o
        do primeiro jogo casado), e o número saía degenerado — P(over 2.5)
        do mesmo jogo contado como se fosse a conjunta de 2 jogos distintos.
        Com `eventos_por_perna` discordando, `_freq_conjunta` recusa antes
        de bater no histórico."""
        pernas = ["Total de Gols Mais de 2.5", "Ambas Equipes Marcam: Sim"]
        # Sem `eventos_por_perna` (comportamento de sempre): calcula normal.
        sem_marcacao = checar_stats(evento_oferta(), pernas, cliente=_client_padrao())
        self.assertIsNotNone(sem_marcacao["freq_conjunta_historica"])

        # Com jogos diferentes marcados: recusa.
        com_marcacao = checar_stats(
            evento_oferta(), pernas, cliente=_client_padrao(),
            eventos_por_perna=["Jogo A - Time X", "Jogo B - Time Y"],
        )
        self.assertIsNone(com_marcacao["freq_conjunta_historica"])
        self.assertIsNone(com_marcacao["diverge_da_estimativa_independente"])

    def test_pernas_do_mesmo_jogo_com_eventos_por_perna_calculam_normal(self):
        """`eventos_por_perna` só recusa quando os jogos DIVERGEM — todas
        as pernas do mesmo jogo (o caso comum) segue calculando igual."""
        pernas = ["Total de Gols Mais de 2.5", "Ambas Equipes Marcam: Sim"]
        r = checar_stats(
            evento_oferta(), pernas, cliente=_client_padrao(),
            eventos_por_perna=["Real Time - Visitante FC", "Real Time - Visitante FC"],
        )
        self.assertIsNotNone(r["freq_conjunta_historica"])

    def test_perna_fora_do_escopo_de_placar_derruba_a_conjunta_inteira(self):
        """Uma perna de escanteio no meio do combo — nada aqui tem dado pra
        cobrir, e a conjunta do combo INTEIRO sai `None` (não parcial)."""
        r = checar_stats(
            evento_oferta(),
            ["Total de Gols Mais de 2.5", "Escanteios Mais de 9.5"],
            cliente=_client_padrao(),
        )
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["diverge_da_estimativa_independente"])

    def test_historico_insuficiente_e_none(self):
        cliente = _client_padrao(
            jogos_last={TIME_CASA["id"]: _historico_casa_eventos()[:3]})
        r = checar_stats(
            evento_oferta(),
            ["Real Time Marcar em Ambos os Tempos", "Resultado do 1º Tempo Real Time"],
            cliente=cliente,
        )
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertLess(3, N_JOGOS_MINIMO)  # a fixture realmente é curta demais

    def test_totals_e_btts_do_jogo_inteiro_nao_exigem_time_nomeado(self):
        """BTTS e total de gols são fatos do JOGO — dá pra medir a partir do
        histórico de qualquer um dos dois lados, sem nomear time na perna."""
        r = checar_stats(
            evento_oferta(),
            ["Total de Gols Mais de 2.5", "Ambas Equipes Marcam: Sim"],
            cliente=_client_padrao(),
        )
        self.assertIsNotNone(r["freq_conjunta_historica"])
        self.assertIn(r["diverge_da_estimativa_independente"], (True, False))


# --- fixture: histórico pra "forma recente" ------------------------------
#
# 8 jogos, escolhidos pra que o predicado "vence o 1º tempo" tenha uma queda
# CLARA entre os 5 mais recentes e a base de 8 — dá pra conferir a divergência
# na mão, igual à fixture de frequência conjunta acima.
#
# (gols_casa, gols_fora, ht_casa, ht_fora) — sempre TIME_CASA mandante, ordem
# CRESCENTE de data (índice 0 = mais antigo; `_historico_time` inverte).
_JOGOS_FORMA = (
    (2, 0, 1, 0),  # vence 1T: sim
    (1, 1, 1, 0),  # vence 1T: sim
    (3, 1, 2, 0),  # vence 1T: sim
    (0, 1, 0, 1),  # vence 1T: não
    (1, 1, 0, 0),  # vence 1T: não (empate)
    (0, 2, 0, 1),  # vence 1T: não
    (1, 2, 0, 0),  # vence 1T: não (empate)
    (2, 3, 1, 2),  # vence 1T: não
)
# Em ordem de recência (mais recente primeiro, índices 7..0):
#   [não, não, não, não, não, sim, sim, sim]
# taxa recente (5 mais novos)  = 0/5 = 0.0
# taxa base (8 no total)       = 3/8 = 0.375
# diferença                    = 0.375  -> acima do limiar (0.15)


def _historico_forma_eventos() -> list[dict]:
    eventos = []
    for i, (gh, ga, hh, ha) in enumerate(_JOGOS_FORMA):
        eventos.append({
            "id": 6000 + i,
            "status": {"type": "finished"},
            "startTimestamp": _ts(-60 + i * 5),
            "homeTeam": {"id": TIME_CASA["id"]},
            "awayTeam": {"id": 8100 + i},
            "homeScore": {"normaltime": gh, "period1": hh},
            "awayScore": {"normaltime": ga, "period1": ha},
        })
    return eventos


class TestFormaRecente(unittest.TestCase):
    def test_queda_clara_entre_recente_e_base_acende_o_sinal(self):
        cliente = _client_padrao(
            jogos_last={TIME_CASA["id"]: _historico_forma_eventos()})
        r = checar_stats(evento_oferta(), ["Resultado do 1º Tempo Real Time"],
                         cliente=cliente)
        self.assertTrue(r["forma_desfavoravel"])
        self.assertTrue(r["_forma_detalhe"])

    def test_diferenca_pequena_fica_false_nao_none(self):
        """Com a fixture de frequência conjunta (diferença batida na mão em
        0.1, abaixo do limiar de 0.15), o sinal avaliou e não achou nada —
        `False`, resposta real, não `None` de recusa."""
        r = checar_stats(evento_oferta(), ["Resultado do 1º Tempo Real Time"],
                         cliente=_client_padrao())
        self.assertFalse(r["forma_desfavoravel"])
        self.assertIsNotNone(r["forma_desfavoravel"])

    def test_historico_insuficiente_e_none(self):
        cliente = _client_padrao(
            jogos_last={TIME_CASA["id"]: _historico_forma_eventos()[:3]})
        r = checar_stats(evento_oferta(), ["Resultado do 1º Tempo Real Time"],
                         cliente=cliente)
        self.assertIsNone(r["forma_desfavoravel"])

    def test_perna_sem_time_especifico_nao_participa(self):
        """BTTS é fato do jogo inteiro — `_predicado_da_perna` devolve
        `time_interesse=None`, e forma não faz sentido pra ela."""
        r = checar_stats(evento_oferta(), ["Ambas Equipes Marcam: Sim"],
                         cliente=_client_padrao())
        self.assertIsNone(r["forma_desfavoravel"])

    def test_funciona_pra_mercado_simples_uma_perna_so(self):
        """Ao contrário de `freq_conjunta_historica` (que exige >=2 pernas),
        forma recente funciona com uma perna só — é o que estende o sinal
        pra ofertas SIMPLES, não só combo."""
        cliente = _client_padrao(
            jogos_last={TIME_CASA["id"]: _historico_forma_eventos()})
        r = checar_stats(evento_oferta(), ["Resultado do 1º Tempo Real Time"],
                         cliente=cliente)
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertTrue(r["forma_desfavoravel"])

    def test_falha_de_jogos_time_e_none_nao_derruba(self):
        """`jogos_time` também alimenta a busca de candidato de evento —
        falhando, nem chega a casar o jogo, e TUDO sai `None` (comportamento
        já coberto por `TestFalhasNuncaViramFalsoOuZero`; aqui confirmamos
        que `forma_desfavoravel` especificamente segue a mesma regra)."""
        r = checar_stats(evento_oferta(), ["Resultado do 1º Tempo Real Time"],
                         cliente=_client_padrao(explode_em={"jogos_time"}))
        self.assertIsNone(r["forma_desfavoravel"])


class TestEscanteiosECartoes(unittest.TestCase):
    """Escanteio e "total de cartões" passaram a ser cobertos em 2026-08-13
    (antes: `TestGuardaDeRegressaoNoStatsCheck` em `test_liquidacao.py`
    garantia que ficavam fora). Escanteio pede request extra em
    `/event/{id}/statistics` por jogo (`_enriquecer_estatisticas_extra`);
    cartão nem tem `market_key` (é SEM_COBERTURA no `market_parser`) — o
    predicado vem de uma regex local (`_CARTOES_TOTAL`), igual à de
    `liquidacao._RE_CARTOES_TOTAL`.
    """

    # 8 jogos, TIME_CASA sempre mandante (sem inverter perspectiva, ao
    # contrário de `_JOGOS_CASA`) — escanteios do TIME_CASA claramente MENOS
    # de 4.5 nos 5 mais recentes, e MAIS de 4.5 nos 3 mais antigos. Cartões
    # do TIME_CASA (amarelo + vermelho) seguem o mesmo padrão pra reusar a
    # mesma fixture nos dois sinais.
    _ESCANTEIOS_PRO = (6, 7, 6, 2, 3, 1, 2, 3)   # índice 0 = mais antigo
    _CARTOES_AMARELOS_PRO = (1, 1, 1, 3, 3, 4, 3, 3)

    def _eventos(self) -> list[dict]:
        eventos = []
        for i in range(8):
            eventos.append({
                "id": 7100 + i,
                "status": {"type": "finished"},
                "startTimestamp": _ts(-60 + i * 5),
                "homeTeam": {"id": TIME_CASA["id"], "name": TIME_CASA["name"]},
                "awayTeam": {"id": 8200 + i},
                "homeScore": {"normaltime": 1, "period1": 0},
                "awayScore": {"normaltime": 1, "period1": 0},
            })
        return eventos

    def _estatisticas_por_evento(self) -> dict:
        saida = {}
        for i in range(8):
            saida[7100 + i] = _payload_estatisticas(
                escanteios=(self._ESCANTEIOS_PRO[i], 3),
                amarelos=(self._CARTOES_AMARELOS_PRO[i], 2))
        return saida

    def _cliente(self, **overrides) -> FakeClient:
        kwargs = dict(jogos_last={TIME_CASA["id"]: self._eventos()},
                      estatisticas=self._estatisticas_por_evento())
        kwargs.update(overrides)
        return _client_padrao(**kwargs)

    # -- predicado isolado (sem rede) -------------------------------------

    def test_predicado_escanteios_total_le_do_market_key(self):
        cand = _CandidatoEvento(
            matchup=Matchup(id=EVENTO_ID, league="L", home_team="Real Time",
                           away_team="Visitante FC", commence_time=HOJE),
            home_id=TIME_CASA["id"], away_id=TIME_FORA["id"],
            home_nome="Real Time", away_nome="Visitante FC")
        leg = parse_leg("Total de Escanteios Mais de 9.5")
        pred, motivo = _predicado_da_perna(leg, cand)
        self.assertIsNotNone(pred, motivo)
        self.assertTrue(pred.precisa_estatisticas)
        self.assertIsNone(pred.time_interesse)  # fato do jogo, não de um time
        jogo = JogoHistorico(1, 1, None, None,
                             escanteios_pro=7, escanteios_contra=4)
        self.assertTrue(pred.avaliar(jogo))   # 11 > 9.5
        jogo_baixo = JogoHistorico(1, 1, None, None,
                                   escanteios_pro=2, escanteios_contra=3)
        self.assertFalse(pred.avaliar(jogo_baixo))  # 5 < 9.5
        self.assertIsNone(pred.avaliar(JogoHistorico(1, 1, None, None)))

    def test_predicado_cartoes_total_via_regex_local(self):
        cand = _CandidatoEvento(
            matchup=Matchup(id=EVENTO_ID, league="L", home_team="Real Time",
                           away_team="Visitante FC", commence_time=HOJE),
            home_id=TIME_CASA["id"], away_id=TIME_FORA["id"],
            home_nome="Real Time", away_nome="Visitante FC")
        leg = parse_leg("Total de Cartões Mais de 4.5")
        self.assertFalse(leg.suportado)  # SEM_COBERTURA no market_parser
        pred, motivo = _predicado_da_perna(leg, cand)
        self.assertIsNotNone(pred, motivo)
        self.assertTrue(pred.precisa_estatisticas)
        jogo = JogoHistorico(1, 1, None, None, cartoes_pro=3, cartoes_contra=3)
        self.assertTrue(pred.avaliar(jogo))    # 6 > 4.5
        self.assertIsNone(pred.avaliar(JogoHistorico(1, 1, None, None)))

    # -- enriquecimento (com o FakeClient) ---------------------------------

    def test_enriquecer_preenche_escanteios_e_cartoes_ate_o_teto(self):
        cliente = self._cliente()
        jogos = _historico_time(cliente, TIME_CASA["id"])
        self.assertEqual(len(jogos), 8)
        _enriquecer_estatisticas_extra(cliente, jogos)
        enriquecidos = [j for j in jogos if j.escanteios_pro is not None]
        # teto de custo: só os N_JOGOS_ESTATISTICAS_EXTRA primeiros da lista
        # (mais recentes, já que `_historico_time` devolve nessa ordem).
        self.assertEqual(len(enriquecidos), min(8, N_JOGOS_ESTATISTICAS_EXTRA))
        mais_recente = jogos[0]
        self.assertEqual(mais_recente.escanteios_pro, self._ESCANTEIOS_PRO[7])
        self.assertEqual(mais_recente.escanteios_contra, 3)
        self.assertEqual(mais_recente.cartoes_pro, self._CARTOES_AMARELOS_PRO[7])

    def test_vermelho_ausente_conta_como_zero_nao_desconhecido(self):
        """Diferente de `liquidacao.Intervalo` (ausência = desconhecido):
        aqui é sinal auxiliar de modo sombra, e ausência de vermelho quase
        sempre É zero de verdade (SofaScore omite estatística zerada)."""
        cliente = _client_padrao(
            jogos_last={TIME_CASA["id"]: self._eventos()[:1]},
            estatisticas={7100: _payload_estatisticas(amarelos=(2, 1))})
        jogos = _historico_time(cliente, TIME_CASA["id"])
        _enriquecer_estatisticas_extra(cliente, jogos)
        self.assertEqual(jogos[0].cartoes_pro, 2)
        self.assertEqual(jogos[0].cartoes_contra, 1)

    # -- fim a fim via checar_stats -----------------------------------------

    def test_forma_desfavoravel_para_escanteios_de_time(self):
        r = checar_stats(evento_oferta(), ["Real Time Escanteios Mais de 4.5"],
                         cliente=self._cliente())
        self.assertTrue(r["forma_desfavoravel"], r)

    def test_freq_conjunta_ignora_escanteios_sem_time_quando_indiferente(self):
        """"Total de Escanteios" (fato do jogo, sem time) só entra na
        conjunta — não em `forma_recente`, que exige `time_interesse`."""
        r = checar_stats(evento_oferta(),
                         ["Real Time Marcar em Ambos os Tempos",
                          "Total de Escanteios Mais de 9.5"],
                         cliente=self._cliente())
        # a conjunta roda (as duas pernas são cobertas agora); o que este
        # teste protege é só não quebrar com a mistura placar+escanteio.
        self.assertIn(r["diverge_da_estimativa_independente"], (True, False, None))


class TestDesfalques(unittest.TestCase):
    def test_filtra_por_valor_de_mercado_e_tipo(self):
        """O reserva de 300k fica de fora (heurística de titular); os dois
        nomes de valor alto entram, com o motivo/rótulo do payload real."""
        nomes = _desfalques(LINEUPS_COM_DESFALQUE, "home", "Real Time")
        self.assertEqual(len(nomes), 2)
        self.assertTrue(any("Nicolás de la Cruz" in n for n in nomes))
        self.assertTrue(any("Luiz Araújo" in n for n in nomes))
        self.assertFalse(any("Fulano Reserva" in n for n in nomes))

    def test_lineups_ausentes_nao_quebra(self):
        self.assertEqual(_desfalques(None, "home", "Real Time"), [])

    def test_sem_desfalque_e_false_nao_none(self):
        """Consulta que TEVE sucesso e não achou nada é uma resposta real:
        `False`, não `None` — `None` é reservado pra falha de consulta."""
        r = checar_stats(
            evento_oferta(), ["Ambas Equipes Marcam: Sim"],
            cliente=_client_padrao(lineups={EVENTO_ID: LINEUPS_SEM_DESFALQUE}),
        )
        self.assertEqual(r["desfalque_recente"], [])
        self.assertFalse(r["flag_noticia_fresca"])

    def test_com_desfalque_de_titular_liga_a_flag(self):
        r = checar_stats(evento_oferta(), ["Ambas Equipes Marcam: Sim"],
                         cliente=_client_padrao())
        self.assertTrue(r["flag_noticia_fresca"])
        self.assertEqual(len(r["desfalque_recente"]), 3)  # 2 casa + 1 fora


class TestFalhasNuncaViramFalsoOuZero(unittest.TestCase):
    def test_falha_de_lineups_e_none_nao_false(self):
        r = checar_stats(evento_oferta(), ["Ambas Equipes Marcam: Sim"],
                         cliente=_client_padrao(explode_em={"lineups"}))
        self.assertIsNone(r["flag_noticia_fresca"])
        self.assertIsNone(r["desfalque_recente"])

    def test_falha_de_busca_devolve_baseline_sem_derrubar(self):
        r = checar_stats(evento_oferta(), ["Ambas Equipes Marcam: Sim"],
                         cliente=_client_padrao(explode_em={"buscar"}))
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["flag_noticia_fresca"])
        self.assertIsNone(r["desfalque_recente"])

    def test_sem_candidato_de_evento_devolve_baseline(self):
        r = checar_stats(evento_oferta("Time Que Não Existe - Outro Fantasma"),
                         ["Ambas Equipes Marcam: Sim"], cliente=_client_padrao())
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["flag_noticia_fresca"])

    def test_evento_sem_separador_de_times_devolve_baseline(self):
        r = checar_stats({"evento": "nome sem separador"}, ["Ambas Equipes Marcam: Sim"])
        self.assertIsNone(r["freq_conjunta_historica"])
        self.assertIsNone(r["flag_noticia_fresca"])


class TestPredicadoDaPerna(unittest.TestCase):
    """Unidade: cada perna vira o predicado certo, na perspectiva certa."""

    def setUp(self):
        self.cand = _CandidatoEvento(
            matchup=Matchup(id=EVENTO_ID, league="Liga Teste",
                           home_team="Real Time", away_team="Visitante FC",
                           commence_time=HOJE),
            home_id=TIME_CASA["id"], away_id=TIME_FORA["id"],
            home_nome="Real Time", away_nome="Visitante FC",
        )

    def test_marca_ambos_tempos_resolve_time_por_nome(self):
        leg = parse_leg("Real Time Marcar em Ambos os Tempos")
        pred, motivo = _predicado_da_perna(leg, self.cand)
        self.assertIsNotNone(pred, motivo)
        self.assertEqual(pred.time_interesse, TIME_CASA["id"])
        self.assertTrue(pred.avaliar(JogoHistorico(2, 0, 1, 0, )))
        self.assertFalse(pred.avaliar(JogoHistorico(1, 0, 0, 0)))
        self.assertIsNone(pred.avaliar(JogoHistorico(1, 0, None, None)))

    def test_time_nao_reconhecido_recusa(self):
        leg = parse_leg("Time Fantasma Marcar em Ambos os Tempos")
        pred, motivo = _predicado_da_perna(leg, self.cand)
        self.assertIsNone(pred)
        self.assertIn("não casou", motivo)

    def test_perna_fora_de_escopo_devolve_motivo(self):
        """Chute a gol continua fora — diferente de escanteio/cartão (total),
        que passaram a ser cobertos em 2026-08-13 (ver `TestEscanteiosECartoes`)."""
        leg = parse_leg("Chutes ao Gol Mais de 3.5")
        pred, motivo = _predicado_da_perna(leg, self.cand)
        self.assertIsNone(pred)
        self.assertIsNotNone(motivo)

    def test_gols_exatos_de_equipe(self):
        leg = parse_leg("Real Time gols exatos: 2")
        pred, motivo = _predicado_da_perna(leg, self.cand)
        self.assertIsNotNone(pred, motivo)
        self.assertTrue(pred.avaliar(JogoHistorico(2, 1, None, None)))
        self.assertFalse(pred.avaliar(JogoHistorico(1, 1, None, None)))


if __name__ == "__main__":
    unittest.main()
