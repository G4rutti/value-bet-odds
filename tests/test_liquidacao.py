"""Testes da liquidação — a aposta deu green ou red?

Zero rede: `ResultadoPartida` é montado direto, e o casamento com o SofaScore
usa um fake com a mesma superfície do `SofaScoreClient`.

O que estes testes protegem, acima de tudo, é a assimetria: dizer `red` sem
certeza inventa uma derrota no histórico do dono, e dizer `green` sem certeza
inventa lucro. `desconhecido` é sempre uma resposta aceitável; chute não é.
"""

from __future__ import annotations

import unittest

from betano_superodds.value import liquidacao as L
from betano_superodds.value.liquidacao import (
    DESCONHECIDO,
    GREEN,
    Intervalo,
    RED,
    VOID,
    ResultadoPartida,
    liquidar_aposta,
)


def partida(*, gols=(2, 1), ht=(1, 0), status="finished",
            home="Corinthians", away="Internacional", **stats) -> ResultadoPartida:
    """Partida de teste. `stats` são pares (casa, fora) por nome interno.

    Estatística NÃO passada fica AUSENTE (não zero) — que é justamente o caso
    que a maior parte destes testes exercita.
    """
    tabela = {}
    for nome, par in stats.items():
        tabela[nome] = (Intervalo.exatamente(par[0]), Intervalo.exatamente(par[1]))
    gols_dict = {}
    if gols is not None:
        gols_dict[L.PERIODO_JOGO] = gols
    if ht is not None:
        gols_dict[L.PERIODO_1T] = ht
    return ResultadoPartida(status=status, home_nome=home, away_nome=away,
                            gols=gols_dict, stats={L.PERIODO_JOGO: tabela})


class TestIntervalo(unittest.TestCase):
    def test_exato_decide_tudo(self):
        i = Intervalo.exatamente(5)
        self.assertTrue(i.acima_de(4.5))
        self.assertFalse(i.acima_de(5.5))
        self.assertTrue(i.abaixo_de(5.5))
        self.assertTrue(i.igual_a(5))

    def test_sem_teto_decide_o_que_da_e_recusa_o_resto(self):
        i = Intervalo(5, float("inf"))
        self.assertTrue(i.acima_de(4.5))     # já passou, o desconhecido não muda
        self.assertFalse(i.abaixo_de(4.5))   # mínimo já estourou
        self.assertIsNone(i.abaixo_de(6.5))  # depende do desconhecido
        self.assertIsNone(i.igual_a(7))

    def test_comparacao_entre_lados(self):
        self.assertTrue(Intervalo.exatamente(9).maior_que(Intervalo.exatamente(1)))
        self.assertIsNone(
            Intervalo(2, float("inf")).maior_que(Intervalo(3, float("inf"))))


class TestMercadosDePlacar(unittest.TestCase):
    """Corinthians 2x1 Internacional, 1º tempo 1x0."""

    def liquidar(self, texto, **kw):
        return liquidar_aposta(texto, partida(**kw)).resultado

    def test_resultado_final(self):
        self.assertEqual(self.liquidar("Resultado Final: Corinthians"), GREEN)
        self.assertEqual(self.liquidar("Resultado Final: Empate"), RED)
        self.assertEqual(self.liquidar("Resultado Final: Internacional"), RED)

    def test_total_de_gols(self):
        self.assertEqual(self.liquidar("Total de Gols: Mais de 2,5"), GREEN)
        self.assertEqual(self.liquidar("Total de Gols: Mais de 3,5"), RED)
        self.assertEqual(self.liquidar("Total de Gols: Menos de 2,5"), RED)

    def test_ambas_marcam(self):
        self.assertEqual(self.liquidar("Ambas equipes Marcam: Sim"), GREEN)
        self.assertEqual(self.liquidar("Ambas equipes Marcam: Sim",
                                       gols=(2, 0), ht=(1, 0)), RED)

    def test_total_de_equipe(self):
        self.assertEqual(self.liquidar("Corinthians Total de gols: Mais de 1.5"), GREEN)
        self.assertEqual(self.liquidar("Internacional Total de gols: Mais de 1.5"), RED)

    def test_chance_dupla(self):
        self.assertEqual(self.liquidar("Chance dupla: Corinthians ou empate"), GREEN)
        self.assertEqual(self.liquidar("Chance dupla: Internacional ou empate"), RED)

    def test_intervalo_final(self):
        self.assertEqual(
            self.liquidar("Intervalo/final do jogo: Corinthians/Corinthians"), GREEN)
        self.assertEqual(
            self.liquidar("Intervalo/final do jogo: Internacional/Corinthians"), RED)

    def test_1x2_com_ambas_marcam(self):
        self.assertEqual(
            self.liquidar("1x2 e ambas equipes marcam: Corinthians e sim"), GREEN)
        self.assertEqual(
            self.liquidar("1x2 e ambas equipes marcam: Corinthians e não"), RED)

    def test_primeiro_tempo_usa_o_placar_do_primeiro_tempo(self):
        # 1º tempo foi 1x0: over 0.5 bate, over 1.5 não.
        self.assertEqual(self.liquidar("1º tempo - Total de gols: Mais de 0.5"), GREEN)
        self.assertEqual(self.liquidar("1º tempo - Total de gols: Mais de 1.5"), RED)


class TestMercadosDeEstatistica(unittest.TestCase):
    def test_escanteios(self):
        p = partida(escanteios=(9, 1))   # total 10
        self.assertEqual(liquidar_aposta("Total de Escanteios: Mais de 9,5", p).resultado,
                         GREEN)
        self.assertEqual(liquidar_aposta("Total de Escanteios: Menos de 9,5", p).resultado,
                         RED)

    def test_1x2_de_escanteios(self):
        p = partida(escanteios=(9, 1))
        self.assertEqual(liquidar_aposta("Escanteios 1x2: Corinthians", p).resultado,
                         GREEN)
        self.assertEqual(liquidar_aposta("Escanteios 1x2: Internacional", p).resultado,
                         RED)


class TestEstatisticaAusenteNuncaEZero(unittest.TestCase):
    """O caso que mais importa: o SofaScore OMITE estatística zerada.

    Ler ausência como 0 inventaria green (e red). A resposta certa é
    `desconhecido`.
    """

    def test_escanteio_nao_reportado_nao_vira_red(self):
        p = partida()  # sem estatística nenhuma
        v = liquidar_aposta("Total de Escanteios: Mais de 9,5", p)
        self.assertEqual(v.resultado, DESCONHECIDO)
        self.assertNotEqual(v.resultado, RED)
        self.assertIn("escanteios", (v.motivo or "").lower())

    def test_escanteio_nao_reportado_nao_vira_green(self):
        p = partida()
        self.assertEqual(
            liquidar_aposta("Total de Escanteios: Menos de 9,5", p).resultado,
            DESCONHECIDO)


class TestCartoesSemVermelho(unittest.TestCase):
    """`/statistics` não traz "Red cards" quando não houve vermelho.

    Com 5 amarelos o total é `[5, inf)`: decide over 4.5 e under 4.5, e se
    RECUSA a decidir under 6.5 — que dependeria do vermelho desconhecido.
    """

    def setUp(self):
        self.p = partida(cartoes_amarelos=(2, 3))   # 5 amarelos, vermelho ausente

    def test_over_abaixo_dos_amarelos_resolve(self):
        self.assertEqual(
            liquidar_aposta("Total de Cartões: Mais de 4.5", self.p).resultado, GREEN)

    def test_under_abaixo_dos_amarelos_resolve(self):
        self.assertEqual(
            liquidar_aposta("Total de Cartões: Menos de 4.5", self.p).resultado, RED)

    def test_under_acima_dos_amarelos_fica_desconhecido(self):
        self.assertEqual(
            liquidar_aposta("Total de Cartões: Menos de 6.5", self.p).resultado,
            DESCONHECIDO)

    def test_com_vermelho_reportado_o_under_passa_a_resolver(self):
        p = partida(cartoes_amarelos=(2, 3), cartoes_vermelhos=(0, 0))
        self.assertEqual(
            liquidar_aposta("Total de Cartões: Menos de 6.5", p).resultado, GREEN)


class TestTemposDoJogo(unittest.TestCase):
    """Dois mercados diferentes que a redação da casa faz parecer um só."""

    def test_marcar_em_ambos_os_tempos(self):
        # Corinthians 2x1, HT 1x0 -> marcou 1 no 1T e 1 no 2T
        self.assertEqual(liquidar_aposta(
            "Corinthians para marcar em ambos os tempos: Sim", partida()).resultado,
            GREEN)
        # Internacional só marcou no 2T
        self.assertEqual(liquidar_aposta(
            "Internacional para marcar em ambos os tempos: Sim", partida()).resultado,
            RED)

    def test_vencer_um_dos_tempos_e_outro_mercado(self):
        """Não é o mesmo que marcar nos dois — e é mais provável.

        Corinthians venceu o 1º tempo (1x0) e empatou o 2º (1x1).
        """
        self.assertEqual(liquidar_aposta(
            "Corinthians para vencer um dos tempos: Sim", partida()).resultado, GREEN)
        self.assertEqual(liquidar_aposta(
            "Internacional para vencer um dos tempos: Sim", partida()).resultado, RED)

    def test_aceita_a_grafia_uma_dos_tempos(self):
        """A casa escreve "um" na 1ª perna do combo e "uma" na 2ª — 6 de 13
        ofertas reais desta família usam "uma"."""
        self.assertEqual(liquidar_aposta(
            "Corinthians para vencer uma dos tempos: Sim", partida()).resultado, GREEN)

    def test_ambos_os_tempos_com_linha(self):
        # 1T: 1 gol, 2T: 1 gol -> ambos acima de 0.5
        self.assertEqual(liquidar_aposta(
            "Ambos os tempos mais de 0.5: Sim", partida()).resultado, GREEN)
        # 1T sem gol
        self.assertEqual(liquidar_aposta(
            "Ambos os tempos mais de 0.5: Sim",
            partida(gols=(2, 0), ht=(0, 0))).resultado, RED)

    def test_segundo_tempo_ambas_marcam(self):
        """O market_parser recusa 2º tempo (a Pinnacle não publica), mas
        liquidar é só subtrair: 2x1 menos 1x0 = 1x1, ambas marcaram."""
        self.assertEqual(liquidar_aposta(
            "2º tempo - ambas equipes marcam: Sim", partida()).resultado, GREEN)


class TestComboKleene(unittest.TestCase):
    RESOLVE = "Total de Gols: Mais de 2,5"          # green nesta partida
    PERDE = "Total de Gols: Mais de 3,5"            # red
    INDEFINIDA = "Total de Escanteios: Mais de 9,5"  # sem estatística

    def test_todas_verdadeiras_e_green(self):
        self.assertEqual(liquidar_aposta(
            f"{self.RESOLVE} + Resultado Final: Corinthians", partida()).resultado,
            GREEN)

    def test_uma_falsa_derruba(self):
        self.assertEqual(liquidar_aposta(
            f"{self.RESOLVE} + {self.PERDE}", partida()).resultado, RED)

    def test_verdadeira_mais_indefinida_e_desconhecido(self):
        """Nunca green: uma perna não verificada não pode virar lucro."""
        self.assertEqual(liquidar_aposta(
            f"{self.RESOLVE} + {self.INDEFINIDA}", partida()).resultado, DESCONHECIDO)

    def test_falsa_mais_indefinida_e_red_por_padrao(self):
        """`red` continua exigindo perna CONFIRMADA perdida — nada é inferido."""
        self.assertEqual(liquidar_aposta(
            f"{self.PERDE} + {self.INDEFINIDA}", partida()).resultado, RED)

    def test_falsa_mais_indefinida_vira_desconhecido_com_a_flag_desligada(self):
        self.assertEqual(liquidar_aposta(
            f"{self.PERDE} + {self.INDEFINIDA}", partida(),
            red_com_perna_indefinida=False).resultado, DESCONHECIDO)


class TestPush(unittest.TestCase):
    """Linha inteira batendo no número: a casa devolve a stake.

    É diferente de "não sei" — aqui se sabe o que aconteceu, e o veredito
    honesto é `void`, não `desconhecido`.
    """

    def test_linha_inteira_exata_e_void(self):
        # 1º tempo foi 1x0 = 1 gol, contra a linha 1
        self.assertEqual(liquidar_aposta(
            "1º tempo - total de gols: Menos de 1", partida()).resultado, VOID)

    def test_linha_meia_nunca_da_push(self):
        self.assertEqual(liquidar_aposta(
            "1º tempo - total de gols: Menos de 1.5", partida()).resultado, GREEN)

    def test_perna_perdida_derruba_mesmo_com_push_na_outra(self):
        """A casa anula a perna do push e liquida o resto — que já foi perdido."""
        self.assertEqual(liquidar_aposta(
            "1º tempo - total de gols: Menos de 1 + Resultado Final: Empate",
            partida()).resultado, RED)


class TestPartidaNaoEncerrada(unittest.TestCase):
    def test_adiada_e_void_nunca_red(self):
        v = liquidar_aposta("Resultado Final: Empate",
                            partida(status="postponed"))
        self.assertEqual(v.resultado, VOID)
        self.assertNotEqual(v.resultado, RED)

    def test_em_andamento_e_void(self):
        self.assertEqual(liquidar_aposta("Resultado Final: Empate",
                                         partida(status="inprogress")).resultado, VOID)


class TestPeL(unittest.TestCase):
    def test_green_paga_odd_menos_um(self):
        self.assertAlmostEqual(
            L.unidades_do_resultado(GREEN, odd=5.30, stake=2.0), 8.60)

    def test_red_perde_a_stake(self):
        self.assertAlmostEqual(
            L.unidades_do_resultado(RED, odd=5.30, stake=2.0), -2.0)

    def test_sem_stake_fica_fora_do_saldo(self):
        """Alerta anterior à migração: inventar a stake contradiria "o
        relatório é registro do que foi recomendado"."""
        self.assertIsNone(L.unidades_do_resultado(GREEN, odd=5.30, stake=None))

    def test_nao_apostavel_nao_credita(self):
        """O alerta saiu dizendo "não vale a pena" — creditar fabricaria
        histórico de aposta que o bot desaconselhou."""
        self.assertEqual(
            L.unidades_do_resultado(GREEN, odd=5.30, stake=0.1, apostavel=False), 0.0)

    def test_desconhecido_e_void_nao_entram(self):
        self.assertIsNone(L.unidades_do_resultado(DESCONHECIDO, 5.3, 2.0))
        self.assertIsNone(L.unidades_do_resultado(VOID, 5.3, 2.0))


class TestChaveDaPartida(unittest.TestCase):
    def test_casas_com_grafias_diferentes_compartilham_a_chave(self):
        """As casas Altenar publicam o mesmo jogo com nomes diferentes; chavear
        pelo texto cru faria a mesma busca fuzzy uma vez por casa."""
        a = L.chave_partida("Vitória - Athletico-PR", "2026-08-06T22:00:00+00:00")
        b = L.chave_partida("Vitória BA - Athletico PR", "2026-08-06T22:00:00+00:00")
        self.assertEqual(a, b)

    def test_mandante_invertido_nao_muda_a_chave(self):
        a = L.chave_partida("Flamengo - Santos", "2026-08-06T22:00:00+00:00")
        b = L.chave_partida("Santos - Flamengo", "2026-08-06T22:00:00+00:00")
        self.assertEqual(a, b)

    def test_dias_diferentes_sao_partidas_diferentes(self):
        a = L.chave_partida("Flamengo - Santos", "2026-08-06T22:00:00+00:00")
        b = L.chave_partida("Flamengo - Santos", "2026-08-13T22:00:00+00:00")
        self.assertNotEqual(a, b)

    def test_evento_sem_separador_devolve_none(self):
        self.assertIsNone(L.chave_partida("texto solto", None))


class TestGuardaDeRegressaoNoStatsCheck(unittest.TestCase):
    """A liquidação NÃO pode ter ampliado a cobertura do `stats_check`.

    Ampliar lá muda quais combos ganham `freq_conjunta_historica`, o que muda o
    rebaixamento de confiança, o que muda a stake de Kelly. Uma feature de
    relatório não pode mexer em quanto se aposta.
    """

    def test_escanteio_e_cartao_seguem_fora_do_stats_check(self):
        from betano_superodds.value.market_parser import parse_leg
        from betano_superodds.value.stats_check import (_CandidatoEvento,
                                                        _predicado_da_perna)
        from betano_superodds.value.models import Matchup

        cand = _CandidatoEvento(
            matchup=Matchup(id=1, league="", home_team="Corinthians",
                            away_team="Internacional", commence_time=None,
                            sport="soccer"),
            home_id=1, away_id=2, home_nome="Corinthians", away_nome="Internacional")
        for texto in ("Total de Escanteios: Mais de 9,5", "Total de Cartões: Mais de 4.5"):
            pred, motivo = _predicado_da_perna(parse_leg(texto), cand)
            self.assertIsNone(pred, f"{texto} não deveria ser coberta no stats_check")
            self.assertIsNotNone(motivo)


if __name__ == "__main__":
    unittest.main()
