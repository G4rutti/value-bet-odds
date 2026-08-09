"""Testes do scraper MR12 da Betano — foco no writer de `mercados_casa`.

Fixtures no formato verificado ao vivo em 2026-08-09 contra
`/api/odds/bahia-vasco-da-gama/89808694/?req=m,ms,s,c,stnf` (evento real
Bahia-Vasco da Gama): o nome humano do mercado é `market["name"]` (não
`marketName`), `market["id"]` é estável, `selection["name"]` já é o texto
pronto ("Mais de 4.5", "Sim"), e um mercado como "Total de Cartões" agrupa
várias linhas sob o mesmo `id` — o payload real tem muito mais campos por
mercado/seleção do que os aqui, trimados para o que o parser lê.
"""

from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone

from betano_superodds import config
from betano_superodds.scraper import BetanoScraper


def daqui_millis(horas: float) -> int:
    quando = datetime.now(timezone.utc) + timedelta(hours=horas)
    return int(quando.timestamp() * 1000)


EVENT_ID = 999
EVENT_URL = "/odds/bahia-vasco-da-gama/999/"


def listagem() -> dict:
    """Payload de `/api/sport/{slug}/jogos-de-hoje/`: evento com MR12 na listagem."""
    return {
        "data": {
            "blocks": [{
                "events": [{
                    "id": EVENT_ID,
                    "name": "Bahia - Vasco da Gama",
                    "url": EVENT_URL,
                    "participants": [{"name": "Bahia"}, {"name": "Vasco da Gama"}],
                    "startTime": daqui_millis(3),
                    "leagueName": "Brasileirão - Série A Betano",
                    "markets": [{
                        "id": "mkt-mr12",
                        "type": config.SUPERODDS_MARKET_TYPE,
                        "marketCloseTimeMillis": daqui_millis(3),
                        "selections": [
                            {"id": 1, "name": "Bahia", "fullName": "Bahia", "price": 2.20},
                            {"id": 2, "name": "Empate", "fullName": "Empate", "price": 3.30},
                            {"id": 3, "name": "Vasco da Gama", "fullName": "Vasco da Gama",
                             "price": 4.00},
                        ],
                    }],
                }],
            }],
        },
    }


def detalhe() -> dict:
    """Payload de `/api{event_url}?req=m,ms,s,c,stnf`: o evento inteiro."""
    return {
        "data": {
            "event": {"id": EVENT_ID, "name": "Bahia - Vasco da Gama"},
            "marketOffersData": {
                "marketOffers": {
                    "mkt-mr12": [{"offerTypeId": config.SUPERODDS_OFFER_TYPE_ID}],
                },
            },
            "markets": [
                {
                    "id": "mkt-mres",
                    "type": config.BASE_MARKET_TYPE,
                    "name": "Resultado Final",
                    "selections": [
                        {"id": 1, "name": "Bahia", "fullName": "Bahia", "price": 2.10},
                        {"id": 2, "name": "Empate", "fullName": "Empate", "price": 3.20},
                        {"id": 3, "name": "Vasco da Gama", "fullName": "Vasco da Gama",
                         "price": 3.90},
                    ],
                },
                # Mercado de cartões: dois lados válidos, é o caso central
                # que a Pinnacle não cobre — matéria prima do consenso.
                {
                    "id": "mkt-cartoes",
                    "type": "TCOU",
                    "name": "Total de Cartões",
                    "selections": [
                        {"id": 10, "name": "Mais de 4.5", "price": 1.90},
                        {"id": 11, "name": "Menos de 4.5", "price": 1.85},
                    ],
                },
                # Seleção suspensa (price 0): sobra 1 lado válido só, e
                # mercado com <2 lados válidos não serve pro de-vig.
                {
                    "id": "mkt-suspenso",
                    "type": "BTSC",
                    "name": "Ambas equipes Marcam",
                    "selections": [
                        {"id": 20, "name": "Sim", "price": 1.75},
                        {"id": 21, "name": "Não", "price": 0},
                    ],
                },
                # Mercado de artilheiro/placar exato: `selections` vem vazio
                # de verdade (usa `scorerSelections`/`exactScoreSelections`),
                # então cai na mesma guarda de "<2 lados válidos".
                {
                    "id": "mkt-artilheiro",
                    "type": "PSCR",
                    "name": "Marcar em qualquer momento",
                    "selections": [],
                    "scorerSelections": [{"name": "Everaldo", "price": 2.5}],
                },
            ],
        },
    }


class ScraperFake(BetanoScraper):
    """Troca a rede pelos payloads fixos acima."""

    def __init__(self, listagem_payload: dict, detalhe_payload: dict) -> None:
        super().__init__(session=object())  # type: ignore[arg-type]
        self._listagem, self._detalhe = listagem_payload, detalhe_payload

    async def _get_json(self, path: str) -> dict:
        if "jogos-de-hoje" in path:
            return self._listagem
        return self._detalhe


class TestExtractOriginalsRegressao(unittest.TestCase):
    """`_extract_originals` (odd original do MR12) não pode mudar de
    comportamento — a escrita em `mercados_casa` é aditiva, ao lado."""

    def test_odd_original_continua_vindo_do_mres(self):
        detail = detalhe()
        originals, confirmed = BetanoScraper._extract_originals(detail, "mkt-mr12")
        self.assertEqual(originals["Bahia"], 2.10)
        self.assertEqual(originals["Empate"], 3.20)
        self.assertEqual(originals["Vasco da Gama"], 3.90)
        self.assertTrue(confirmed)


class TestMercadosParaConsenso(unittest.TestCase):
    """`mercados_vistos`: o mercado COMPLETO de cada seleção, não só o MR12."""

    def _rodar(self) -> BetanoScraper:
        sc = ScraperFake(listagem(), detalhe())
        asyncio.run(sc._scrape_mr12_for_sport(config.SPORTS[0]))
        return sc

    def test_mercado_nao_1x2_e_colhido_com_os_dois_lados(self):
        sc = self._rodar()
        cartoes = {m.selecao: m.preco for m in sc.mercados_vistos
                   if m.market_nome == "Total de Cartões"}
        self.assertEqual(cartoes, {"Mais de 4.5": 1.90, "Menos de 4.5": 1.85})

    def test_selecao_com_preco_menor_ou_igual_a_1_e_descartada(self):
        sc = self._rodar()
        nomes = {m.market_nome for m in sc.mercados_vistos}
        self.assertNotIn("Ambas equipes Marcam", nomes,
                          "sobrou 1 lado válido só (o outro tinha price=0)")

    def test_mercado_com_menos_de_dois_lados_e_descartado(self):
        sc = self._rodar()
        nomes = {m.market_nome for m in sc.mercados_vistos}
        self.assertNotIn("Marcar em qualquer momento", nomes,
                          "artilheiro não tem `selections`, só `scorerSelections`")

    def test_grava_casa_evento_e_identidade(self):
        sc = self._rodar()
        um = next(m for m in sc.mercados_vistos if m.market_nome == "Total de Cartões")
        self.assertEqual(um.casa, "Betano")
        self.assertEqual(um.evento_id, str(EVENT_ID))
        self.assertEqual(um.evento, "Bahia - Vasco da Gama")
        self.assertEqual(um.liga, "Brasileirão - Série A Betano")
        self.assertIsNotNone(um.inicio_evento)

    def test_linhas_diferentes_do_mesmo_mercado_ficam_separadas(self):
        """Mesmo princípio do writer Altenar: `market_id` é o que impede duas
        linhas (ou dois mercados homônimos) de virar um só e destruir a
        margem no de-vig."""
        det = detalhe()
        det["data"]["markets"].append({
            "id": "mkt-cartoes-outra-linha",
            "type": "TCOU",
            "name": "Total de Cartões",
            "selections": [
                {"id": 30, "name": "Mais de 5.5", "price": 2.60},
                {"id": 31, "name": "Menos de 5.5", "price": 1.45},
            ],
        })
        sc = ScraperFake(listagem(), det)
        asyncio.run(sc._scrape_mr12_for_sport(config.SPORTS[0]))
        cartoes = [m for m in sc.mercados_vistos if m.market_nome == "Total de Cartões"]
        self.assertEqual(len({m.market_id for m in cartoes}), 2)

    def test_mr12_reset_entre_ciclos(self):
        """`scrape()` roda no MESMO objeto `BetanoScraper` a cada ciclo
        (`main.py` abre um só, no `while True`) — sem resetar,
        `mercados_vistos` cresceria sem teto e regravaria ciclos antigos."""
        sc = ScraperFake(listagem(), detalhe())
        asyncio.run(sc.scrape())
        primeira_visita = len(sc.mercados_vistos)
        self.assertGreater(primeira_visita, 0)
        asyncio.run(sc.scrape())
        self.assertEqual(len(sc.mercados_vistos), primeira_visita)

    def test_evento_sem_detalhe_nao_quebra(self):
        """Sem `url`, não há request de detalhe — a oferta MR12 ainda sai,
        só sem mercado nenhum pro consenso (mesma política do `originals`)."""
        listagem_sem_url = listagem()
        del listagem_sem_url["data"]["blocks"][0]["events"][0]["url"]
        sc = ScraperFake(listagem_sem_url, detalhe())
        offers = asyncio.run(sc._scrape_mr12_for_sport(config.SPORTS[0]))
        self.assertTrue(offers)
        self.assertEqual(sc.mercados_vistos, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
