"""Testes de notificação: dedup, resumo diário, teto de alertas e formatação."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from pathlib import Path

from betano_superodds import config
from betano_superodds.alerts import CHAVE_ACUMULADO, CHAVE_RESUMO, Alertador
from betano_superodds.diff import DiffResult, OfferChange
from betano_superodds.models import Offer
from betano_superodds.notifier import LIMITE_TELEGRAM, TelegramNotifier
from betano_superodds.revalidacao import Revalidacao
from betano_superodds.storage import Storage


class NotifierFake(TelegramNotifier):
    """Captura as mensagens em vez de mandar pro Telegram."""

    def __init__(self, ativo: bool = True) -> None:
        super().__init__(token="tok" if ativo else "", chat_id="1" if ativo else "")
        self.enviadas: list[str] = []
        self.falhar = False

    def enviar(self, texto: str) -> bool:
        if not self.ativo or self.falhar:
            return False
        self.enviadas.append(texto)
        return True


def oferta(mercado: str = "Resultado Final: Celtic", odd: float = 1.21,
           evento_id: str = "e1") -> Offer:
    return Offer(fonte="mr12", evento_id=evento_id, evento="Celtic - Dundee FC",
                 mercado=mercado, odd_original=1.19, odd_boost=odd,
                 url="https://betano.bet.br/x", liga="Escócia", boost_pct=1.7)


class BaseAlertas(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.db")
        self.notifier = NotifierFake()
        self.alertador = Alertador(self.storage, self.notifier)

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def resultado_value(self, of: Offer, edge: float = 7.5,
                         familia_mercado: str = "outros") -> dict:
        return {**of.to_row(), "status": "avaliada", "is_value": True,
                "edge_pct": edge, "odd_justa": 1.12, "tipo_mercado": "simples",
                "fonte_odd": "pinnacle", "threshold_usado": 5.0,
                "evento_pinnacle": "Celtic vs Dundee", "match_score": 100.0,
                "familia_mercado": familia_mercado}


class TestDedup(BaseAlertas):
    def test_nao_alerta_a_mesma_oferta_duas_vezes(self):
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertEqual(len(self.notifier.enviadas), 1)

        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertEqual(len(self.notifier.enviadas), 1, "reenviou oferta idêntica")

    def test_edge_melhorando_o_suficiente_gera_alerta_novo(self):
        """Regressão de 2026-08-07: `alert_hash` incluía `odd_boost`, então
        qualquer centavo de variação virava "alerta novo" e a mesma oferta
        floodava o Telegram (4 envios da mesma oferta em ~90 min, edge indo
        de +22.4% a +18.1%). Agora quem decide um segundo envio é o EDGE:
        só libera se melhorar pelo menos `REALERTA_MELHORA_MIN_PP` sobre o
        melhor já alertado — e só depois de `INTERVALO_MIN_REALERTA`
        (backdatado aqui pra isolar esta guarda da guarda de tempo)."""
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=7.5)]))
        self.assertEqual(len(self.notifier.enviadas), 1)

        self.storage._conn.execute(
            "UPDATE alertas_enviados SET enviado_em = '2020-01-01T00:00:00+00:00'")
        edge_2 = 7.5 + config.REALERTA_MELHORA_MIN_PP
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=edge_2)]))
        self.assertEqual(len(self.notifier.enviadas), 2)

    def test_edge_melhorando_pouco_nao_realerta(self):
        """Melhora abaixo de `REALERTA_MELHORA_MIN_PP` não é oportunidade
        nova o bastante — é ruído de preço, mesma oferta."""
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=7.5)]))
        self.storage._conn.execute(
            "UPDATE alertas_enviados SET enviado_em = '2020-01-01T00:00:00+00:00'")

        edge_2 = 7.5 + config.REALERTA_MELHORA_MIN_PP - 0.1
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=edge_2)]))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_piso_de_tempo_bloqueia_mesmo_com_edge_melhorando(self):
        """Edge melhorando o suficiente não pula `INTERVALO_MIN_REALERTA` —
        é piso por cima da guarda de edge, não substituto dela."""
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=7.5)]))
        edge_2 = 7.5 + config.REALERTA_MELHORA_MIN_PP
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=edge_2)]))
        self.assertEqual(len(self.notifier.enviadas), 1, "pulou o piso de tempo")

    def test_teto_de_realertas_bloqueia_mesmo_com_edge_melhorando(self):
        """Depois de `MAX_REALERTAS_POR_OFERTA` envios, bloqueia mesmo com
        melhora de edge legítima repetida — teto por oferta, não só por
        edge, senão uma oferta de vida longa vira flood devagar.

        Cada envio usa uma odd DIFERENTE (`oferta(odd=...)`), igual à
        produção: `alertas_enviados` é chaveada por `content_hash`, que
        muda com a odd — reusar a mesma odd faria os 3 envios upsertar a
        MESMA linha e o teto nunca contaria certo.
        """
        edge = 7.5
        asyncio.run(self.alertador._alertar_values(
            [self.resultado_value(oferta(odd=2.00), edge=edge)]))
        self.storage._conn.execute(
            "UPDATE alertas_enviados SET enviado_em = '2020-01-01T00:00:00+00:00'")

        edge += config.REALERTA_MELHORA_MIN_PP
        asyncio.run(self.alertador._alertar_values(
            [self.resultado_value(oferta(odd=2.20), edge=edge)]))
        self.assertEqual(len(self.notifier.enviadas), config.MAX_REALERTAS_POR_OFERTA)
        self.storage._conn.execute(
            "UPDATE alertas_enviados SET enviado_em = '2020-01-01T00:00:00+00:00'")

        edge += config.REALERTA_MELHORA_MIN_PP
        asyncio.run(self.alertador._alertar_values(
            [self.resultado_value(oferta(odd=2.40), edge=edge)]))
        self.assertEqual(len(self.notifier.enviadas), config.MAX_REALERTAS_POR_OFERTA,
                         "realertou além do teto")

    def test_reavaliacao_nao_realerta_a_mesma_versao(self):
        """Com TTL, a oferta volta pra fila — mas o alerta não pode repetir."""
        of = oferta()
        self.storage.apply_diff(
            DiffResult(novas=[of], alteradas=[], expiradas=[], inalteradas=[]))
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertEqual(len(self.notifier.enviadas), 1)

        # Simula o TTL vencendo e a oferta sendo reavaliada com o mesmo edge.
        self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")
        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(len(self.storage.pendentes_de_avaliacao(50, 60)), 1)

        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertEqual(len(self.notifier.enviadas), 1, "realertou na reavaliação")

    def test_falha_de_envio_nao_marca_como_alertado(self):
        """Se o Telegram caiu, a value bet tem que ser reofertada no próximo ciclo."""
        self.notifier.falhar = True
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertFalse(self.storage.ja_alertou(of.offer_id))

        self.notifier.falhar = False
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of)]))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_sem_telegram_configurado_ainda_deduplica(self):
        """Sem bot, o alerta vai só pro log — mas não pode repetir todo ciclo."""
        alertador = Alertador(self.storage, NotifierFake(ativo=False))
        of = oferta()
        asyncio.run(alertador._alertar_values([self.resultado_value(of)]))
        self.assertTrue(self.storage.ja_alertou(of.offer_id))

    def test_odd_original_oscilando_nao_reenvia_alerta_identico(self):
        """Regressão do alerta duplicado de 2026-08-04.

        `dc32e1e88f69cf16` saiu 12:07 e 12:11 com odd 2.02 e edge 8.46 nos
        dois — mensagem idêntica. A dedup usava `content_hash`, que inclui
        `odd_original`; a odd pré-boost da Betano mexeu sozinha, o hash virou
        outro e o alerta passou. O usuário não vê `odd_original`.
        """
        antes = oferta(odd=2.02)
        antes.odd_original = 1.84
        depois = oferta(odd=2.02)
        depois.odd_original = 1.85          # só o preço pré-boost mudou

        self.assertNotEqual(antes.content_hash, depois.content_hash)
        self.assertEqual(antes.alert_hash, depois.alert_hash)

        asyncio.run(self.alertador._alertar_values([self.resultado_value(antes)]))
        asyncio.run(self.alertador._alertar_values([self.resultado_value(depois)]))
        self.assertEqual(len(self.notifier.enviadas), 1, "reenviou alerta idêntico")

    def test_boost_que_piora_nao_realerta_dentro_da_janela(self):
        """Edge piorando não é oportunidade nova — bloqueia, ponto."""
        of = oferta()
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=9.0)]))
        asyncio.run(self.alertador._alertar_values([self.resultado_value(of, edge=6.0)]))
        self.assertEqual(len(self.notifier.enviadas), 1)


class TestRevalidacao(BaseAlertas):
    """A odd ainda existe na hora de mandar? Ver `revalidacao`."""

    def _com_revalidador(self, rev: Revalidacao) -> Alertador:
        async def revalidador(_row: dict) -> Revalidacao:
            return rev
        return Alertador(self.storage, self.notifier, revalidador)

    def test_odd_confirmada_deixa_o_alerta_passar(self):
        al = self._com_revalidador(Revalidacao("confirmada", 1.21))
        asyncio.run(al._alertar_values([self.resultado_value(oferta())]))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_mercado_que_sumiu_nao_alerta(self):
        """O caso Levski: a casa moveu escanteios de 9.5 pra 9.

        O rótulo deixa de bater, a revalidação devolve "sumiu" e o alerta
        morre. Alertar aqui é mandar o usuário procurar aposta que não existe.
        """
        al = self._com_revalidador(Revalidacao("sumiu", None, "não está mais na casa"))
        asyncio.run(al._alertar_values([self.resultado_value(oferta())]))
        self.assertEqual(self.notifier.enviadas, [])

    def test_odd_que_caiu_nao_alerta(self):
        al = self._com_revalidador(Revalidacao("mudou", 1.70, "2.42 → 1.70"))
        asyncio.run(al._alertar_values([self.resultado_value(oferta())]))
        self.assertEqual(self.notifier.enviadas, [])

    def test_alerta_bloqueado_nao_volta_pra_fila(self):
        """Requeue aqui viraria loop: a odd do banco só muda quando a casa
        for reraspada, então reavaliar antes disso repetiria o mesmo bloqueio
        a cada ciclo. `avaliacoes` é chaveada por `content_hash` — quando a
        odd mudar de verdade, a oferta volta pra fila sozinha."""
        of = oferta()
        self.storage.apply_diff(
            DiffResult(novas=[of], alteradas=[], expiradas=[], inalteradas=[]))
        self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")

        al = self._com_revalidador(Revalidacao("mudou", 1.70))
        asyncio.run(al._alertar_values([self.resultado_value(of)]))
        self.assertEqual(self.storage.pendentes_de_avaliacao(50), [])

    def test_falha_de_rede_nao_engole_a_value_bet(self):
        """Indeterminada não bloqueia: value bet é rara e nem sempre volta."""
        al = self._com_revalidador(
            Revalidacao("indeterminada", None, "timeout"))
        asyncio.run(al._alertar_values([self.resultado_value(oferta())]))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_indeterminada_com_captura_velha_nao_alerta(self):
        """Não deu pra confirmar E o dado está velho — é a combinação que
        produziu o alerta falso das 12:21 (captura das 11:29)."""
        velho = (datetime.now().astimezone()
                 - timedelta(minutes=config.IDADE_MAX_PARA_ALERTA + 10))
        resultado = {**self.resultado_value(oferta()),
                     "last_seen": velho.isoformat(timespec="seconds")}
        al = self._com_revalidador(Revalidacao("indeterminada", None, "timeout"))
        asyncio.run(al._alertar_values([resultado]))
        self.assertEqual(self.notifier.enviadas, [])

    def test_confirmada_alerta_mesmo_com_captura_velha(self):
        """Se a casa confirmou a odd, idade não importa mais."""
        velho = (datetime.now().astimezone()
                 - timedelta(minutes=config.IDADE_MAX_PARA_ALERTA + 10))
        resultado = {**self.resultado_value(oferta()),
                     "last_seen": velho.isoformat(timespec="seconds")}
        al = self._com_revalidador(Revalidacao("confirmada", 1.21))
        asyncio.run(al._alertar_values([resultado]))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_alerta_bloqueado_nao_conta_como_enviado(self):
        """Se bloqueou, a oferta não pode ficar marcada — senão nunca mais sai."""
        of = oferta()
        al = self._com_revalidador(Revalidacao("sumiu", None))
        asyncio.run(al._alertar_values([self.resultado_value(of)]))
        self.assertFalse(self.storage.ja_alertou(of.offer_id))


class TestFilaDeAvaliacao(BaseAlertas):
    """A fila é o que impede o teto por ciclo de virar descarte silencioso."""

    def _semear(self, n: int) -> list[Offer]:
        ofertas = []
        for i in range(n):
            of = oferta(evento_id=f"e{i}")
            of.boost_pct = float(i)
            ofertas.append(of)
        self.storage.apply_diff(
            DiffResult(novas=ofertas, alteradas=[], expiradas=[], inalteradas=[]))
        return ofertas

    def test_teto_por_ciclo_prioriza_maior_boost(self):
        self._semear(config.MAX_AVALIACOES_POR_CICLO + 10)
        lote = self.storage.pendentes_de_avaliacao(config.MAX_AVALIACOES_POR_CICLO)
        self.assertEqual(len(lote), config.MAX_AVALIACOES_POR_CICLO)
        self.assertEqual(lote[0]["boost_pct"],
                         float(config.MAX_AVALIACOES_POR_CICLO + 9))

    def test_captura_recente_passa_na_frente_de_boost_maior(self):
        """O rodízio deixa captura envelhecer até ~1h; edge de odd velha é
        edge sobre preço que a casa já mexeu. Frescor manda mais que boost."""
        velha, nova = oferta(evento_id="velha"), oferta(evento_id="nova")
        velha.boost_pct, nova.boost_pct = 90.0, 1.0
        self.storage.apply_diff(
            DiffResult(novas=[velha, nova], alteradas=[], expiradas=[], inalteradas=[]))
        self.storage._conn.execute(
            "UPDATE offers SET last_seen = '2020-01-01T00:00:00+00:00' "
            " WHERE evento_id = 'velha'")

        lote = self.storage.pendentes_de_avaliacao(10)
        self.assertEqual(lote[0]["evento_id"], "nova")

    def test_o_que_sobra_do_teto_entra_no_ciclo_seguinte(self):
        """O bug que existia: 137 ofertas ficavam órfãs pra sempre."""
        ofertas = self._semear(config.MAX_AVALIACOES_POR_CICLO + 10)
        primeiro = self.storage.pendentes_de_avaliacao(config.MAX_AVALIACOES_POR_CICLO)
        for row in primeiro:
            self.storage.registrar_avaliacao(row["offer_id"], row["content_hash"],
                                             "sem_odd_justa")

        segundo = self.storage.pendentes_de_avaliacao(config.MAX_AVALIACOES_POR_CICLO)
        self.assertEqual(len(segundo), 10, "o resto da fila não voltou")
        vistos = {r["offer_id"] for r in primeiro} | {r["offer_id"] for r in segundo}
        self.assertEqual(len(vistos), len(ofertas), "nem toda oferta foi avaliada")

    def test_avaliada_sai_da_fila(self):
        ofertas = self._semear(3)
        for of in ofertas:
            self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")
        self.assertEqual(self.storage.pendentes_de_avaliacao(50), [])

    def test_odd_alterada_volta_pra_fila(self):
        """content_hash novo = versão não avaliada."""
        of = oferta(odd=1.21)
        self.storage.apply_diff(DiffResult(novas=[of], alteradas=[], expiradas=[],
                                           inalteradas=[]))
        self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")
        self.assertEqual(self.storage.pendentes_de_avaliacao(50), [])

        reajustada = oferta(odd=1.55)
        self.storage.apply_diff(DiffResult(novas=[], expiradas=[], inalteradas=[],
            alteradas=[OfferChange(offer=reajustada, odd_boost_anterior=1.21,
                                   odd_original_anterior=1.19)]))
        self.assertEqual(len(self.storage.pendentes_de_avaliacao(50)), 1)

    def test_ttl_devolve_a_oferta_para_reavaliacao(self):
        """A linha da Pinnacle anda mesmo com a odd da Betano parada."""
        of = self._semear(1)[0]
        self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")
        self.assertEqual(self.storage.pendentes_de_avaliacao(50, 60), [],
                         "reavaliou antes da hora")
        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(len(self.storage.pendentes_de_avaliacao(50, 60)), 1)

    def test_ttl_zero_avalia_cada_versao_uma_vez_so(self):
        of = self._semear(1)[0]
        self.storage.registrar_avaliacao(of.offer_id, of.content_hash, "avaliada")
        self.storage._conn.execute(
            "UPDATE avaliacoes SET avaliado_em = '2020-01-01T00:00:00+00:00'")
        self.assertEqual(self.storage.pendentes_de_avaliacao(50, 0), [])

    def test_oferta_expirada_sai_da_fila(self):
        ofertas = self._semear(2)
        self.storage.apply_diff(DiffResult(
            novas=[], alteradas=[], inalteradas=[],
            expiradas=[{**ofertas[0].to_row(), "offer_id": ofertas[0].offer_id}]))
        pendentes = self.storage.pendentes_de_avaliacao(50)
        self.assertEqual(len(pendentes), 1)

    def test_contagem_bate_com_a_fila(self):
        self._semear(7)
        self.assertEqual(self.storage.contar_pendentes(), 7)


class TestTetoDeAlertas(BaseAlertas):
    def test_teto_de_alertas_por_ciclo_manda_os_maiores_edges(self):
        # Família distinta por item: este teste cobre só o teto GERAL
        # (`MAX_ALERTAS_POR_CICLO`), não o teto por família — esse tem teste
        # próprio logo abaixo.
        values = [self.resultado_value(oferta(evento_id=f"e{i}"), edge=float(i),
                                       familia_mercado=f"fam{i}")
                  for i in range(config.MAX_ALERTAS_POR_CICLO + 5)]
        asyncio.run(self.alertador._alertar_values(values))
        self.assertEqual(len(self.notifier.enviadas), config.MAX_ALERTAS_POR_CICLO)
        # O de maior edge é o primeiro da fila.
        maior = max(v["edge_pct"] for v in values)
        self.assertIn(f"{maior:+.1f}%", self.notifier.enviadas[0])

    def test_teto_por_familia_nao_deixa_uma_familia_engolir_o_ciclo(self):
        # Mais values de "handicap" com edge alto do que o teto por família
        # permite — o resto do ciclo tem que sobrar pra outra família, mesmo
        # com edge menor.
        teto = config.MAX_ALERTAS_POR_FAMILIA_POR_CICLO
        handicaps = [self.resultado_value(oferta(evento_id=f"h{i}"),
                                          edge=100.0 - i, familia_mercado="handicap")
                     for i in range(teto + 3)]
        escanteios = self.resultado_value(oferta(evento_id="corner1"),
                                          edge=1.0, familia_mercado="escanteios")
        asyncio.run(self.alertador._alertar_values(handicaps + [escanteios]))

        enviadas = self.notifier.enviadas
        self.assertEqual(len(enviadas), min(config.MAX_ALERTAS_POR_CICLO, teto + 1))
        n_handicap = sum(1 for h in handicaps
                         if any(f"{h['edge_pct']:+.1f}%" in msg for msg in enviadas))
        self.assertEqual(n_handicap, teto, "teto por família não segurou o handicap")
        # Só os de MAIOR edge dentro da família passam, não os últimos.
        top_handicaps = sorted(handicaps, key=lambda h: h["edge_pct"], reverse=True)[:teto]
        for h in top_handicaps:
            self.assertTrue(any(f"{h['edge_pct']:+.1f}%" in msg for msg in enviadas))
        self.assertTrue(any(f"{escanteios['edge_pct']:+.1f}%" in msg for msg in enviadas),
                        "escanteios de edge menor devia entrar no lugar do handicap excedente")


class TestProcessarCiclo(BaseAlertas):
    """O ciclo puxa da fila — banco quente ou frio, tanto faz."""

    def _semear(self, n: int) -> list[Offer]:
        ofertas = [oferta(evento_id=f"e{i}") for i in range(n)]
        self.storage.apply_diff(
            DiffResult(novas=ofertas, alteradas=[], expiradas=[], inalteradas=[]))
        return ofertas

    def _rodar(self, avaliadas: list[dict]) -> dict:
        """Roda o ciclo trocando a chamada de rede por um retorno fixo."""
        import betano_superodds.alerts as mod
        original = mod.avaliar_ofertas
        # Aceita o `storage` que o alertador passa pra habilitar o consenso
        # entre casas — o dublê ignora, mas a assinatura tem que bater.
        mod.avaliar_ofertas = lambda ofertas, storage=None: avaliadas
        try:
            return asyncio.run(self.alertador.processar_ciclo())
        finally:
            mod.avaliar_ofertas = original

    def test_avalia_ofertas_ativas_e_alerta(self):
        ofertas = self._semear(3)
        ciclo = self._rodar([self.resultado_value(ofertas[0])])
        self.assertEqual(ciclo["values"], 1)
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_e_idempotente_entre_reinicios(self):
        ofertas = self._semear(2)
        self._rodar([self.resultado_value(ofertas[0])])
        self._rodar([self.resultado_value(ofertas[0])])
        self.assertEqual(len(self.notifier.enviadas), 1, "realertou ao reiniciar")

    def test_marca_como_avaliada_mesmo_sem_value(self):
        """Senão a oferta sem cobertura voltaria pra fila em todo ciclo."""
        ofertas = self._semear(1)
        self._rodar([{**ofertas[0].to_row(), "status": "sem_odd_justa",
                      "is_value": None, "motivo": "sem cobertura: aces"}])
        self.assertEqual(self.storage.pendentes_de_avaliacao(50, 0), [])

    def test_sem_ofertas_ativas_nao_faz_rede(self):
        def explodir(_ofertas, _storage=None):
            raise AssertionError("não devia ter chamado a Pinnacle")
        import betano_superodds.alerts as mod
        original = mod.avaliar_ofertas
        mod.avaliar_ofertas = explodir
        try:
            ciclo = asyncio.run(self.alertador.processar_ciclo())
        finally:
            mod.avaliar_ofertas = original
        self.assertEqual(ciclo["avaliadas"], 0)


def _no_alvo(ano, mes, dia, *, mais_minutos=0):
    """Um instante exatamente no horário configurado do resumo (+ offset)."""
    return (datetime(ano, mes, dia, config.RESUMO_DIARIO_HORA,
                     config.RESUMO_DIARIO_MINUTO)
            + timedelta(minutes=mais_minutos))


class TestResumoDiario(BaseAlertas):
    def test_nao_manda_antes_da_hora(self):
        antes = _no_alvo(2026, 8, 3, mais_minutos=-60)
        self.assertFalse(self.alertador.talvez_resumo_diario(antes))
        self.assertEqual(self.notifier.enviadas, [])

    def test_manda_uma_vez_so_por_dia(self):
        agora = _no_alvo(2026, 8, 3, mais_minutos=1)
        self.assertTrue(self.alertador.talvez_resumo_diario(agora))
        self.assertFalse(self.alertador.talvez_resumo_diario(agora))
        self.assertEqual(len(self.notifier.enviadas), 1)

    def test_volta_a_mandar_no_dia_seguinte(self):
        self.alertador.talvez_resumo_diario(_no_alvo(2026, 8, 3))
        self.alertador.talvez_resumo_diario(_no_alvo(2026, 8, 4))
        self.assertEqual(len(self.notifier.enviadas), 2)


class TestResumoAtravessandoMeiaNoite(BaseAlertas):
    """O alvo 23:59 deixa só 60s até o dia virar, e as checagens acontecem a
    cada ~4,6 min (ciclo de ~100s + POLL_INTERVAL). Na maioria dos dias a
    primeira checagem elegível cai já em 00:0x — é isso que
    `RESUMO_TOLERANCIA_MINUTOS` e a atribuição por dia-de-referência resolvem.

    O alvo é fixado em 23:59 aqui de propósito: estes casos não existem com
    alvo no meio do dia, então herdar o config do ambiente testaria outra coisa.
    """

    def setUp(self):
        super().setUp()
        for atributo, valor in (("RESUMO_DIARIO_HORA", 23),
                                ("RESUMO_DIARIO_MINUTO", 59)):
            p = patch.object(config, atributo, valor)
            p.start()
            self.addCleanup(p.stop)

    def test_atrasado_apos_a_meia_noite_fecha_o_dia_anterior(self):
        # 00:04 do dia 4 — o relatório pendente é o do dia 3
        self.assertTrue(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 0, 4)))
        self.assertEqual(self.storage.get_estado(CHAVE_RESUMO), "2026-08-03")

    def test_nao_pula_o_dia_seguinte(self):
        """O bug que a atribuição por dia-de-referência evita: gravar o resumo
        do dia 3 sob a data do dia 4 faria o relatório do dia 4 ser pulado."""
        self.assertTrue(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 0, 4)))          # fecha o dia 3
        self.assertFalse(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 0, 9)))          # não repete
        self.assertTrue(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 23, 59)))        # dia 4 sai normalmente
        self.assertEqual(len(self.notifier.enviadas), 2)

    def test_fora_da_tolerancia_nao_dispara_resumo_de_ontem(self):
        """Senão subir o bot ao meio-dia cuspiria o resumo do dia anterior."""
        self.assertFalse(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 12, 0)))
        self.assertEqual(self.notifier.enviadas, [])

    def test_acumulado_do_dia_que_fechou_entra_no_resumo_atrasado(self):
        """Às 00:0x o acumulado traz o dia 3 — comparar com a data do relógio
        faria o relatório sair sem avaliadas/values e sem erro nenhum."""
        self.storage.set_estado(CHAVE_ACUMULADO, json.dumps(
            {"dia": "2026-08-03", "avaliadas": 42, "values": 3,
             "melhor_edge": 12.5}))
        self.assertTrue(self.alertador.talvez_resumo_diario(
            datetime(2026, 8, 4, 0, 4)))
        self.assertIn("42", self.notifier.enviadas[-1])


class TestAcumuladoDiario(BaseAlertas):
    def test_acumulado_soma_ciclos_e_guarda_o_melhor_edge(self):
        self.alertador.acumular({"avaliadas": 3, "values": 0, "melhor_edge": -4.0})
        self.alertador.acumular({"avaliadas": 2, "values": 1, "melhor_edge": 6.5})
        self.alertador.acumular({"avaliadas": 1, "values": 0, "melhor_edge": -9.0})
        acc = json.loads(self.storage.get_estado(CHAVE_ACUMULADO))
        self.assertEqual((acc["avaliadas"], acc["values"], acc["melhor_edge"]), (6, 1, 6.5))

    def test_acumulado_reseta_quando_vira_o_dia(self):
        self.storage.set_estado(CHAVE_ACUMULADO, json.dumps(
            {"dia": "1999-01-01", "avaliadas": 99, "values": 9, "melhor_edge": 50.0}))
        self.alertador.acumular({"avaliadas": 1, "values": 0, "melhor_edge": None})
        acc = json.loads(self.storage.get_estado(CHAVE_ACUMULADO))
        self.assertEqual(acc["avaliadas"], 1)

    def test_acumulado_corrompido_nao_derruba(self):
        self.storage.set_estado(CHAVE_ACUMULADO, "{isso não é json")
        self.alertador.acumular({"avaliadas": 2, "values": 0, "melhor_edge": None})
        self.assertEqual(json.loads(self.storage.get_estado(CHAVE_ACUMULADO))["avaliadas"], 2)

    def test_resumo_marca_enviado_mesmo_se_o_telegram_falhar(self):
        """Senão o loop tentaria reenviar a cada ciclo até a rede voltar."""
        self.notifier.falhar = True
        self.alertador.talvez_resumo_diario(_no_alvo(2026, 8, 3, mais_minutos=1))
        self.assertEqual(self.storage.get_estado(CHAVE_RESUMO), "2026-08-03")


class TestApostasDoDia(unittest.TestCase):
    """A seção de apostas liquidadas do resumo diário."""

    @staticmethod
    def aposta(**kw):
        base = {"evento": "Corinthians - Internacional",
                "mercado": "Resultado Final: Corinthians", "odd_boost": 2.50,
                "stake_unidades": 1.0, "stake_apostavel": 1, "resultado": "green"}
        return {**base, **kw}

    def test_sem_aposta_nao_manda_secao_nenhuma(self):
        """Melhor não mandar nada que mandar uma seção vazia todo dia."""
        self.assertEqual(TelegramNotifier.formatar_apostas_do_dia([]), [])

    def test_traz_evento_mercado_odd_e_unidades(self):
        (msg,) = TelegramNotifier.formatar_apostas_do_dia([self.aposta()])
        self.assertIn("Corinthians - Internacional", msg)
        self.assertIn("Resultado Final: Corinthians", msg)
        self.assertIn("2.50", msg)
        self.assertIn("+1.50un", msg)   # 1un x (2.50 - 1)

    def test_red_desconta_a_stake(self):
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(resultado="red")])
        self.assertIn("-1.00un", msg)

    def test_nao_apurada_nao_entra_no_saldo(self):
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(resultado="desconhecido")])
        self.assertIn("não apurada", msg)
        self.assertIn("Saldo: <b>+0.00un</b>", msg)

    def test_anulada_nao_entra_no_saldo(self):
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(resultado="void")])
        self.assertIn("anulada", msg)
        self.assertIn("Saldo: <b>+0.00un</b>", msg)

    def test_sem_stake_gravada_aparece_mas_fica_fora_do_saldo(self):
        """Alerta anterior à migração: inventar a stake contradiria "o
        relatório é registro do que foi recomendado"."""
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(stake_unidades=None, stake_apostavel=None)])
        self.assertIn("Corinthians", msg)
        self.assertIn("sem stake registrada", msg)
        self.assertIn("Saldo: <b>+0.00un</b>", msg)

    def test_stake_nao_apostavel_conta_zero(self):
        """O alerta saiu dizendo "não vale a pena" — creditar fabricaria
        histórico de aposta que o bot desaconselhou."""
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(stake_unidades=0.1, stake_apostavel=0)])
        self.assertIn("Saldo: <b>+0.00un</b>", msg)

    # -- o fatiador --------------------------------------------------------

    def test_volume_real_vira_varias_mensagens_sem_partir_bloco(self):
        """~40 apostas/dia x ~130 chars estoura o limite do Telegram. O corte
        tem que cair ENTRE blocos: partir uma tag faz a API recusar a mensagem
        inteira com 400."""
        apostas = [self.aposta(evento=f"Time {i} - Adversário {i}")
                   for i in range(45)]
        partes = TelegramNotifier.formatar_apostas_do_dia(apostas)
        self.assertGreater(len(partes), 1)
        for parte in partes:
            self.assertLess(len(parte), LIMITE_TELEGRAM)
            self.assertEqual(parte.count("<b>"), parte.count("</b>"))
            self.assertEqual(parte.count("<i>"), parte.count("</i>"))
        # nenhuma aposta perdida nem duplicada no corte
        self.assertEqual(sum(p.count("⚽") for p in partes), 45)

    def test_mercado_gigante_nao_estoura_a_mensagem(self):
        """Caso patológico: o corte é no DADO, antes de montar o HTML."""
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(mercado="Gol " * 2000)])
        self.assertLess(len(msg), LIMITE_TELEGRAM)
        self.assertEqual(msg.count("<b>"), msg.count("</b>"))

    def test_escapa_html_do_evento_e_do_mercado(self):
        (msg,) = TelegramNotifier.formatar_apostas_do_dia(
            [self.aposta(evento="A & <b>B</b>", mercado="x < y")])
        self.assertIn("&amp;", msg)
        self.assertIn("&lt;", msg)
        self.assertEqual(msg.count("<b>"), msg.count("</b>"))

    def test_saldo_soma_greens_e_reds(self):
        partes = TelegramNotifier.formatar_apostas_do_dia([
            self.aposta(odd_boost=3.0, stake_unidades=1.0),            # +2.00
            self.aposta(odd_boost=2.0, stake_unidades=2.0, resultado="red"),  # -2.00
            self.aposta(odd_boost=5.0, stake_unidades=0.5),            # +2.00
        ])
        self.assertIn("Saldo: <b>+2.00un</b>", partes[-1])
        self.assertIn("✅ 2", partes[-1])
        self.assertIn("❌ 1", partes[-1])


class TestResumoEnviaAsApostas(BaseAlertas):
    def test_apostas_saem_junto_do_resumo(self):
        self.storage.registrar_alerta("of1", "ch1", "ah1", odd_boost=2.5,
                                      stake_unidades=1.0, stake_apostavel=True)
        agora = datetime.now(timezone.utc)
        self.storage.registrar_liquidacao(
            "of1", resultado="green", motivo=None,
            fim_partida=(agora - timedelta(hours=1)).isoformat(timespec="seconds"))
        self.alertador.talvez_resumo_diario(_no_alvo(2026, 8, 3, mais_minutos=1))
        self.assertGreaterEqual(len(self.notifier.enviadas), 1)

    def test_falha_nas_apostas_nao_derruba_o_heartbeat(self):
        """O resumo é o sinal de "estou vivo" e não pode depender de uma seção
        acessória — nem deixar de marcar o dia como enviado."""
        def explode(*a, **k):
            raise RuntimeError("banco pifou")
        self.storage.apostas_liquidadas_24h = explode

        agora = _no_alvo(2026, 8, 3, mais_minutos=1)
        self.assertTrue(self.alertador.talvez_resumo_diario(agora))
        self.assertEqual(len(self.notifier.enviadas), 1)   # o heartbeat saiu
        self.assertEqual(self.storage.get_estado(CHAVE_RESUMO), "2026-08-03")


class TestFormatacao(unittest.TestCase):
    def test_alerta_de_value_traz_edge_e_odds(self):
        """Checa o CONTEÚDO, não a decoração — o layout é livre pra mudar."""
        of = oferta()
        texto = TelegramNotifier.formatar_value(
            {**of.to_row(), "odd_justa": 1.12, "edge_pct": 8.04, "tipo_mercado": "simples",
             "fonte_odd": "pinnacle", "threshold_usado": 5.0})
        self.assertIn(of.evento, texto)
        self.assertIn(of.mercado, texto)
        self.assertIn("+8.0%", texto)   # edge
        self.assertIn("1.21", texto)    # odd boost
        self.assertIn("1.12", texto)    # odd justa

    def test_marca_linha_interpolada(self):
        base = {**oferta().to_row(), "odd_justa": 1.12, "edge_pct": 8.0,
                "tipo_mercado": "simples", "fonte_odd": "pinnacle", "threshold_usado": 5.0}
        self.assertNotIn("interpolada", TelegramNotifier.formatar_value(base))
        self.assertIn("interpolada",
                      TelegramNotifier.formatar_value({**base, "interpolada": True}))

    def _base(self, **extra) -> dict:
        return {**oferta().to_row(), "odd_justa": 1.12, "edge_pct": 8.0,
                "tipo_mercado": "simples", "fonte_odd": "pinnacle",
                "threshold_usado": 5.0, **extra}

    def test_alerta_de_consenso_diz_que_nao_e_pinnacle(self):
        """O rodapé dizia "pinnacle" fixo. Um alerta precificado pelas casas se
        anunciando como preço sharp é a confusão exata que o pipeline inteiro
        trabalha pra evitar."""
        texto = TelegramNotifier.formatar_value(
            self._base(fonte_odd="consenso", n_casas_consenso=4,
                       n_precos_consenso=3, threshold_usado=20.0))
        self.assertIn("consenso de 4 casas", texto)
        self.assertNotIn("de-vig · pinnacle", texto)
        self.assertIn("⚠️", texto)
        self.assertIn("Pinnacle", texto, "o aviso tem que explicar o que falta")

    def test_avisa_quando_as_casas_sao_o_mesmo_feed(self):
        """90.3% das seleções cotadas por 2+ casas Altenar têm preço idêntico.
        "4 casas" com um preço só passaria por concordância independente."""
        um_preco = TelegramNotifier.formatar_value(
            self._base(fonte_odd="consenso", n_casas_consenso=4,
                       n_precos_consenso=1, threshold_usado=20.0))
        self.assertIn("mesmo preço", um_preco)
        self.assertIn("feed só", um_preco)

        varios = TelegramNotifier.formatar_value(
            self._base(fonte_odd="consenso", n_casas_consenso=4,
                       n_precos_consenso=3, threshold_usado=20.0))
        self.assertNotIn("mesmo preço", varios)

    def test_alerta_normal_nao_ganha_selo_de_suspeita(self):
        texto = TelegramNotifier.formatar_value(self._base())
        self.assertIn("de-vig · pinnacle", texto)
        self.assertNotIn("alto demais", texto)

    def test_edge_absurdo_sai_marcado_e_nao_suprimido(self):
        """Política declarada pelo usuário: se parece bug, manda mesmo assim —
        marcado. Alerta que some não dá chance de julgar."""
        texto = TelegramNotifier.formatar_value(self._base(edge_pct=782.2))
        self.assertIn("+782", texto, "o alerta tem que sair")
        self.assertIn("alto demais", texto)

    def test_escapa_html_do_nome_do_evento(self):
        of = oferta()
        of.evento = "A <b>x</b> & B"
        texto = TelegramNotifier.formatar_value(
            {**of.to_row(), "odd_justa": 1.1, "edge_pct": 5.0, "tipo_mercado": "simples",
             "fonte_odd": "pinnacle", "threshold_usado": 5.0})
        self.assertIn("&lt;b&gt;", texto)
        self.assertIn("&amp;", texto)

    def test_mensagem_longa_e_truncada(self):
        notifier = NotifierFake()
        self.assertTrue(notifier.enviar("x" * 50_000))
        self.assertLessEqual(len(notifier.enviadas[0]), 50_000)

    def test_notifier_sem_credencial_fica_inerte(self):
        vazio = TelegramNotifier(token="", chat_id="")
        self.assertFalse(vazio.ativo)
        self.assertFalse(vazio.enviar("oi"))

    def test_combo_multi_jogo_mostra_o_jogo_de_cada_perna(self):
        """Regressão do "Festival de Gols" (Novibet, 2026-08-07): 3 pernas
        "Total de Gols: Mais de 2,5" de 3 jogos diferentes saíam idênticas
        no card, sem nada dizendo que eram jogos distintos. Com
        `matches_por_jogo` presente, o THE BET quebra por perna e o rodapé
        lista um match por jogo, não só o do primeiro."""
        texto = TelegramNotifier.formatar_value(self._base(
            mercado=("[FC Cincinnati - Pumas UNAM] Total de Gols: Mais de 2,5 + "
                    "[Tigres UANL - Minnesota United FC] Total de Gols: Mais de 2,5"),
            matches_por_jogo=[
                {"jogo": "FC Cincinnati - Pumas UNAM",
                 "evento_pinnacle": "FC Cincinnati vs Pumas UNAM", "match_score": 100.0},
                {"jogo": "Tigres UANL - Minnesota United FC",
                 "evento_pinnacle": "Tigres UANL vs Minnesota United", "match_score": 92.3},
            ],
        ))
        self.assertNotIn("[FC Cincinnati", texto, "colchete cru não pode vazar pro card")
        self.assertIn("FC Cincinnati - Pumas UNAM", texto)
        self.assertIn("Tigres UANL - Minnesota United FC", texto)
        # Um link por jogo, não só o do primeiro — senão o 100.0 do primeiro
        # jogo passaria a impressão de que as duas pernas casaram perfeito.
        self.assertIn("FC Cincinnati vs Pumas UNAM", texto)
        self.assertIn("Tigres UANL vs Minnesota United", texto)
        self.assertIn("92.3", texto)

    def test_oferta_de_jogo_unico_nao_ganha_bloco_multi_jogo(self):
        """Não-regressão: sem `matches_por_jogo`, o card sai byte a byte
        igual ao de sempre."""
        texto = TelegramNotifier.formatar_value(self._base())
        self.assertNotIn("→", texto)

    def test_rebaixamento_do_sofascore_aparece_no_card(self):
        """Até 2026-08-07 o rebaixamento de confiança do SofaScore só
        existia no log — quem lia o Telegram não tinha como saber que o
        stake mostrado foi calculado com a confiança ANTERIOR."""
        texto = TelegramNotifier.formatar_value(self._base(
            confianca="média", confianca_original="média-alta",
            stats_sofascore={"flag_noticia_fresca": True,
                             "desfalque_recente": ["Fulano (Time A) — Knee Injury"]},
        ))
        self.assertIn("sofascore", texto.lower())
        self.assertIn("média-alta", texto)
        self.assertIn("média", texto)
        self.assertIn("Fulano (Time A)", texto)
        self.assertIn("ANTERIOR", texto)

    def test_sem_rebaixamento_nao_ganha_linha_de_sofascore(self):
        texto = TelegramNotifier.formatar_value(self._base())
        self.assertNotIn("sofascore", texto.lower())


class TestAntecedencia(BaseAlertas):
    """A dor que originou tudo: alerta chegando com o jogo começando.

    Medido em 50 alertas: 7 saíram DEPOIS do apito inicial (até -15,8 min) e
    outros 2 com ~19 min. O diff e a fila barram antes, mas entre a avaliação e
    o envio há rede (Pinnacle + revalidação) — este é o último portão.
    """

    def _com_inicio(self, horas: float, **extra) -> dict:
        of = oferta(**extra)
        of.inicio_evento = (datetime.now(timezone.utc)
                            + timedelta(hours=horas)).isoformat(timespec="seconds")
        return self.resultado_value(of)

    def test_jogo_ja_comecou_nao_vira_mensagem(self):
        asyncio.run(self.alertador._alertar_values([self._com_inicio(-0.2)]))
        self.assertEqual(self.notifier.enviadas, [])

    def test_alerta_apertado_ainda_sai_mas_marcado(self):
        """Decisão de projeto: em cima da hora é aviso, não descarte."""
        asyncio.run(self.alertador._alertar_values([self._com_inicio(0.12)]))  # ~7 min
        self.assertEqual(len(self.notifier.enviadas), 1)
        texto = self.notifier.enviadas[0]
        self.assertIn("EM CIMA DA HORA", texto)
        self.assertIn("7 min", texto)

    def test_alerta_com_folga_nao_leva_o_aviso(self):
        asyncio.run(self.alertador._alertar_values([self._com_inicio(3)]))
        texto = self.notifier.enviadas[0]
        self.assertNotIn("EM CIMA DA HORA", texto)
        self.assertIn("Começa em", texto)
        self.assertIn("3h00", texto)

    def test_countdown_aparece_no_card(self):
        texto = TelegramNotifier.formatar_value(self._com_inicio(2.5))
        self.assertIn("2h30", texto)

    def test_oferta_sem_horario_nao_quebra_a_formatacao(self):
        """Casa que não publica horário continua alertando, sem countdown."""
        texto = TelegramNotifier.formatar_value(self.resultado_value(oferta()))
        self.assertNotIn("Começa em", texto)
        self.assertIn("Celtic - Dundee FC", texto)


class TestMensagensSaoValidasParaOTelegram(unittest.TestCase):
    """As amostras de `testar_mensagens.py` são o contrato com a API.

    O Telegram recusa a mensagem INTEIRA (400, "can't parse entities") se o
    HTML estiver desbalanceado — verificado contra a API real. Um `<b>` sem
    fechar num nome de time transformaria o alerta em silêncio.
    """

    def _amostras(self):
        return TelegramNotifier.amostras_exemplo()

    def test_ha_amostra_de_cada_tipo(self):
        nomes = [n for n, _ in self._amostras()]
        self.assertEqual(len(nomes), len(set(nomes)), "nome de amostra duplicado")
        for esperado in ("value", "resumo", "quebra", "apostas"):
            self.assertTrue(any(esperado in n for n in nomes), f"falta amostra de {esperado}")

    def test_tags_html_balanceadas(self):
        for nome, texto in self._amostras():
            for tag in ("b", "i", "code"):
                self.assertEqual(
                    texto.count(f"<{tag}>"), texto.count(f"</{tag}>"),
                    f"{nome}: <{tag}> desbalanceada")

    def test_nenhuma_amostra_estoura_o_limite(self):
        from betano_superodds.notifier import LIMITE_TELEGRAM
        for nome, texto in self._amostras():
            self.assertLess(len(texto), LIMITE_TELEGRAM, f"{nome} passou do limite")

    def test_texto_livre_e_escapado(self):
        """Nome de time com & ou < não pode chegar cru ao parser do Telegram."""
        amostra = dict(self._amostras())["value-com-caractere-especial"]
        self.assertIn("&amp;", amostra)
        self.assertIn("&lt;Co&gt;", amostra)
        self.assertNotIn("<Co>", amostra)

    def test_truncamento_respeita_o_limite(self):
        """Verificado contra a API real: acima de 4096 o Telegram devolve 400."""
        from betano_superodds.notifier import LIMITE_TELEGRAM, truncar
        cortado = truncar("x" * (LIMITE_TELEGRAM * 3))
        self.assertLessEqual(len(cortado), LIMITE_TELEGRAM)
        self.assertTrue(cortado.endswith("[…truncado]"))

    def test_texto_curto_passa_intacto(self):
        from betano_superodds.notifier import truncar
        self.assertEqual(truncar("mensagem curta"), "mensagem curta")


if __name__ == "__main__":
    unittest.main(verbosity=2)
