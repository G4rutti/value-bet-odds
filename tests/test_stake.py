"""Testes do dimensionamento de aposta (Kelly fracionado, em unidades)."""

from __future__ import annotations

import unittest
from unittest import mock

from betano_superodds.value import config, stake


class TestKellyCheio(unittest.TestCase):
    def test_formula(self):
        """odd 2.00 com p=0.55: f = (1·0.55 − 0.45)/1 = 0.10."""
        self.assertAlmostEqual(
            stake.kelly_cheio(odd_boost=2.00, odd_justa=1 / 0.55), 0.10, places=4)

    def test_sem_edge_devolve_zero(self):
        """odd oferecida igual à justa: nada a ganhar."""
        self.assertEqual(stake.kelly_cheio(1.90, 1.90), 0.0)

    def test_odd_pior_que_a_justa_nao_vira_negativo(self):
        self.assertEqual(stake.kelly_cheio(1.50, 1.90), 0.0)

    def test_odds_invalidas(self):
        self.assertEqual(stake.kelly_cheio(1.0, 1.5), 0.0)
        self.assertEqual(stake.kelly_cheio(2.0, 1.0), 0.0)

    def test_cresce_com_o_edge(self):
        pouco = stake.kelly_cheio(2.00, 1.95)
        muito = stake.kelly_cheio(2.00, 1.60)
        self.assertLess(pouco, muito)


class TestCalcular(unittest.TestCase):
    def test_caso_real_de_tenis(self):
        """Carol Zhao: boost 1.95, justa 1.79 -> ~2un no ¼ Kelly."""
        s = stake.calcular(1.95, 1.79)
        self.assertTrue(s.apostavel)
        self.assertAlmostEqual(s.unidades, 2.25, places=2)
        self.assertIn("¼ Kelly", s.descrever())

    def test_arredonda_sempre_para_baixo(self):
        """Errar pra menos custa retorno; pra mais custa banca."""
        with mock.patch.object(config, "STAKE_PASSO", 0.25):
            for bruto, esperado in ((2.99, 2.75), (2.75, 2.75), (0.99, 0.75)):
                self.assertEqual(stake._arredondar_para_baixo(bruto, 0.25), esperado)

    def test_teto_limita_e_avisa(self):
        """Kelly em odd baixa com edge alto pede fração absurda da banca."""
        s = stake.calcular(1.21, 1.12)   # 38% da banca no Kelly cheio
        self.assertGreater(s.kelly_cheio, 0.30)
        self.assertEqual(s.unidades, config.STAKE_MAX_UNIDADES)
        self.assertTrue(s.limitada_pelo_teto)
        self.assertIn("teto", s.descrever())

    def test_edge_minusculo_fica_abaixo_do_minimo(self):
        s = stake.calcular(2.00, 1.99)
        self.assertFalse(s.apostavel)
        self.assertIn("não vale a pena", s.descrever())

    def test_sem_edge_nao_recomenda_nada(self):
        s = stake.calcular(1.80, 1.90)
        self.assertEqual(s.unidades, 0.0)
        self.assertFalse(s.apostavel)


class TestDescontos(unittest.TestCase):
    """A odd justa às vezes é sabidamente otimista — o stake tem que refletir."""

    def test_combo_aposta_menos_que_simples_no_mesmo_preco(self):
        simples = stake.calcular(3.00, 2.40, tipo_mercado="simples")
        combo = stake.calcular(3.00, 2.40, tipo_mercado="combo")
        self.assertLess(combo.unidades, simples.unidades)
        self.assertIn("combo", combo.descrever())

    def test_linha_interpolada_aposta_menos(self):
        firme = stake.calcular(3.00, 2.40)
        estimada = stake.calcular(3.00, 2.40, interpolada=True)
        self.assertLess(estimada.unidades, firme.unidades)
        self.assertIn("linha estimada", estimada.descrever())

    def test_descontos_acumulam(self):
        s = stake.calcular(6.00, 4.20, tipo_mercado="combo", interpolada=True)
        so_combo = stake.calcular(6.00, 4.20, tipo_mercado="combo")
        self.assertLess(s.unidades, so_combo.unidades)
        self.assertIn("combo", s.descrever())
        self.assertIn("linha estimada", s.descrever())


class TestFracaoDeKelly(unittest.TestCase):
    def test_fracao_escala_o_stake(self):
        with mock.patch.object(config, "KELLY_FRACAO", 0.25):
            quarto = stake.calcular(2.50, 2.00).fracao_usada
        with mock.patch.object(config, "KELLY_FRACAO", 0.5):
            metade = stake.calcular(2.50, 2.00).fracao_usada
        self.assertGreater(metade, quarto)

    def test_rotulos_dos_valores_comuns(self):
        for fracao, rotulo in ((1.0, "Kelly cheio"), (0.5, "½ Kelly"),
                               (0.25, "¼ Kelly"), (0.125, "⅛ Kelly")):
            with mock.patch.object(config, "KELLY_FRACAO", fracao):
                self.assertEqual(stake._rotulo_kelly(), rotulo)

    def test_fracao_incomum_tem_rotulo_generico(self):
        with mock.patch.object(config, "KELLY_FRACAO", 0.3):
            self.assertEqual(stake._rotulo_kelly(), "0.3× Kelly")

    def test_unidade_maior_significa_menos_unidades(self):
        """1un = 2% da banca -> metade das unidades pro mesmo dinheiro."""
        with mock.patch.object(config, "UNIDADE_PCT_BANCA", 1.0):
            a = stake.calcular(2.50, 2.00)
        with mock.patch.object(config, "UNIDADE_PCT_BANCA", 2.0):
            b = stake.calcular(2.50, 2.00)
        self.assertAlmostEqual(b.unidades, a.unidades / 2, delta=config.STAKE_PASSO)


class TestFormatacao(unittest.TestCase):
    def test_nao_escreve_2_ponto_zero_un(self):
        self.assertEqual(stake._fmt(2.0), "2")
        self.assertEqual(stake._fmt(1.25), "1.25")
        self.assertEqual(stake._fmt(0.5), "0.5")


if __name__ == "__main__":
    unittest.main(verbosity=2)
