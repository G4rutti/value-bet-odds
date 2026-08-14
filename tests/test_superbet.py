"""Testes da Superbet — fonte de REFERÊNCIA para o consenso, não de oferta.

Os payloads aqui são recortes do formato real, sondado em 2026-08-14
(`production-superbet-offer-br.freetls.fastly.net`). O que os testes travam é
principalmente o que NÃO pode passar: evento virtual com nome de time real,
mercado de um lado só, e a casa virando fonte de oferta por acidente.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from betano_superodds import config
from betano_superodds.superbet import CASA, ESPORTES_REAIS, SuperbetScraper
from betano_superodds.value.consenso import _chave_da_linha, _familia_da_casa
from betano_superodds.value.market_parser import chave_consenso


def daqui(horas: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=horas)).isoformat()


def _odd(uuid: str, mercado: str, selecao: str, preco: float,
         status: str = "active") -> dict:
    return {"marketUuid": uuid, "marketId": 100001, "marketName": mercado,
            "name": selecao, "price": preco, "status": status}


def evento(ev_id: int, nome: str, sport_id: int = 5, horas: float = 3.0,
           torneio: str = "900") -> dict:
    return {"eventId": ev_id, "matchName": nome, "sportId": sport_id,
            "tournamentId": torneio, "utcDate": daqui(horas),
            "marketCount": 12}


class ScraperFake(SuperbetScraper):
    """Sem rede: `_get` responde do dicionário de rotas."""

    def __init__(self, listagem: list[dict], detalhes: dict[int, dict] | None = None,
                 torneios: dict[str, str] | None = None, **kw) -> None:
        super().__init__(session=object(), **kw)   # session != None => não abre rede
        self._listagem_fake = listagem
        self._detalhes = detalhes or {}
        self._torneios_fake = torneios or {"900": "Inglaterra - Premier League"}
        self.pedidos: list[str] = []

    async def _get(self, path: str, **params):
        self.pedidos.append(path)
        if path == "/struct":
            return {"data": {"tournaments": [
                {"id": k, "localNames": {"pt-BR": v}}
                for k, v in self._torneios_fake.items()]}}
        if path == "/events/by-date":
            return {"data": self._listagem_fake}
        if path.startswith("/events/"):
            ev_id = int(path.rsplit("/", 1)[1])
            return {"data": self._detalhes.get(ev_id, {"odds": []})}
        raise AssertionError(f"rota não esperada: {path}")


class TestFiltroDeEsporte(unittest.TestCase):
    """Futebol virtual usa nome de time REAL.

    "Granada (V)·Valencia (V)" e "Sporting Lisboa (JKey)·..." normalizam pra
    `granada v` / `lisboa jkey`, que o matcher casa com o jogo de verdade —
    injetando preço de partida simulada como referência de partida real. Eram
    563 dos 3.450 eventos da listagem quando isto foi medido.
    """

    def test_allowlist_cobre_so_o_que_a_pinnacle_carrega(self):
        self.assertEqual(set(ESPORTES_REAIS), {5, 2, 4})

    def test_virtual_e_esport_ficam_de_fora(self):
        lst = [evento(1, "Arsenal·Manchester City", sport_id=5),
               evento(2, "Granada (V)·Valencia (V)", sport_id=190),
               evento(3, "Sporting Lisboa (JKey)·Vitória Guimarães (JKey)", sport_id=75),
               evento(4, "Toronto Raptors·Miami Heat", sport_id=70)]
        sc = ScraperFake(lst)
        achados = asyncio.run(sc._listagem())
        self.assertEqual([e["evento"] for e in achados],
                         ["Arsenal - Manchester City"])

    def test_e_allowlist_nao_denylist(self):
        """Esporte novo que a Superbet inventar tem que nascer FORA. Uma
        denylist deixaria passar o virtual da próxima temporada."""
        sc = ScraperFake([evento(9, "A·B", sport_id=99999)])
        self.assertEqual(asyncio.run(sc._listagem()), [])


class TestIdentidadeDoEvento(unittest.TestCase):
    def test_separador_vira_hifen(self):
        sc = ScraperFake([evento(1, "Arsenal·Manchester City")])
        self.assertEqual(asyncio.run(sc._listagem())[0]["evento"],
                         "Arsenal - Manchester City")

    def test_liga_vem_do_struct(self):
        """Sem `liga` o `genero_conflita` não enxerga o marcador feminino do
        lado do pool e a ponte de evento recusa o jogo — falha fechada, mas é
        cobertura perdida de graça."""
        sc = ScraperFake([evento(1, "Arsenal·Man City", torneio="900")])
        self.assertEqual(asyncio.run(sc._listagem())[0]["liga"],
                         "Inglaterra - Premier League")

    def test_nome_que_nao_separa_em_dois_times_nao_entra(self):
        """Entraria no pool como linha morta: sem dois times, não casa com
        nada e ainda ocupa espaço na varredura da ponte."""
        sc = ScraperFake([evento(1, "Torneio Qualquer")])
        self.assertEqual(asyncio.run(sc._listagem()), [])


class TestMercadosDoDetalhe(unittest.TestCase):
    def _um_evento(self, odds: list[dict]) -> list:
        lst = [evento(1, "Arsenal·Manchester City")]
        sc = ScraperFake(lst, {1: {"odds": odds}})
        asyncio.run(sc.scrape())
        return sc.mercados_vistos

    def test_agrupa_por_marketUuid_e_nao_por_marketId(self):
        """`marketId` é o TIPO do mercado e se repete entre linhas: as 7
        linhas de "Total de Gols" compartilham `marketId`, cada uma com seu
        `marketUuid`. Agrupar por `marketId` juntaria over 0.5 com over 3.5
        num mercado só, e `remover_vig` recusaria tudo em silêncio."""
        m = self._um_evento([
            _odd("u1", "Total de Gols", "Mais de 0.5", 1.06),
            _odd("u1", "Total de Gols", "Menos de 0.5", 10.0),
            _odd("u2", "Total de Gols", "Mais de 3.5", 4.20),
            _odd("u2", "Total de Gols", "Menos de 3.5", 1.22),
        ])
        self.assertEqual(len({r.market_id for r in m}), 2)
        self.assertEqual(len(m), 4)

    def test_mercado_de_um_lado_so_e_descartado(self):
        """Todo o catálogo `Jogador - *` desta casa é assim: só o preço do
        "sim"/"mais de". Sem os dois lados não há margem a medir, e
        `remover_vig` recusaria — melhor nem gravar."""
        m = self._um_evento([
            _odd("p1", "Jogador - Chutes no Gol", "White, Ben - Mais de 0.5", 4.55),
            _odd("p2", "Jogador - Marcar Gol", "Haaland, Erling", 2.47),
            _odd("ok", "Total de Cartões", "Mais de 4.5", 1.90),
            _odd("ok", "Total de Cartões", "Menos de 4.5", 1.90),
        ])
        self.assertEqual({r.market_nome for r in m}, {"Total de Cartões"})

    def test_selecao_suspensa_sai_do_grupo(self):
        """Preço de seleção suspensa é velho — deixá-la faria a margem do
        mercado parecer outra coisa."""
        m = self._um_evento([
            _odd("u1", "Total de Cartões", "Mais de 4.5", 1.90),
            _odd("u1", "Total de Cartões", "Menos de 4.5", 1.90, status="suspended"),
        ])
        self.assertEqual(m, [], "sobrou 1 lado — o grupo tinha que cair inteiro")

    def test_preco_invalido_nao_entra(self):
        m = self._um_evento([
            _odd("u1", "Total de Cartões", "Mais de 4.5", 1.0),
            _odd("u1", "Total de Cartões", "Menos de 4.5", None),
        ])
        self.assertEqual(m, [])

    def test_identidade_vai_junto_em_toda_linha(self):
        m = self._um_evento([
            _odd("u1", "Total de Cartões", "Mais de 4.5", 1.90),
            _odd("u1", "Total de Cartões", "Menos de 4.5", 1.90),
        ])
        self.assertTrue(m)
        for r in m:
            self.assertEqual(r.casa, CASA)
            self.assertEqual(r.evento, "Arsenal - Manchester City")
            self.assertTrue(r.inicio_evento)
            self.assertTrue(r.liga)


class TestNaoEFonteDeOferta(unittest.TestCase):
    def test_scrape_nunca_devolve_oferta(self):
        """A rota da odd turbinada da Superbet nunca foi achada
        (`CASAS-PENDENTES.md`). Inventar uma oferta a partir de
        `matchTags=price_boost`, sem saber QUAL seleção foi turbinada,
        produziria oferta fantasma."""
        sc = ScraperFake([evento(1, "Arsenal·Man City")],
                         {1: {"odds": [_odd("u1", "Total de Cartões", "Mais de 4.5", 1.9),
                                       _odd("u1", "Total de Cartões", "Menos de 4.5", 1.9)]}})
        self.assertEqual(asyncio.run(sc.scrape()), [])
        self.assertTrue(sc.mercados_vistos, "mas o pool tinha que encher")


class TestFamiliaDeFeed(unittest.TestCase):
    def test_superbet_e_familia_propria(self):
        """O ponto inteiro da casa: hoje o pool só tem `altenar` e `betano`,
        e uma 12ª casa Altenar somaria zero. Ver `CONSENSO_MIN_FAMILIAS`."""
        self.assertNotIn(_familia_da_casa(CASA),
                         {_familia_da_casa("vupi"), _familia_da_casa("Betano")})


class TestChaveCasaComAGrafiaDaSuperbet(unittest.TestCase):
    """O casamento canônico tem que ligar a grafia DELA à grafia da NOSSA
    oferta — senão a fonte entra no banco e não serve pra nada."""

    def test_grafias_reais_casam(self):
        pares = [
            (("Total de Cartões", "Mais de 4.5"), "Total de Cartões Mais de 4.5"),
            (("1º Tempo - Total de Cartões", "Mais de 1.5"),
             "1º tempo - total cartões Mais de 1.5"),
            (("Total de Chutes no Gol", "Mais de 8.5"),
             "Total de Chutes no gol Mais de 8.5"),
            (("Total de Defesas do Goleiro", "Mais de 3.5"),
             "Total de Defesas do Goleiro Mais de 3.5"),
            (("Arsenal - Total de Cartões", "Mais de 1.5"),
             "Cartões do Arsenal Mais de 1.5"),
        ]
        for (mercado, selecao), perna in pares:
            self.assertEqual(
                _chave_da_linha({"market_nome": mercado, "selecao": selecao}),
                chave_consenso(perna), f"{mercado} / {selecao}")


class TestSondaDirecionada(unittest.TestCase):
    def test_alvo_da_fila_vem_na_frente(self):
        lst = [evento(1, "A Um·B Um", horas=1),
               evento(2, "C Dois·D Dois", horas=2),
               evento(3, "Norwich·West Bromwich", horas=5)]
        sc = ScraperFake(lst, {}, alvos=[
            {"evento": "Norwich - West Bromwich", "inicio_evento": daqui(5)}])
        antigo = config.SUPERBET_MAX_DETALHES
        config.SUPERBET_MAX_DETALHES = 1
        try:
            asyncio.run(sc.scrape())
        finally:
            config.SUPERBET_MAX_DETALHES = antigo
        self.assertIn("/events/3", sc.pedidos)

    def test_guarda_de_uf_vale_aqui_tambem(self):
        """Casar errado aqui não gera edge falso, mas gasta o request no jogo
        errado — e é o request que esta fonte existe pra dar."""
        lst = [evento(1, "Botafogo-SP·Ferroviaria", horas=2)]
        sc = ScraperFake(lst, {}, alvos=[
            {"evento": "Botafogo-RJ - Ferroviaria", "inicio_evento": daqui(2)}])
        eventos = asyncio.run(sc._listagem())
        self.assertEqual(sc._eventos_da_fila(eventos), [])
        # controle positivo: com o MESMO UF, casa.
        sc.alvos = [{"evento": "Botafogo-SP - Ferroviaria", "inicio_evento": daqui(2)}]
        self.assertEqual(sc._eventos_da_fila(eventos), [1])

    def test_alvo_alem_do_horizonte_nao_gasta_request(self):
        """A tolerância do matcher mede a CONCORDÂNCIA entre as duas datas,
        não a distância até agora."""
        alem = config.ALTENAR_HORIZONTE_HORAS + 5
        lst = [evento(1, "A Um·B Um", horas=alem)]
        sc = ScraperFake(lst, {}, alvos=[
            {"evento": "A Um - B Um", "inicio_evento": daqui(alem)}])
        eventos = asyncio.run(sc._listagem())
        self.assertEqual(sc._eventos_da_fila(eventos), [])

    def test_jogo_ja_iniciado_nao_gasta_request(self):
        sc = ScraperFake([evento(1, "A Um·B Um", horas=-1)], {})
        asyncio.run(sc.scrape())
        self.assertNotIn("/events/1", sc.pedidos)


if __name__ == "__main__":
    unittest.main()
