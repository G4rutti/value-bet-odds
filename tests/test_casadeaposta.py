"""Testes do scraper da CasaDeAposta (widget de promoções do CMS zizy).

O payload de exemplo abaixo é uma captura REAL do
`GET https://zizy-cms.casadeapostas.tv/api/home/widget/promocoes?userId=0&lg=1`,
feita ao vivo em 2026-08-05 (fora do scraper, com `curl_cffi` puro) — não é
um dict inventado. Só os campos irrelevantes pro parser (blocos `casino`,
`aovivo`, decoração visual como `event_bg`/`timer`) foram cortados pra não
inflar o fixture; a estrutura de `sports[i].event`/`championship` é a
resposta de verdade.

Ver `ADAPTER_CONTRACT.md` §7.3 pro checklist que estes testes cobrem.
"""

from __future__ import annotations

import unittest

from betano_superodds.casadeaposta import CASA, CasaDeApostaScraper
from betano_superodds.scraper import ScraperError


class ScraperFake(CasaDeApostaScraper):
    """Sobrescreve só o seam de rede (`_get_json`), como o contrato exige."""

    def __init__(self, payload: dict) -> None:
        super().__init__(session=object())  # sessão nunca usada de verdade
        self._payload = payload

    async def _get_json(self, url: str, params: dict) -> dict:
        return self._payload


def item_fortaleza_palmeiras() -> dict:
    """Combo real de 3 pernas (`option`/`option1`/`option2`).

    Revalidado em 2026-08-05: o payload real traz até três pernas por item,
    não duas — `_parse_item` só lia `option`/`option1` e descartava a
    terceira em silêncio, gravando um `mercado` que não era a combinada real
    que a odd precifica. Corrigido; este fixture tem as três."""
    return {
        "type": "comum",
        "title": "SUPER ODDS",
        "championship": {
            "label": "⚽ COPA DO BRASIL",
            "date": "2026-08-05 21:30:00",
            "linkLeague": "https://casadeapostas.bet.br/br/sports/league/2278-Copa-do-Brasil",
        },
        "event": {
            "team1": "Fortaleza",
            "team2": "Palmeiras",
            "teamSeparator": "X",
            "originalOdds": "5.50",
            "boostedOdds": "7.00",
            "gameLink": "https://casadeapostas.bet.br/br/sports/event/8109029-Fortaleza-CE-vs-Palmeiras-SP",
            "option": "Dupla chance",
            "choice": "Fortaleza ou empate",
            "option1": "Vencedor  (1º tempo)",
            "choice1": "Empate",
            "option2": "Total de gols (2º tempo)",
            "choice2": "Menos de 1.5",
        },
    }


def item_sem_event_id() -> dict:
    """`gameLink` sem `/event/<id>` — captura real de link decorativo (`#`)
    que a home usa pros blocos de cassino; um combo esportivo nunca deveria
    vir assim, mas o parser precisa sobreviver caso o CMS publique um."""
    item = item_fortaleza_palmeiras()
    item["event"] = dict(item["event"], gameLink="https://casadeapostas.bet.br/sports")
    return item


def payload(*items: dict) -> dict:
    return {"casino": [], "aovivo": [], "multipla_da_casa": [], "sports": list(items)}


class TestPayloadFeliz(unittest.IsolatedAsyncioTestCase):
    async def test_gera_a_oferta_esperada(self):
        scraper = ScraperFake(payload(item_fortaleza_palmeiras()))
        ofertas = await scraper.scrape()

        self.assertEqual(len(ofertas), 1)
        o = ofertas[0]
        self.assertEqual(o.casa, CASA)
        self.assertEqual(o.fonte, "casadeaposta_combo")
        self.assertEqual(o.evento, "Fortaleza - Palmeiras")
        self.assertEqual(o.evento_id, "8109029")
        self.assertEqual(
            o.mercado,
            "Dupla chance: Fortaleza ou empate + Vencedor  (1º tempo): Empate"
            " + Total de gols (2º tempo): Menos de 1.5")
        self.assertEqual(o.odd_original, 5.50)
        self.assertEqual(o.odd_boost, 7.00)
        self.assertEqual(
            o.url,
            "https://casadeapostas.bet.br/br/sports/event/8109029-Fortaleza-CE-vs-Palmeiras-SP",
        )
        self.assertEqual(o.liga, "⚽ COPA DO BRASIL")
        self.assertAlmostEqual(o.boost_pct, 27.3, places=1)
        # Só uma data no payload: replicada nos dois campos (mesma limitação
        # documentada em ADAPTER_CONTRACT.md §2.4 pra novibet/sportingtech).
        # `valido_ate` fica como o adapter grava (-03:00); `inicio_evento` é
        # normalizado pra UTC dentro de `Offer.__post_init__` (`to_utc_iso`),
        # por isso os dois campos têm strings diferentes aqui apesar de
        # virem da mesma data de origem.
        self.assertEqual(o.valido_ate, "2026-08-05T21:30:00-03:00")
        self.assertEqual(o.inicio_evento, "2026-08-06T00:30:00+00:00")


class TestInvariantes(unittest.IsolatedAsyncioTestCase):
    async def test_odd_boost_menor_ou_igual_a_original_e_descartada(self):
        item = item_fortaleza_palmeiras()
        item["event"] = dict(item["event"], originalOdds="7.00", boostedOdds="5.50")
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(ofertas, [])

    async def test_odd_boost_igual_a_original_e_descartada(self):
        item = item_fortaleza_palmeiras()
        item["event"] = dict(item["event"], originalOdds="5.50", boostedOdds="5.50")
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(ofertas, [])

    async def test_campos_invertidos_nao_viram_edge_invertido(self):
        """Se o CMS mandar `originalOdds` > `boostedOdds` (rótulos trocados),
        a oferta é descartada — não a `Offer` sai com o boost menor que o
        original. É o comportamento seguro exigido pela invariante #1/#3 do
        contrato: nunca confiar cegamente na ordem/rótulo dos campos."""
        item = item_fortaleza_palmeiras()
        item["event"] = dict(item["event"], originalOdds="8.00", boostedOdds="6.00")
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(ofertas, [])

    async def test_sem_evento_id_reconhecivel_e_descartada(self):
        ofertas = await ScraperFake(payload(item_sem_event_id())).scrape()
        self.assertEqual(ofertas, [])

    async def test_sem_perna_reconhecivel_e_descartada(self):
        item = item_fortaleza_palmeiras()
        item["event"] = {k: v for k, v in item["event"].items()
                          if k not in ("option", "choice", "option1", "choice1",
                                       "option2", "choice2")}
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(ofertas, [])

    async def test_terceira_perna_e_lida(self):
        """Regressão: `option2`/`choice2` era descartada em silêncio — o
        `mercado` gravado não era a combinada real que a odd precifica."""
        item = item_fortaleza_palmeiras()
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertIn("Total de gols (2º tempo): Menos de 1.5", ofertas[0].mercado)

    async def test_falta_so_a_terceira_perna_ainda_gera_oferta(self):
        """Duas pernas bastam — a terceira é aditiva, não obrigatória."""
        item = item_fortaleza_palmeiras()
        item["event"] = {k: v for k, v in item["event"].items()
                          if k not in ("option2", "choice2")}
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(len(ofertas), 1)
        self.assertNotIn("Total de gols", ofertas[0].mercado)

    async def test_odds_nao_numericas_nao_quebram_o_scraper(self):
        item = item_fortaleza_palmeiras()
        item["event"] = dict(item["event"], originalOdds="—", boostedOdds="7.00")
        ofertas = await ScraperFake(payload(item)).scrape()
        self.assertEqual(ofertas, [])

    async def test_pernas_usam_o_separador_do_parser(self):
        from betano_superodds.value.market_parser import SEPARADOR_PERNAS
        ofertas = await ScraperFake(payload(item_fortaleza_palmeiras())).scrape()
        self.assertIn(SEPARADOR_PERNAS, ofertas[0].mercado)

    async def test_boas_vindas_nao_se_aplica_a_esta_casa(self):
        """A CasaDeAposta não distingue boost de boas-vindas no payload (não
        existe campo tipo `isWelcome` no widget de promoções) — diferente da
        Altenar. ADAPTER_CONTRACT.md §5.4 só exige o filtro "se a casa
        distinguir isso no payload"; aqui não há o que filtrar. Este teste
        documenta a ausência em vez de simular um campo que a API real não
        manda."""
        item = item_fortaleza_palmeiras()
        self.assertNotIn("isWelcome", item["event"])


class TestRespostaHttp(unittest.IsolatedAsyncioTestCase):
    """Testa o `_get_json` de verdade (não o fake), simulando a sessão HTTP —
    é a parte que o seam do `ScraperFake` propositalmente não exercita."""

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

        async def get(self, url, params=None):
            return self._response

    def _scraper_com_sessao(self, response) -> CasaDeApostaScraper:
        scraper = CasaDeApostaScraper(session=self._FakeSession(response))
        return scraper

    async def test_http_nao_200_vira_scraper_error(self):
        scraper = self._scraper_com_sessao(self._FakeResponse(503))
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
            async def get(self, url, params=None):
                raise RequestException("timeout")

        scraper = CasaDeApostaScraper(session=_SessaoQuebrada())
        with self.assertRaises(ScraperError):
            await scraper.scrape()


class TestPayloadVazioOuMalformado(unittest.IsolatedAsyncioTestCase):
    async def test_dict_vazio_nao_derruba_o_scraper(self):
        ofertas = await ScraperFake({}).scrape()
        self.assertEqual(ofertas, [])

    async def test_sports_vazio_e_sucesso_vazio_nao_erro(self):
        ofertas = await ScraperFake(payload()).scrape()
        self.assertEqual(ofertas, [])

    async def test_sports_none_nao_derruba_o_scraper(self):
        ofertas = await ScraperFake({"sports": None}).scrape()
        self.assertEqual(ofertas, [])

    async def test_item_sem_chave_event_nao_derruba_o_scraper(self):
        ofertas = await ScraperFake(payload({"championship": {}})).scrape()
        self.assertEqual(ofertas, [])

    async def test_varios_itens_um_invalido_nao_derruba_os_outros(self):
        bom = item_fortaleza_palmeiras()
        ruim = item_sem_event_id()
        ofertas = await ScraperFake(payload(ruim, bom)).scrape()
        self.assertEqual(len(ofertas), 1)
        self.assertEqual(ofertas[0].evento_id, "8109029")


if __name__ == "__main__":
    unittest.main()
