"""Testes do diff e da persistência.

Os ramos "alterada" e "expirada" não dá pra validar só olhando o site — dependem
de a Betano mexer nas odds enquanto o loop roda. Aqui eles são forçados.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betano_superodds import config
from betano_superodds.diff import diff_offers
from betano_superodds.models import Offer, minutos_ate_inicio, to_utc_iso
from betano_superodds.storage import Storage


def daqui(horas: float) -> str:
    """Horário relativo ao agora, em ISO local com offset."""
    return (datetime.now(timezone.utc) + timedelta(hours=horas)
            ).astimezone().isoformat(timespec="seconds")


def make_offer(mercado="Resultado Final: Flamengo", boost=5.0, original=3.2,
               evento="Flamengo - Palmeiras", evento_id="123", fonte="mr12",
               inicio_evento=None) -> Offer:
    return Offer(
        fonte=fonte,
        evento_id=evento_id,
        evento=evento,
        mercado=mercado,
        odd_original=original,
        odd_boost=boost,
        url="https://www.betano.bet.br/odds/flamengo-palmeiras/123/",
        liga="Brasileirão",
        valido_ate=daqui(28),
        # Relativo, não fixo: o diff e a fila decidem pelo horário do jogo, e
        # uma data fixa faz o teste medir o calendário em vez do código.
        
        inicio_evento=daqui(28) if inicio_evento is None else inicio_evento,
    )


class TestOfferIdentity(unittest.TestCase):
    def test_id_ignores_odds_so_changes_are_detectable(self):
        """Se odd_boost entrasse no offer_id, um reajuste viraria expirada+nova."""
        a, b = make_offer(boost=5.0), make_offer(boost=5.5)
        self.assertEqual(a.offer_id, b.offer_id)
        self.assertNotEqual(a.content_hash, b.content_hash)

    def test_different_market_is_different_offer(self):
        a = make_offer(mercado="Resultado Final: Flamengo")
        b = make_offer(mercado="Resultado Final: Empate")
        self.assertNotEqual(a.offer_id, b.offer_id)

    def test_ganho_pct(self):
        self.assertEqual(make_offer(boost=5.0, original=4.0).ganho_pct, 25.0)
        self.assertIsNone(make_offer(original=None).ganho_pct)


class TestDiff(unittest.TestCase):
    def test_new_offer(self):
        result = diff_offers({}, [make_offer()])
        self.assertEqual(len(result.novas), 1)
        self.assertFalse(result.alteradas or result.expiradas)
        self.assertTrue(result.has_changes)

    def test_unchanged_offer(self):
        offer = make_offer()
        previous = {offer.offer_id: {**offer.to_row()}}
        result = diff_offers(previous, [offer])
        self.assertEqual(len(result.inalteradas), 1)
        self.assertFalse(result.has_changes)
        self.assertEqual(result.total_ativas, 1)

    def test_changed_odd(self):
        before = make_offer(boost=5.0)
        after = make_offer(boost=6.5)
        previous = {before.offer_id: {**before.to_row()}}

        result = diff_offers(previous, [after])
        self.assertEqual(len(result.alteradas), 1)
        self.assertFalse(result.novas or result.expiradas)

        change = result.alteradas[0]
        self.assertEqual(change.odd_boost_anterior, 5.0)
        self.assertEqual(change.offer.odd_boost, 6.5)

    def test_expired_offer(self):
        gone = make_offer()
        previous = {gone.offer_id: {**gone.to_row(), "evento": gone.evento,
                                    "mercado": gone.mercado}}
        result = diff_offers(previous, [])
        self.assertEqual(len(result.expiradas), 1)
        self.assertEqual(result.total_ativas, 0)

    def test_mixed_cycle(self):
        kept = make_offer(mercado="Resultado Final: Flamengo", boost=5.0)
        bumped_before = make_offer(mercado="Resultado Final: Empate", boost=3.0)
        bumped_after = make_offer(mercado="Resultado Final: Empate", boost=3.4)
        gone = make_offer(mercado="Resultado Final: Palmeiras", boost=8.0)
        fresh = make_offer(mercado="Ambas marcam: Sim", boost=2.1)

        previous = {o.offer_id: {**o.to_row()} for o in (kept, bumped_before, gone)}
        result = diff_offers(previous, [kept, bumped_after, fresh])

        self.assertEqual([o.mercado for o in result.novas], ["Ambas marcam: Sim"])
        self.assertEqual([c.offer.mercado for c in result.alteradas], ["Resultado Final: Empate"])
        self.assertEqual([r["mercado"] for r in result.expiradas], ["Resultado Final: Palmeiras"])
        self.assertEqual(result.total_ativas, 3)


class TestTempoAteOInicio(unittest.TestCase):
    """Helpers de tempo: é deles que dependem o diff, a fila e o alerta."""

    def test_to_utc_iso_normaliza_todos_os_formatos_das_casas(self):
        esperado = "2026-08-05T18:00:00+00:00"
        millis = int(datetime(2026, 8, 5, 18, tzinfo=timezone.utc).timestamp() * 1000)
        self.assertEqual(to_utc_iso("2026-08-05T18:00:00Z"), esperado)
        self.assertEqual(to_utc_iso("2026-08-05T15:00:00-03:00"), esperado)
        self.assertEqual(to_utc_iso(millis), esperado)
        self.assertEqual(
            to_utc_iso(datetime(2026, 8, 5, 18, tzinfo=timezone.utc)), esperado)

    def test_to_utc_iso_devolve_none_sem_data_legivel(self):
        for lixo in (None, "", "amanhã", "2026-13-45"):
            self.assertIsNone(to_utc_iso(lixo))

    def test_naive_e_lido_como_local(self):
        """Mesma convenção do resto do projeto — trocar isso desloca 3h."""
        local = datetime(2026, 8, 5, 18).astimezone()
        self.assertEqual(to_utc_iso("2026-08-05T18:00:00"),
                         local.astimezone(timezone.utc).isoformat(timespec="seconds"))

    def test_minutos_ate_inicio_conta_para_frente_e_para_tras(self):
        self.assertAlmostEqual(
            minutos_ate_inicio({"inicio_evento": daqui(2)}), 120, delta=1)
        self.assertAlmostEqual(
            minutos_ate_inicio({"inicio_evento": daqui(-0.5)}), -30, delta=1)

    def test_valido_ate_e_so_fallback(self):
        """Oferta gravada antes da coluna existir ainda precisa responder."""
        row = {"inicio_evento": None, "valido_ate": daqui(1)}
        self.assertAlmostEqual(minutos_ate_inicio(row), 60, delta=1)
        # Com os dois, quem manda é o kickoff — o `valido_ate` da Altenar é o
        # fim da promoção e pode estar em qualquer lugar.
        row = {"inicio_evento": daqui(3), "valido_ate": daqui(1)}
        self.assertAlmostEqual(minutos_ate_inicio(row), 180, delta=1)

    def test_sem_data_nenhuma_responde_none(self):
        self.assertIsNone(minutos_ate_inicio({"inicio_evento": None,
                                              "valido_ate": None}))

    def test_offer_normaliza_o_inicio_na_construcao(self):
        o = make_offer(inicio_evento="2026-08-05T18:00:00Z")
        self.assertEqual(o.inicio_evento, "2026-08-05T18:00:00+00:00")
        self.assertEqual(o.to_row()["inicio_evento"], "2026-08-05T18:00:00+00:00")


class TestDiffJogoIniciado(unittest.TestCase):
    """Regressão: 7 dos 50 alertas medidos saíram DEPOIS do apito inicial.

    A Betano continua publicando combo de tênis com o jogo em andamento, e o
    teste de vencimento só rodava sobre o que sumia do snapshot — então a
    oferta nova entrava lisa e a que continuava publicada nunca expirava.
    """

    def test_oferta_nova_de_jogo_em_andamento_nao_entra(self):
        result = diff_offers({}, [make_offer(inicio_evento=daqui(-0.1))])
        self.assertFalse(result.novas)
        self.assertFalse(result.has_changes)

    def test_oferta_ainda_publicada_expira_quando_o_jogo_comeca(self):
        offer = make_offer(inicio_evento=daqui(-0.1))
        previous = {offer.offer_id: {**offer.to_row()}}
        # A casa MANDOU a oferta de novo — é esse o caso que passava batido.
        result = diff_offers(previous, [offer])
        self.assertEqual(len(result.expiradas), 1)
        self.assertFalse(result.inalteradas)

    def test_reajuste_de_odd_de_jogo_iniciado_tambem_nao_passa(self):
        antes = make_offer(boost=5.0, inicio_evento=daqui(-0.1))
        depois = make_offer(boost=6.5, inicio_evento=daqui(-0.1))
        result = diff_offers({antes.offer_id: {**antes.to_row()}}, [depois])
        self.assertFalse(result.alteradas)
        self.assertEqual(len(result.expiradas), 1)

    def test_sem_data_legivel_nao_expira(self):
        """Chutar seria pior: casa que não publica horário perderia tudo."""
        offer = make_offer(inicio_evento="")
        offer.valido_ate = None
        result = diff_offers({}, [offer])
        self.assertEqual(len(result.novas), 1)

    def test_jogo_prestes_a_comecar_ainda_entra(self):
        """O corte é o apito, não a antecedência — quem marca os apertados é
        o alerta."""
        result = diff_offers({}, [make_offer(inicio_evento=daqui(0.05))])
        self.assertEqual(len(result.novas), 1)

    def test_descarte_por_jogo_em_andamento_e_logado(self):
        """Antes, o `continue` de `_ja_comecou` era mudo — foi isso que fez
        uma família inteira de oferta (tênis "Total de Games" da Betano)
        sumir do alerta sem nenhuma pista no log (2026-08-04/05). Agora o
        ciclo loga quantas ofertas descartou, por (casa, fonte)."""
        ofertas = [
            make_offer(fonte="smartpick", inicio_evento=daqui(-0.1)),
            make_offer(fonte="smartpick", evento_id="456", inicio_evento=daqui(-0.2)),
            make_offer(fonte="mr12", evento_id="789", inicio_evento=daqui(0.1)),
        ]
        with self.assertLogs("betano_superodds.diff", level="INFO") as cm:
            result = diff_offers({}, ofertas)
        self.assertEqual(len(result.novas), 1, "só a que não começou entra")
        linha = "\n".join(cm.output)
        self.assertIn("descartadas 2 oferta(s)", linha)
        self.assertIn("Betano/smartpick: 2", linha)

    def test_sem_descarte_nao_loga_nada(self):
        with self.assertNoLogs("betano_superodds.diff", level="INFO"):
            diff_offers({}, [make_offer(inicio_evento=daqui(28))])


class TestStorage(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def test_full_lifecycle(self):
        """nova → inalterada → alterada → expirada, com histórico em cada passo."""
        offer = make_offer(boost=5.0)

        # 1. nova
        self.storage.apply_diff(diff_offers({}, [offer]))
        active = self.storage.active_offers()
        self.assertEqual(len(active), 1)
        row = active[offer.offer_id]
        self.assertEqual(row["odd_boost"], 5.0)
        self.assertEqual(row["active"], 1)
        first_seen = row["first_seen"]

        # 2. inalterada — não pode duplicar linha nem inflar histórico
        self.storage.apply_diff(diff_offers(self.storage.active_offers(), [offer]))
        self.assertEqual(self.storage.stats(), {"total": 1, "ativas": 1, "historico": 1})

        # 3. alterada — atualiza a linha e registra histórico
        bumped = make_offer(boost=6.5)
        self.storage.apply_diff(diff_offers(self.storage.active_offers(), [bumped]))
        row = self.storage.active_offers()[offer.offer_id]
        self.assertEqual(row["odd_boost"], 6.5)
        self.assertEqual(row["first_seen"], first_seen, "first_seen não deve ser sobrescrito")
        self.assertEqual(self.storage.stats()["historico"], 2)

        # 4. expirada — sai das ativas mas continua no banco
        self.storage.apply_diff(diff_offers(self.storage.active_offers(), []))
        self.assertEqual(self.storage.active_offers(), {})
        self.assertEqual(self.storage.stats(), {"total": 1, "ativas": 0, "historico": 3})

    def test_unchanged_offer_still_refreshes_metadata(self):
        """url/valido_ate/liga não entram no content_hash — têm que ser renovados
        mesmo quando as odds não mudam, senão ficam congelados pra sempre."""
        offer = make_offer()
        self.storage.apply_diff(diff_offers({}, [offer]))

        remarcado = make_offer()
        remarcado.valido_ate = "2026-09-01T18:00:00-03:00"
        remarcado.url = "https://www.betano.bet.br/match-odds/flamengo-palmeiras/123/"

        result = diff_offers(self.storage.active_offers(), [remarcado])
        self.assertEqual(len(result.inalteradas), 1, "odds iguais => inalterada")
        self.storage.apply_diff(result)

        row = self.storage.active_offers()[offer.offer_id]
        self.assertEqual(row["valido_ate"], "2026-09-01T18:00:00-03:00")
        self.assertIn("match-odds", row["url"])
        self.assertEqual(self.storage.stats()["historico"], 1, "sem odd nova, sem histórico")

    def test_reappearing_offer_becomes_active_again(self):
        offer = make_offer()
        self.storage.apply_diff(diff_offers({}, [offer]))
        self.storage.apply_diff(diff_offers(self.storage.active_offers(), []))
        self.assertEqual(self.storage.active_offers(), {})

        self.storage.apply_diff(diff_offers({}, [offer]))
        self.assertEqual(len(self.storage.active_offers()), 1)
        self.assertEqual(self.storage.stats()["total"], 1, "não deve criar linha duplicada")

    def test_migracao_cria_inicio_evento_em_banco_antigo(self):
        """Quem já rodava não pode quebrar na abertura — nem no CREATE INDEX."""
        caminho = Path(self._tmp.name) / "antigo.db"
        antigo = sqlite3.connect(caminho)
        antigo.execute("""
            CREATE TABLE offers (
                offer_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                fonte TEXT NOT NULL, evento_id TEXT NOT NULL, evento TEXT NOT NULL,
                liga TEXT, mercado TEXT NOT NULL, odd_original REAL,
                odd_boost REAL NOT NULL, boost_pct REAL, valido_ate TEXT, url TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1)
        """)
        antigo.execute(
            "INSERT INTO offers VALUES ('abc','h','mr12','1','A - B',NULL,'M',"
            "2.0,3.0,NULL,?,'u','2026-01-01T00:00:00-03:00',"
            "'2026-01-01T00:00:00-03:00',1)", (daqui(5),))
        antigo.commit()
        antigo.close()

        with Storage(caminho) as storage:
            row = storage.active_offers()["abc"]
            self.assertIsNone(row["inicio_evento"])
            # Sem `inicio_evento`, a fila ainda tem que enxergar a oferta.
            self.assertEqual(
                [r["offer_id"] for r in storage.pendentes_de_avaliacao(10, 60)],
                ["abc"])

    def test_breakage_detection(self):
        self.assertEqual(self.storage.consecutive_failed_runs(), 0)
        self.storage.record_run(ofertas=0)
        self.storage.record_run(ofertas=0, erro="boom")
        self.assertEqual(self.storage.consecutive_failed_runs(), 2)

        # Um ciclo bom zera o contador.
        self.storage.record_run(ofertas=7, novas=7)
        self.assertEqual(self.storage.consecutive_failed_runs(), 0)


class TestFilaPorUrgencia(unittest.TestCase):
    """A fila decide QUEM é avaliado com o orçamento do ciclo.

    Medido antes desta mudança: 114 pendentes para um teto de 60, com 699 das
    800 ofertas ativas em jogos a 12-48h — faixa onde a Pinnacle quase não
    publica mercado. O jogo desta tarde esperava atrás de todos eles.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "test.db")

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _gravar(self, *ofertas: Offer) -> None:
        self.storage.apply_diff(diff_offers({}, list(ofertas)))

    def _fila(self, limite: int = 10) -> list[str]:
        return [r["mercado"] for r in
                self.storage.pendentes_de_avaliacao(limite, config.REAVALIAR_APOS_MINUTOS)]

    def test_jogo_ja_iniciado_nao_gasta_pinnacle(self):
        """Defesa em profundidade: o diff já barra, mas se uma linha antiga
        sobrar no banco ela não pode consumir request."""
        offer = make_offer(mercado="começou", inicio_evento=daqui(-1))
        self.storage.apply_diff(diff_offers({}, [make_offer(mercado="ok")]))
        # Entra por baixo do diff, como uma linha gravada antes desta mudança.
        self.storage._upsert(offer, "2026-01-01T00:00:00-03:00", is_new=True)
        self.storage._conn.commit()
        self.assertEqual(self._fila(), ["ok"])
        self.assertEqual(self.storage.contar_pendentes(config.REAVALIAR_APOS_MINUTOS), 1)

    def test_janela_util_vem_antes_do_jogo_distante(self):
        self._gravar(make_offer(mercado="amanhã", inicio_evento=daqui(30)),
                     make_offer(mercado="hoje", inicio_evento=daqui(2)))
        self.assertEqual(self._fila(), ["hoje", "amanhã"])

    def test_dentro_da_janela_a_mais_urgente_primeiro(self):
        self._gravar(make_offer(mercado="em 8h", inicio_evento=daqui(8)),
                     make_offer(mercado="em 1h", inicio_evento=daqui(1)),
                     make_offer(mercado="em 4h", inicio_evento=daqui(4)))
        self.assertEqual(self._fila(), ["em 1h", "em 4h", "em 8h"])

    def test_reavaliacao_urgente_passa_na_frente_de_inedita_distante(self):
        """O conserto principal. A reavaliação é o momento em que o mercado da
        Pinnacle abriu — mandá-la pro fim da fila era o que fazia o alerta
        sair 19 min antes do jogo."""
        perto = make_offer(mercado="perto", inicio_evento=daqui(2))
        longe = make_offer(mercado="longe", inicio_evento=daqui(30))
        self._gravar(perto, longe)
        # `perto` já foi avaliada, há tempo suficiente pra voltar pra fila.
        self.storage.registrar_avaliacao(perto.offer_id, perto.content_hash,
                                         "sem_odd_justa")
        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = ? WHERE offer_id = ?",
            (daqui(-3), perto.offer_id))
        self.storage._conn.commit()
        self.assertEqual(self._fila(), ["perto", "longe"])

    def test_jogo_distante_reavalia_com_ttl_maior(self):
        """Reavaliar de hora em hora um jogo de amanhã é pagar pra ouvir o
        mesmo "sem mercado" — era isso que entupia a fila."""
        longe = make_offer(mercado="longe", inicio_evento=daqui(30))
        self._gravar(longe)
        self.storage.registrar_avaliacao(longe.offer_id, longe.content_hash,
                                         "sem_odd_justa")
        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = ? WHERE offer_id = ?",
            (daqui(-1.5), longe.offer_id))   # 90 min: passou do TTL curto
        self.storage._conn.commit()
        self.assertEqual(self._fila(), [], "TTL longo ainda não venceu")

        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = ?",
            (daqui(-(config.REAVALIAR_LONGE_MINUTOS / 60 + 1)),))
        self.storage._conn.commit()
        self.assertEqual(self._fila(), ["longe"])

    def test_contar_pendentes_conta_a_mesma_fila_que_roda(self):
        """Se divergirem, o log "N pendentes" mente sobre o backlog."""
        self._gravar(make_offer(mercado="a", inicio_evento=daqui(2)),
                     make_offer(mercado="b", inicio_evento=daqui(30)),
                     make_offer(mercado="c", inicio_evento=daqui(-1)))
        ttl = config.REAVALIAR_APOS_MINUTOS
        self.assertEqual(self.storage.contar_pendentes(ttl), len(self._fila(50)))


class TestLiquidacaoNoBanco(unittest.TestCase):
    """Persistência da liquidação: migração, janela em UTC e uma linha por alerta."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "liq.db")

    def tearDown(self):
        self.storage.close()
        self._tmp.cleanup()

    def _semear(self, *, offer_id="of1", inicio_horas=-4.0,
                mercado="Resultado Final: Flamengo") -> str:
        inicio = to_utc_iso(datetime.now(timezone.utc)
                            + timedelta(hours=inicio_horas))
        offer = make_offer(mercado=mercado, evento_id=offer_id)
        object.__setattr__(offer, "inicio_evento", inicio) if hasattr(
            offer, "__dataclass_fields__") else None
        from betano_superodds.diff import DiffResult
        self.storage.apply_diff(DiffResult(novas=[offer], alteradas=[], expiradas=[]))
        self.storage._conn.execute(
            "UPDATE offers SET inicio_evento = ? WHERE offer_id = ?",
            (inicio, offer.offer_id))
        self.storage._conn.commit()
        return offer.offer_id

    # -- migração ---------------------------------------------------------

    def test_banco_antigo_ganha_as_colunas_e_le_null(self):
        """Alerta gravado antes da migração não pode virar stake inventada."""
        caminho = Path(self._tmp.name) / "antigo.db"
        con = sqlite3.connect(caminho)
        con.execute("""CREATE TABLE alertas_enviados (
            offer_id TEXT NOT NULL, content_hash TEXT NOT NULL, alert_hash TEXT,
            odd_boost REAL, edge_pct REAL, enviado_em TEXT NOT NULL,
            PRIMARY KEY (offer_id, content_hash))""")
        con.execute("INSERT INTO alertas_enviados VALUES"
                    " ('of1','ch1','ah1',5.3,12.0,'2026-08-06T10:00:00-03:00')")
        con.commit()
        con.close()
        with Storage(caminho) as s:
            row = s._conn.execute("SELECT * FROM alertas_enviados").fetchone()
            self.assertIsNone(row["stake_unidades"])
            self.assertIsNone(row["stake_apostavel"])
            self.assertEqual(row["odd_boost"], 5.3)

    # -- janela de 24h, em UTC --------------------------------------------

    def test_janela_de_24h_usa_utc_nas_duas_pontas(self):
        """`fim_partida` é UTC e `enviado_em` é local: comparar com o corte
        local de `resumo_24h` deslocaria 3h de apostas sem erro nenhum."""
        offer_id = self._semear()
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", 10.0, 5.0,
                                      stake_unidades=1.0, stake_apostavel=True)
        agora = datetime.now(timezone.utc)
        dentro = (agora - timedelta(hours=23)).isoformat(timespec="seconds")
        self.storage.registrar_liquidacao(offer_id, resultado="green", motivo=None,
                                          fim_partida=dentro)
        self.assertEqual(len(self.storage.apostas_liquidadas_24h(agora)), 1)

        fora = (agora - timedelta(hours=24, seconds=1)).isoformat(timespec="seconds")
        self.storage.registrar_liquidacao(offer_id, resultado="green", motivo=None,
                                          fim_partida=fora)
        self.assertEqual(self.storage.apostas_liquidadas_24h(agora), [])

    def test_agora_ingenuo_e_lido_como_local_nao_como_utc(self):
        """`talvez_resumo_diario` passa `datetime.now()`, que é local ingênuo.

        Ler isso como UTC deslocaria a janela de 24h em um fuso inteiro (3h
        aqui) e derrubaria apostas da borda, sem erro em lugar nenhum.
        """
        offer_id = self._semear()
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", odd_boost=2.0,
                                      stake_unidades=1.0, stake_apostavel=True)
        agora_utc = datetime.now(timezone.utc)
        # 23h atrás: dentro da janela sob qualquer leitura correta
        self.storage.registrar_liquidacao(
            offer_id, resultado="green", motivo=None,
            fim_partida=(agora_utc - timedelta(hours=23)).isoformat(timespec="seconds"))

        ingenuo = datetime.now()          # local, sem tzinfo — como no loop
        self.assertEqual(len(self.storage.apostas_liquidadas_24h(ingenuo)), 1)
        # e concorda com o aware equivalente
        self.assertEqual(len(self.storage.apostas_liquidadas_24h(ingenuo)),
                         len(self.storage.apostas_liquidadas_24h(agora_utc)))

    def test_uma_linha_por_ALERTA_nao_por_aposta(self):
        """Realerta com odd melhor é outro content_hash, logo outra linha —
        que é a semântica pedida."""
        offer_id = self._semear()
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", odd_boost=10.45,
                                      stake_unidades=1.0, stake_apostavel=True)
        self.storage.registrar_alerta(offer_id, "ch2", "ah2", odd_boost=11.55,
                                      stake_unidades=1.0, stake_apostavel=True)
        agora = datetime.now(timezone.utc)
        self.storage.registrar_liquidacao(
            offer_id, resultado="green", motivo=None,
            fim_partida=(agora - timedelta(hours=1)).isoformat(timespec="seconds"))
        linhas = self.storage.apostas_liquidadas_24h(agora)
        self.assertEqual(len(linhas), 2)
        self.assertEqual({l["odd_boost"] for l in linhas}, {10.45, 11.55})

    def test_recupera_evento_e_mercado_de_oferta_expirada(self):
        """A oferta expira quando o jogo começa; o alerta continua valendo."""
        offer_id = self._semear(mercado="Total de Gols: Mais de 2,5")
        self.storage._conn.execute("UPDATE offers SET active = 0")
        self.storage._conn.commit()
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", 3.0, 5.0,
                                      stake_unidades=1.0, stake_apostavel=True)
        agora = datetime.now(timezone.utc)
        self.storage.registrar_liquidacao(
            offer_id, resultado="red", motivo=None,
            fim_partida=(agora - timedelta(hours=1)).isoformat(timespec="seconds"))
        linha = self.storage.apostas_liquidadas_24h(agora)[0]
        self.assertEqual(linha["mercado"], "Total de Gols: Mais de 2,5")
        self.assertTrue(linha["evento"])

    # -- fila e backoff ----------------------------------------------------

    def test_pendente_so_depois_do_atraso(self):
        offer_id = self._semear(inicio_horas=-4.0)
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", 3.0, 5.0)
        self.assertEqual(len(self.storage.pendentes_de_liquidacao(10, 150)), 1)
        # jogo recém-começado não entra: ainda está rolando
        self.storage._conn.execute(
            "UPDATE offers SET inicio_evento = ?",
            (to_utc_iso(datetime.now(timezone.utc) - timedelta(minutes=30)),))
        self.storage._conn.commit()
        self.assertEqual(self.storage.pendentes_de_liquidacao(10, 150), [])

    def test_ja_liquidada_sai_da_fila(self):
        offer_id = self._semear()
        self.storage.registrar_alerta(offer_id, "ch1", "ah1", 3.0, 5.0)
        self.storage.registrar_liquidacao(
            offer_id, resultado="green", motivo=None,
            fim_partida=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self.assertEqual(self.storage.pendentes_de_liquidacao(10, 150), [])

    def test_backoff_acumula_tentativas_e_segura_a_partida(self):
        agora = datetime.now(timezone.utc)
        n1 = self.storage.registrar_tentativa_evento(
            "flamengo|santos|2026-08-06", "Flamengo - Santos",
            motivo="sem match", backoff_minutos=30, agora=agora)
        n2 = self.storage.registrar_tentativa_evento(
            "flamengo|santos|2026-08-06", "Flamengo - Santos",
            motivo="sem match", backoff_minutos=60, agora=agora)
        self.assertEqual((n1, n2), (1, 2))
        self.assertIn("flamengo|santos|2026-08-06",
                      self.storage.eventos_em_backoff(agora))
        # passado o castigo, volta a ser elegível
        self.assertNotIn("flamengo|santos|2026-08-06",
                         self.storage.eventos_em_backoff(agora + timedelta(hours=2)))

    def test_id_casado_sobrevive_a_uma_falha_posterior(self):
        """COALESCE: uma tentativa que só falhou de rede não pode apagar o
        casamento que já custou requests."""
        chave = "flamengo|santos|2026-08-06"
        self.storage.registrar_tentativa_evento(chave, "Flamengo - Santos",
                                                sofascore_id=999, match_score=95.0)
        self.storage.registrar_tentativa_evento(chave, "Flamengo - Santos",
                                                motivo="resultado indisponível")
        self.assertEqual(self.storage.evento_liquidacao(chave)["sofascore_id"], 999)


if __name__ == "__main__":
    unittest.main(verbosity=2)
