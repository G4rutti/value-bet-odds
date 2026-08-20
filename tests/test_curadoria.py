"""Testes de `curadoria.py` — veredito estruturado sobre os sinais do
SofaScore (aprovado / degrau / vetado).

`_julgar` é testado isoladamente (matriz de sinais -> decisão, sem rede —
mesma filosofia de `test_stats_check.py`). `avaliar()` é testado com um
cliente fake mínimo que nunca acha nada (cai no baseline de `checar_stats`,
tudo `None`/vazio) — o que importa aqui é a elegibilidade e o campo `modo`,
não o cálculo dos sinais em si (isso já é coberto em `test_stats_check.py`).
"""

from __future__ import annotations

import unittest

from betano_superodds.value import config as vconfig
from betano_superodds.value.curadoria import ELEGIVEL, avaliar, _julgar


class TestJulgar(unittest.TestCase):
    def test_nenhum_sinal_grave_aprova(self):
        decisao, motivo = _julgar({}, "alta")
        self.assertEqual(decisao, "aprovado")
        self.assertIsNone(motivo)

    def test_sinais_none_nao_contam_como_graves(self):
        """`None` é "não sei", não é "sim" — não pode empurrar pro veto."""
        sinais = {"flag_noticia_fresca": None,
                 "diverge_da_estimativa_independente": None,
                 "forma_desfavoravel": None}
        decisao, motivo = _julgar(sinais, "alta")
        self.assertEqual(decisao, "aprovado")

    def test_sinais_false_nao_contam_como_graves(self):
        sinais = {"flag_noticia_fresca": False,
                 "diverge_da_estimativa_independente": False,
                 "forma_desfavoravel": False}
        decisao, motivo = _julgar(sinais, "alta")
        self.assertEqual(decisao, "aprovado")

    def test_um_sinal_grave_rebaixa_um_degrau_nao_veta(self):
        decisao, motivo = _julgar({"flag_noticia_fresca": True}, "alta")
        self.assertEqual(decisao, "degrau")
        self.assertIn("notícia fresca", motivo)

    def test_dois_sinais_graves_vetam(self):
        sinais = {"flag_noticia_fresca": True,
                 "diverge_da_estimativa_independente": True}
        decisao, motivo = _julgar(sinais, "alta")
        self.assertEqual(decisao, "vetado")
        self.assertIn("notícia fresca", motivo)
        self.assertIn("combo diverge", motivo)

    def test_tres_sinais_graves_tambem_vetam_nao_so_dois(self):
        sinais = {"flag_noticia_fresca": True,
                 "diverge_da_estimativa_independente": True,
                 "forma_desfavoravel": True}
        decisao, motivo = _julgar(sinais, "alta")
        self.assertEqual(decisao, "vetado")

    def test_threshold_de_veto_e_calibravel_via_config(self):
        """`CURADORIA_MIN_SINAIS_VETO` não é fixo no código — subir pra 3
        exige um sinal a mais pra vetar."""
        original = vconfig.CURADORIA_MIN_SINAIS_VETO
        try:
            vconfig.CURADORIA_MIN_SINAIS_VETO = 3
            sinais = {"flag_noticia_fresca": True,
                     "diverge_da_estimativa_independente": True}
            decisao, _ = _julgar(sinais, "alta")
            self.assertEqual(decisao, "degrau", "2 sinais não bastam mais com o piso em 3")
        finally:
            vconfig.CURADORIA_MIN_SINAIS_VETO = original


class _FakeClienteVazio:
    """Cliente que nunca acha nada (busca sempre vazia) — `checar_stats`
    cai no `_matchups_candidatos` vazio e devolve o baseline (tudo `None`)
    sem precisar das fixtures ricas de `test_stats_check.py`. Suficiente pra
    testar elegibilidade/modo de `avaliar()`, que é o que este arquivo cobre."""

    def buscar(self, termo: str) -> dict:
        return {"results": []}

    def jogos_time(self, team_id: int, *, direcao: str, pagina: int = 0,
                   ttl_horas: float | None = None) -> list:
        return []

    def lineups(self, event_id: int):
        return None


def _oferta() -> dict:
    return {"evento": "Time A - Time B"}


class TestAvaliarIntegracao(unittest.TestCase):
    def setUp(self):
        self._enforce_orig = vconfig.CURADORIA_ENFORCE

    def tearDown(self):
        vconfig.CURADORIA_ENFORCE = self._enforce_orig

    def test_confianca_insuficiente_nao_e_elegivel(self):
        self.assertNotIn("insuficiente", ELEGIVEL)
        v = avaliar(_oferta(), ["Ambas Equipes Marcam: Sim"], "insuficiente")
        self.assertIsNone(v)

    def test_baixa_e_elegivel_pedido_do_dono(self):
        """Hoje o gate legado (`pipeline._CONFIANCA_ELEGIVEL_STATS`) exclui
        "baixa" — este módulo NÃO exclui, de propósito."""
        self.assertIn("baixa", ELEGIVEL)
        v = avaliar(_oferta(), ["Ambas Equipes Marcam: Sim"], "baixa",
                    cliente=_FakeClienteVazio())
        self.assertIsNotNone(v)
        self.assertEqual(v.decisao, "aprovado")
        self.assertEqual(v.confianca_original, "baixa")
        self.assertEqual(v.confianca_final, "baixa")

    def test_modo_reflete_curadoria_enforce(self):
        vconfig.CURADORIA_ENFORCE = False
        v_sombra = avaliar(_oferta(), ["Ambas Equipes Marcam: Sim"], "alta",
                           cliente=_FakeClienteVazio())
        self.assertEqual(v_sombra.modo, "sombra")

        vconfig.CURADORIA_ENFORCE = True
        v_enforce = avaliar(_oferta(), ["Ambas Equipes Marcam: Sim"], "alta",
                            cliente=_FakeClienteVazio())
        self.assertEqual(v_enforce.modo, "enforce")


if __name__ == "__main__":
    unittest.main()
