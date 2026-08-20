"""Testes do scraper da Lottu (`/event/highlights`, ngbras).

O fixture abaixo é uma captura REAL de `GET
https://alpha-sb.ngbras.com/event/highlights` com `Origin:
https://www.lottu.bet.br`, feita ao vivo em 2026-08-20 (fora do scraper, com
`curl_cffi` puro) — não é um dict inventado. Campos decorativos irrelevantes
pro parser (`custom_bet`, `live_stream`, `market_config`, `video_url` etc.)
foram cortados pra não inflar o fixture.

Ver `ADAPTER_CONTRACT.md` §7.3 pro checklist que estes testes cobrem.
"""

from __future__ import annotations

import unittest

from betano_superodds.lottu import CASA, LottuScraper
from betano_superodds.scraper import ScraperError


class ScraperFake(LottuScraper):
    """Sobrescreve só o seam de rede (`_get_json`), como o contrato exige."""

    def __init__(self, payload: object) -> None:
        super().__init__(session=object())  # sessão nunca usada de verdade
        self._payload = payload

    async def _get_json(self) -> object:
        return self._payload


def highlight_athletic_crb() -> dict:
    """Combo real de 2 pernas — captura de 2026-08-20."""
    return {
        "_id": "6a85eb9e1d02191929637786",
        "status": "NOT_STARTED",
        "date": "2026-08-20T22:29:50.000Z",
        "country": "Brasil",
        "championship": "Odds Turbinadas - Brasileiro Série B",
        "question": "Athletic x CRB",
        "group": "oddsturbinadas",
        "title": "PRESSÃO ATHLETICANA!",
        "value": 0,
        "valid_odds": 1,
        "odds": {
            "answers": [
                {
                    "_id": "6a85eb92196f61122f8e8d0d",
                    "enable": True,
                    "answer_list": [
                        "Athletic Para Ganhar Um Dos Tempos",
                        "Athletic Para Ter o Maior Número de Escanteios",
                    ],
                    "answer": (
                        "Athletic Para Ganhar Um Dos Tempos & Athletic Para"
                        " Ter o Maior Número de Escanteios"
                    ),
                    "value": 4.75,
                    "old_value": 3.97,
                    "order": 0,
                }
            ]
        },
        "widget_ids": [],
    }


class TestPayloadFeliz(unittest.IsolatedAsyncioTestCase):
    async def test_gera_a_oferta_esperada(self):
        ofertas = await ScraperFake([highlight_athletic_crb()]).scrape()

        self.assertEqual(len(ofertas), 1)
        o = ofertas[0]
        self.assertEqual(o.casa, CASA)
        self.assertEqual(o.fonte, "lottu_highlight")
        self.assertEqual(o.evento_id, "6a85eb9e1d02191929637786")
        self.assertEqual(o.evento, "Athletic x CRB")
        self.assertEqual(
            o.mercado,
            "Athletic Para Ganhar Um Dos Tempos + Athletic Para Ter o Maior"
            " Número de Escanteios",
        )
        self.assertEqual(o.odd_original, 3.97)
        self.assertEqual(o.odd_boost, 4.75)
        self.assertEqual(o.liga, "Odds Turbinadas - Brasileiro Série B")
        self.assertAlmostEqual(o.boost_pct, 19.6, places=1)
        # Só uma data no payload (kickoff): replicada nos dois campos, mesma
        # limitação documentada em ADAPTER_CONTRACT.md §2.4 pra outros
        # adapters que só têm uma data disponível.
        self.assertEqual(o.valido_ate, "2026-08-20T22:29:50.000Z")
        self.assertEqual(o.inicio_evento, "2026-08-20T22:29:50+00:00")

    async def test_evento_ja_vem_no_formato_que_o_matcher_reconhece(self):
        """`question` vem "Time A x Time B" — um dos separadores nativos de
        `matcher.split_times` (` x `), diferente da Altenar (`"A vs. B"`,
        precisa normalizar). Não deveria precisar de tratamento aqui."""
        from odds_service.matcher import split_times

        ofertas = await ScraperFake([highlight_athletic_crb()]).scrape()
        times = split_times(ofertas[0].evento)
        self.assertIsNotNone(times)

    async def test_group_casadinhas_turbinadas_tambem_e_lido(self):
        """Segundo tipo visto na sondagem (`CasadinhasTurbinadas`), mesmo
        formato de odds — tratado como a mesma família de oferta."""
        item = highlight_athletic_crb()
        item["group"] = "CasadinhasTurbinadas"
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(len(ofertas), 1)


class TestInvariantes(unittest.IsolatedAsyncioTestCase):
    async def test_odd_boost_menor_ou_igual_a_original_e_descartada(self):
        item = highlight_athletic_crb()
        item["odds"]["answers"][0].update(value=3.97, old_value=4.75)
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_odd_boost_igual_a_original_e_descartada(self):
        item = highlight_athletic_crb()
        item["odds"]["answers"][0].update(value=3.97, old_value=3.97)
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_campos_invertidos_nao_viram_edge_invertido(self):
        """Se a API mandar `old_value` > `value` (rótulos trocados), a
        oferta é descartada — não a `Offer` sai com o boost menor que o
        original. Invariante #1/#3 do contrato."""
        item = highlight_athletic_crb()
        item["odds"]["answers"][0].update(value=3.00, old_value=5.00)
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_sem_evento_id_e_descartada(self):
        item = highlight_athletic_crb()
        del item["_id"]
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_sem_question_e_descartada(self):
        item = highlight_athletic_crb()
        del item["question"]
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_sem_resposta_com_odds_e_descartada(self):
        item = highlight_athletic_crb()
        item["odds"]["answers"] = []
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_odds_nao_numericas_nao_quebram_o_scraper(self):
        item = highlight_athletic_crb()
        item["odds"]["answers"][0].update(value="—", old_value=3.97)
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_pernas_usam_o_separador_do_parser_mesmo_a_api_separando_com_e_comercial(self):
        """A Lottu junta as pernas com "&" no campo `answer` — o adapter usa
        `answer_list` (já separado) e o `SEPARADOR_PERNAS` do projeto, não o
        "&" cru. Mesma armadilha documentada pra SportingTech em
        ADAPTER_CONTRACT.md §2.3."""
        from betano_superodds.value.market_parser import SEPARADOR_PERNAS

        ofertas = await ScraperFake([highlight_athletic_crb()]).scrape()
        self.assertIn(SEPARADOR_PERNAS, ofertas[0].mercado)
        self.assertNotIn(" & ", ofertas[0].mercado)

    async def test_fallback_pra_answer_quando_answer_list_ausente(self):
        item = highlight_athletic_crb()
        item["odds"]["answers"][0]["answer_list"] = []
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(len(ofertas), 1)
        self.assertEqual(ofertas[0].mercado, item["odds"]["answers"][0]["answer"])

    async def test_boas_vindas_nao_se_aplica_a_esta_casa(self):
        """A Lottu não distingue boost de boas-vindas/conta nova no payload
        de highlights (não existe campo tipo `isWelcome`) — diferente da
        Altenar. ADAPTER_CONTRACT.md §5.4 só exige o filtro "se a casa
        distinguir isso no payload"; aqui não há o que filtrar."""
        item = highlight_athletic_crb()
        self.assertNotIn("isWelcome", item)


class TestRespostaHttp(unittest.IsolatedAsyncioTestCase):
    """Testa o `_get_json` de verdade (não o fake), simulando a sessão HTTP —
    a parte que o seam do `ScraperFake` propositalmente não exercita."""

    class _FakeResponse:
        def __init__(self, status_code: int, body: object = None, raise_on_json: bool = False):
            self.status_code = status_code
            self._body = body
            self._raise_on_json = raise_on_json

        def json(self):
            if self._raise_on_json:
                raise ValueError("Expecting value: line 1 column 1 (char 0)")
            return self._body

    class _FakeSession:
        def __init__(self, response):
            self._response = response

        async def get(self, url):
            return self._response

    def _scraper_com_sessao(self, response) -> LottuScraper:
        return LottuScraper(session=self._FakeSession(response))

    async def test_http_nao_200_vira_scraper_error(self):
        scraper = self._scraper_com_sessao(self._FakeResponse(400))
        with self.assertRaises(ScraperError):
            await scraper.scrape()

    async def test_json_invalido_vira_scraper_error_com_mensagem_clara(self):
        scraper = self._scraper_com_sessao(self._FakeResponse(200, raise_on_json=True))
        with self.assertRaises(ScraperError) as ctx:
            await scraper.scrape()
        self.assertIn("não devolveu JSON", str(ctx.exception))

    async def test_falha_de_rede_vira_scraper_error(self):
        from curl_cffi.requests.exceptions import RequestException

        class _SessaoQuebrada:
            async def get(self, url):
                raise RequestException("timeout")

        scraper = LottuScraper(session=_SessaoQuebrada())
        with self.assertRaises(ScraperError):
            await scraper.scrape()


class TestPayloadVazioOuMalformado(unittest.IsolatedAsyncioTestCase):
    async def test_lista_vazia_e_sucesso_vazio_nao_erro(self):
        ofertas = await ScraperFake([]).scrape()
        self.assertEqual(ofertas, [])

    async def test_none_nao_derruba_o_scraper(self):
        ofertas = await ScraperFake(None).scrape()
        self.assertEqual(ofertas, [])

    async def test_item_sem_chave_odds_nao_derruba_o_scraper(self):
        item = highlight_athletic_crb()
        del item["odds"]
        ofertas = await ScraperFake([item]).scrape()
        self.assertEqual(ofertas, [])

    async def test_varios_itens_um_invalido_nao_derruba_os_outros(self):
        bom = highlight_athletic_crb()
        ruim = highlight_athletic_crb()
        ruim["_id"] = ""
        ofertas = await ScraperFake([ruim, bom]).scrape()
        self.assertEqual(len(ofertas), 1)
        self.assertEqual(ofertas[0].evento_id, "6a85eb9e1d02191929637786")


if __name__ == "__main__":
    unittest.main()
