"""Testes da CasaDeAposta (livro de mercados) — fonte de REFERÊNCIA para o
consenso, não de oferta. Não confundir com `casadeaposta.py` (combos do CMS
de promoções) nem com `tests/test_casadeaposta.py`.

Os payloads aqui são recortes do formato real, sondado ao vivo em
2026-08-19 (`casadeapostas.bet.br/api/odds/games`). O que os testes travam é
principalmente o que NÃO pode passar: mercado por JOGADOR (que sondando ao
vivo virou ~450 dos 530 nomes de mercado distintos — "<Nome> para marcar
(incl. prolongamento)", um por artilheiro em potencial), mercado com nome
de template não resolvido, mercado de um lado só, hora local confundida com
UTC, e a casa virando fonte de oferta por acidente.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from betano_superodds import config
from betano_superodds.casadeaposta_livro import (
    CASA,
    ESPORTES_REAIS,
    CasaDeApostaLivroScraper,
    _mercado_seguro,
)
from betano_superodds.value.consenso import _familia_da_casa


def daqui(horas: float) -> str:
    """String SEM fuso, como a API manda — testa o gancho de `+00:00`."""
    return (datetime.now(timezone.utc) + timedelta(hours=horas)).strftime(
        "%Y-%m-%dT%H:%M:%S")


def _odd(nome: str, valor: float, state: int = 1) -> dict:
    return {"name": nome, "value": valor, "state": state}


def _mercado(mkt_id: int, nome: str, odds: list[dict], published: bool = True) -> dict:
    return {"id": mkt_id, "name": nome, "published": published, "odds": odds}


def jogo(jid: int, nome: str, markets: list[dict], horas: float = 3.0) -> dict:
    return {"id": jid, "name": nome, "startDate": daqui(horas), "markets": markets}


class ScraperFake(CasaDeApostaLivroScraper):
    """Sem rede: `_get_json` responde do dicionário de páginas por esporte,
    `_warmup` não faz nada."""

    def __init__(self, jogos_por_esporte: dict[int, list[dict]] | None = None,
                 **kw) -> None:
        super().__init__(session=object(), **kw)   # session != None => não abre rede
        self._jogos = jogos_por_esporte or {}
        self.pedidos: list[dict] = []

    async def _warmup(self) -> None:
        return None

    async def _get_json(self, url: str, **params):
        self.pedidos.append(params)
        sport_id = params.get("sportId")
        pagina = params.get("pageNumber", 0)
        todos = self._jogos.get(sport_id, [])
        if pagina > 0:
            return {"items": []}
        return {"items": todos}


class TestFiltroDeEsporte(unittest.TestCase):
    def test_allowlist_cobre_so_o_que_a_pinnacle_carrega(self):
        self.assertEqual(set(ESPORTES_REAIS), {1, 2, 6})

    def test_esporte_fora_da_allowlist_nao_e_sondado(self):
        """Allowlist, não denylist — esporte novo que a casa inventar (ex.
        e-sports, futebol virtual) nasce fora sem precisar de mais uma
        regra de exclusão."""
        sc = ScraperFake({99: [jogo(1, "A - B", [
            _mercado(1, "1x2", [_odd("A", 1.5), _odd("B", 2.5)])])]})
        asyncio.run(sc.scrape())
        self.assertEqual(sc.mercados_vistos, [])


class TestIdentidadeDoEvento(unittest.TestCase):
    def _rodar(self, jogos: list[dict]) -> list:
        sc = ScraperFake({1: jogos})
        asyncio.run(sc.scrape())
        return sc.mercados_vistos

    def test_nome_que_nao_separa_em_dois_times_nao_entra(self):
        m = self._rodar([jogo(1, "Torneio Qualquer", [
            _mercado(1, "1x2", [_odd("A", 1.5), _odd("B", 2.5)])])])
        self.assertEqual(m, [])

    def test_liga_e_sempre_none(self):
        """A listagem não traz nome de campeonato — documentado no
        cabeçalho do módulo como limitação conhecida, não bug."""
        m = self._rodar([jogo(1, "Time A - Time B", [
            _mercado(1, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)])])])
        self.assertTrue(m)
        for r in m:
            self.assertIsNone(r.liga)

    def test_startDate_sem_fuso_e_lido_como_utc_nao_como_local(self):
        """Confirmado ao vivo contra jogo real do Brasileirão: `startDate`
        SEM sufixo é UTC. `models.to_utc_iso` trata string sem fuso como
        hora LOCAL da máquina — sem o `+00:00` grudado antes, a hora sairia
        deslocada pelo fuso local."""
        sc = ScraperFake({1: [{
            "id": 1, "name": "Time A - Time B",
            "startDate": "2026-08-20T00:30:00",
            "markets": [_mercado(1, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)])],
        }]})
        asyncio.run(sc.scrape())
        self.assertEqual(sc.mercados_vistos[0].inicio_evento,
                         "2026-08-20T00:30:00+00:00")


class TestMercadoSeguro(unittest.TestCase):
    """Sondado ao vivo em 2026-08-19 sem filtro de `marketTypeIds`: de 530
    nomes de mercado distintos, ~450 eram mercado por JOGADOR (artilheiro em
    potencial, um por jogador por jogo) — allowlist de mercado é a defesa
    contra isso, não denylist de prefixo."""

    def test_mercados_seguros_passam(self):
        for nome in ("1x2", "Total", "Dupla chance", "Ambas Marcam",
                     "Empate devolve aposta", "Escanteios 1x2",
                     "Total de escanteios", "Escanteios handicap",
                     "Handicap Asiático", "Ímpar/Par", "Vencedor"):
            self.assertTrue(_mercado_seguro(nome), nome)

    def test_mercado_com_prefixo_de_periodo_ainda_casa(self):
        for nome in ("1º tempo - 1x2", "2º tempo - total",
                     "1º tempo - escanteios handicap"):
            self.assertTrue(_mercado_seguro(nome), nome)

    def test_mercado_por_jogador_fica_de_fora(self):
        """A grafia real vista ao vivo: "<Sobrenome>, <Nome> para marcar
        (incl. prolongamento)" — um por jogador, sem prefixo "Jogador" que
        um denylist simples pegaria."""
        for nome in ("Cristiano Da Silva para marcar (incl. prolongamento)",
                     "Carter-Vickers, Cameron para marcar (incl. prolongamento)",
                     "Primeiro jogador a receber cartão",
                     "Último marcador", "Marcador a qualquer altura",
                     "CR Flamengo RJ 1st player to score",
                     "Jogador - Cartões", "Jogador - Chutes no Gol"):
            self.assertFalse(_mercado_seguro(nome), nome)

    def test_mercado_com_template_nao_resolvido_fica_de_fora(self):
        m = ScraperFake({1: [jogo(1, "Time A - Time B", [
            _mercado(1, "{Game.Competitors[0].Competitor.Name} total de escanteios",
                     [_odd("mais de 4.5", 1.9), _odd("menos de 4.5", 1.9)]),
        ])]})
        asyncio.run(m.scrape())
        self.assertEqual(m.mercados_vistos, [])


class TestMercadosDoJogo(unittest.TestCase):
    def _um_jogo(self, markets: list[dict]) -> list:
        sc = ScraperFake({1: [jogo(1, "Time A - Time B", markets)]})
        asyncio.run(sc.scrape())
        return sc.mercados_vistos

    def test_agrupa_por_market_id_e_nao_por_nome(self):
        """Duas linhas de "Total" diferentes (over/under 1.5 e over/under
        3.5) têm `market.id` distintos — agrupar por NOME juntaria as duas
        num mercado só (mesma armadilha do `marketId`/`marketUuid` da
        Superbet)."""
        m = self._um_jogo([
            _mercado(100, "Total", [_odd("mais de 1.5", 1.18), _odd("menos de 1.5", 3.95)]),
            _mercado(101, "Total", [_odd("mais de 3.5", 2.45), _odd("menos de 3.5", 1.44)]),
        ])
        self.assertEqual(len({r.market_id for r in m}), 2)
        self.assertEqual(len(m), 4)

    def test_mercado_de_um_lado_so_e_descartado(self):
        m = self._um_jogo([
            _mercado(100, "Total", [_odd("mais de 1.5", 1.18)]),
            _mercado(101, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)]),
        ])
        self.assertEqual({r.market_nome for r in m}, {"1x2"})

    def test_odd_com_state_diferente_de_1_e_descartada(self):
        """Não há exemplo ao vivo de `state != 1` na sondagem, mas a postura
        é a mesma da Superbet com `status != "active"`: recusar em vez de
        confiar num preço que pode estar fechado/suspenso."""
        m = self._um_jogo([
            _mercado(100, "1x2", [_odd("Time A", 1.5, state=1),
                                  _odd("Time B", 2.5, state=0)]),
        ])
        self.assertEqual(m, [])

    def test_mercado_nao_publicado_e_descartado(self):
        m = self._um_jogo([
            _mercado(100, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)],
                     published=False),
        ])
        self.assertEqual(m, [])

    def test_preco_invalido_nao_entra(self):
        m = self._um_jogo([
            _mercado(100, "1x2", [_odd("Time A", 1.0), _odd("Time B", None)]),
        ])
        self.assertEqual(m, [])

    def test_identidade_vai_junto_em_toda_linha(self):
        m = self._um_jogo([
            _mercado(100, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)]),
        ])
        self.assertTrue(m)
        for r in m:
            self.assertEqual(r.casa, CASA)
            self.assertEqual(r.evento, "Time A - Time B")
            self.assertTrue(r.inicio_evento)


class TestNaoEFonteDeOferta(unittest.TestCase):
    def test_scrape_nunca_devolve_oferta(self):
        sc = ScraperFake({1: [jogo(1, "Time A - Time B", [
            _mercado(100, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)])])]})
        self.assertEqual(asyncio.run(sc.scrape()), [])
        self.assertTrue(sc.mercados_vistos, "mas o pool tinha que encher")


class TestFamiliaDeFeed(unittest.TestCase):
    def test_casadeaposta_e_familia_propria(self):
        outras = {_familia_da_casa("Betano"), _familia_da_casa("Superbet"),
                  _familia_da_casa("vupi")}
        self.assertNotIn(_familia_da_casa(CASA), outras)


class TestOrcamentoDePaginas(unittest.TestCase):
    def test_para_de_paginar_quando_pagina_vem_curta(self):
        """Página com menos que `_PAGE_SIZE` itens é o fim do catálogo —
        não vale gastar mais requests batendo em páginas vazias."""
        sc = ScraperFake({1: [jogo(1, "Time A - Time B", [
            _mercado(100, "1x2", [_odd("Time A", 1.5), _odd("Time B", 2.5)])])]})
        asyncio.run(sc.scrape())
        pedidos_futebol = [p for p in sc.pedidos if p.get("sportId") == 1]
        self.assertEqual(len(pedidos_futebol), 1)


class TestDiagnosticoDeAlvos(unittest.TestCase):
    """`alvos` aqui não decide o que pedir (a listagem já traz o livro
    completo, sem orçamento de detalhe pra repartir) — é só diagnóstico de
    quantos eventos da fila apareceram nas páginas puxadas."""

    def test_alvo_que_bate_e_contado(self):
        itens = [jogo(1, "Time A - Time B", []), jogo(2, "Time C - Time D", [])]
        sc = ScraperFake({1: itens}, alvos=[
            {"evento": "Time A - Time B", "inicio_evento": daqui(3)}])
        self.assertEqual(sc._contar_alvos_batidos(itens), 1)

    def test_alvo_que_nao_bate_nao_e_contado(self):
        itens = [jogo(1, "Time A - Time B", [])]
        sc = ScraperFake({1: itens}, alvos=[
            {"evento": "Jogo Que Nao Existe - Outro", "inicio_evento": daqui(3)}])
        self.assertEqual(sc._contar_alvos_batidos(itens), 0)

    def test_varios_alvos_conta_so_os_que_batem(self):
        itens = [jogo(1, "Time A - Time B", []), jogo(2, "Time C - Time D", [])]
        sc = ScraperFake({1: itens}, alvos=[
            {"evento": "Time A - Time B", "inicio_evento": daqui(3)},
            {"evento": "Time C - Time D", "inicio_evento": daqui(3)},
            {"evento": "Ninguem - Contra Ninguem", "inicio_evento": daqui(3)},
        ])
        self.assertEqual(sc._contar_alvos_batidos(itens), 2)

    def test_sem_candidatos_validos_nao_quebra(self):
        """Evento sem par de times (nome que não separa) não vira candidato
        — não pode gerar erro, só zero."""
        itens = [jogo(1, "Torneio Qualquer", [])]
        sc = ScraperFake({1: itens}, alvos=[
            {"evento": "Time A - Time B", "inicio_evento": daqui(3)}])
        self.assertEqual(sc._contar_alvos_batidos(itens), 0)


if __name__ == "__main__":
    unittest.main()
