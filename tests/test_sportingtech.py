"""Testes do scraper SportingTech (EsportesDaSorte).

Antes deste arquivo o adapter tinha ZERO teste de regressão (ver
ADAPTER_CONTRACT.md §7). Os payloads abaixo são um recorte de uma captura REAL
feita em 2026-08-05 contra `esportesdasorte.bet.br` (`SportingTechScraper`
ainda ao vivo, 9 ofertas retornadas nesse dia) — não são inventados. Foram só
reduzidos a poucos jogos e comentados com o "porquê" de cada campo, seguindo
`ADAPTER_CONTRACT.md §7.2`.

Nota sobre o item 4 do checklist do contrato ("promoção de boas-vindas
filtrada, SE a casa distinguir isso"): o payload real da SportingTech (visto
acima) não carrega nenhum campo tipo `isWelcome`/`isNew` em nenhum nível
(`fos`/`btgs`/`fs`) — diferente da Altenar. Não há o que filtrar aqui porque a
casa não distingue; por isso este arquivo não tem um teste para esse item,
igual a novibet.py/casadeaposta.py.
"""

from __future__ import annotations

import asyncio
import unittest
from typing import Any

from curl_cffi.requests.exceptions import RequestException

from betano_superodds.scraper import ScraperError
from betano_superodds.sportingtech import (
    CASAS_SPORTINGTECH,
    CasaSportingTech,
    SportingTechScraper,
    _normalizar_perna,
)
from betano_superodds.value.market_parser import SEPARADOR_PERNAS


# --- seam de teste (ADAPTER_CONTRACT.md §7.1) --------------------------------

class ScraperFake(SportingTechScraper):
    """Troca a rede por um payload fixo — sobrescreve só `_get_json`.

    `scrape()`/`_parse_combo` continuam sendo o código real; só o ponto de
    entrada de rede muda, exatamente o seam que `ADAPTER_CONTRACT.md` exige.
    """

    def __init__(self, payload: dict, casa: CasaSportingTech | None = None) -> None:
        super().__init__(casa or CASAS_SPORTINGTECH[0], session=object())  # type: ignore[arg-type]
        self._payload = payload

    async def _get_json(self, url: str) -> Any:
        return self._payload


class FakeResponse:
    """Simula o objeto de resposta do curl_cffi só no que `_get_json` usa."""

    def __init__(self, status_code: int = 200, payload: Any = None, json_quebrado: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_quebrado = json_quebrado

    def json(self) -> Any:
        if self._json_quebrado:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._payload


class FakeSession:
    """Substitui `curl_requests.AsyncSession` só no método `get`."""

    def __init__(self, response: FakeResponse | None = None, excecao: Exception | None = None) -> None:
        self._response = response
        self._excecao = excecao

    async def get(self, url: str, **kwargs: Any) -> FakeResponse:
        if self._excecao is not None:
            raise self._excecao
        assert self._response is not None
        return self._response


def _scrape_com_sessao(response: FakeResponse | None = None, excecao: Exception | None = None):
    casa = CASAS_SPORTINGTECH[0]
    scraper = SportingTechScraper(casa, session=FakeSession(response, excecao))  # type: ignore[arg-type]
    return asyncio.run(scraper.scrape())


# --- fixture: recorte real capturado em 2026-08-05 ---------------------------

def _combo(fo_id: int, bt_id: int, texto_com_era: str, hO: float, tm_id: int) -> dict:
    return {
        "foId": fo_id, "btId": bt_id, "btN": texto_com_era, "valid": True,
        "tvalid": True, "freeze": False, "prm": False, "lineItem": False,
        "hO": hO, "hSh": texto_com_era, "tmId": tm_id, "foCe": True,
        "btCe": True, "taCe": False,
    }


def payload_real() -> dict:
    """Recorte de 2 jogos da captura real — cobre "&" e "|" como separador de
    perna e o combo-de-1º-tempo que motivou o ajuste de `_GOLS_BARE_RE` hoje.
    """
    return {
        "success": True,
        "responseCodes": [],
        "data": [{
            "stId": 712, "stN": "Super Odds",
            "cs": [{
                "cId": 10003155, "cN": "Futebol",
                "sns": [{
                    "sId": 841741, "seaN": "Combinadas", "seaSURL": "multis",
                    "fs": [
                        {
                            # Jogo real: Benfica x Hearts. `fsd` é epoch ms —
                            # único dado de data que a casa manda, por isso
                            # vira valido_ate E inicio_evento (ADAPTER_CONTRACT
                            # §2.4, mesma limitação documentada de
                            # novibet/casadeaposta).
                            "fId": 75892784, "fsd": 1786042800000,
                            "hcN": "Benfica x Hearts",
                            "btgs": [{
                                "btgId": 115267, "btgN": "Combinada",
                                "fos": [
                                    # Pernas separadas por "|" na API real —
                                    # sem a troca por " + " (SEPARADOR_PERNAS)
                                    # isto viraria mercado simples e casaria
                                    # contra a odd justa de só uma perna
                                    # (edge inflado, o mesmo bug de classe do
                                    # Mirassol — ver sportingtech.py:79-84).
                                    _combo(
                                        9985022298, 2480166,
                                        "Intervalo/Final - Benfica/Benfica | "
                                        "Mais de 3.5 Gols | Mais de 9.5 Escanteios (Era 3.4)",
                                        4.3, 648765,
                                    ),
                                    # Pernas separadas por "&". A segunda perna
                                    # é "bare" gols de 1º tempo — grafia nova
                                    # achada ao vivo hoje que motivou estender
                                    # `_GOLS_BARE_RE` (ver comentário no
                                    # próprio sportingtech.py).
                                    _combo(
                                        9985022299, 2480167,
                                        "Benfica Para Marcar em Ambos os Tempos & "
                                        "Benfica Mais de 4.5 Escanteios no 1º Tempo (Era 4.1)",
                                        4.8, 648765,
                                    ),
                                ],
                            }],
                        },
                        {
                            # Jogo real: Vitória x Athletico-PR — traz o combo
                            # de 3 pernas "Mais de 0.5 Gols no 1º Tempo | ..."
                            # que É o formato bare-gols-com-período.
                            "fId": 75892785, "fsd": 1786057200000,
                            "hcN": "Vitória x Athletico-PR",
                            "btgs": [{
                                "btgId": 115267, "btgN": "Combinada",
                                "fos": [
                                    _combo(
                                        9985022303, 2480171,
                                        "Mais de 0.5 Gols no 1º Tempo | "
                                        "Mais de 4.5 Escanteios no 1º Tempo | "
                                        "Mais de 0.5 Cartões no 1º Tempo (Era 3.65)",
                                        4.9, 648766,
                                    ),
                                ],
                            }],
                        },
                    ],
                }],
            }],
        }],
    }


class TestPayloadFeliz(unittest.TestCase):
    """Item 1 do checklist: payload feliz gera as `Offer` esperadas."""

    def setUp(self) -> None:
        self.ofertas = asyncio.run(ScraperFake(payload_real()).scrape())

    def test_quantidade_de_ofertas(self):
        self.assertEqual(len(self.ofertas), 3)

    def test_evento_no_formato_que_o_matcher_espera(self):
        # "Time x Time" já está num separador reconhecido por
        # `matcher.SEPARADORES` (" x " está na tupla) — não precisa de
        # conversão, diferente da Altenar com " vs. ".
        eventos = {o.evento for o in self.ofertas}
        self.assertEqual(eventos, {"Benfica x Hearts", "Vitória x Athletico-PR"})

    def test_evento_id_e_o_fid_da_casa(self):
        for o in self.ofertas:
            self.assertIn(o.evento_id, ("75892784", "75892785"))

    def test_pernas_usam_o_separador_do_parser_mesmo_vindo_de_pipe(self):
        oferta = next(o for o in self.ofertas if o.odd_boost == 4.3)
        self.assertEqual(
            oferta.mercado,
            f"Intervalo/Final - Benfica/Benfica{SEPARADOR_PERNAS}"
            f"Total de Gols: Mais de 3.5 Gols{SEPARADOR_PERNAS}"
            f"Mais de 9.5 Escanteios",
        )

    def test_pernas_usam_o_separador_do_parser_mesmo_vindo_de_e_comercial(self):
        oferta = next(o for o in self.ofertas if o.odd_boost == 4.8)
        pernas = oferta.mercado.split(SEPARADOR_PERNAS)
        self.assertEqual(len(pernas), 2)
        self.assertNotIn("&", oferta.mercado)

    def test_bare_gols_de_1o_tempo_ganha_o_nome_do_mercado(self):
        """Regressão de hoje: sem estender `_GOLS_BARE_RE`, "Mais de 0.5 Gols
        no 1º Tempo" ficava sem o prefixo "Total de Gols:" e o market_parser
        não reconhecia o mercado (não tem a palavra "total" na frase nenhuma
        vez)."""
        oferta = next(o for o in self.ofertas if o.odd_boost == 4.9)
        self.assertIn("Total de Gols: Mais de 0.5 Gols no 1º Tempo", oferta.mercado)

    def test_odds_e_ganho(self):
        oferta = next(o for o in self.ofertas if o.odd_boost == 4.3)
        self.assertEqual(oferta.odd_original, 3.4)
        self.assertEqual(oferta.odd_boost, 4.3)
        self.assertAlmostEqual(oferta.boost_pct, 26.5, places=1)

    def test_valido_ate_e_inicio_evento_replicam_a_unica_data_disponivel(self):
        # ADAPTER_CONTRACT §2.4: a SportingTech só manda uma data (`fsd`), tem
        # que replicar nos dois campos, não inventar precisão que não existe.
        oferta = next(o for o in self.ofertas if o.odd_boost == 4.3)
        self.assertIsNotNone(oferta.valido_ate)
        self.assertEqual(oferta.valido_ate, oferta.inicio_evento)

    def test_url_aponta_para_a_pagina_de_super_odds_da_casa(self):
        for o in self.ofertas:
            self.assertEqual(o.url, CASAS_SPORTINGTECH[0].pagina_super_odds)

    def test_casa_e_fonte(self):
        for o in self.ofertas:
            self.assertEqual(o.casa, "EsportesDaSorte")
            self.assertEqual(o.fonte, "sportingtech_super_odds")


class TestInvariantes(unittest.TestCase):
    """Itens 2, 3 e 5 do checklist — todos com bug real por trás."""

    def _payload_com_combo(self, combo: dict, fid: Any = 75892785) -> dict:
        return {
            "success": True,
            "data": [{"cs": [{"cN": "Futebol", "sns": [{
                "fs": [{
                    "fId": fid, "fsd": 1786057200000, "hcN": "Vitória x Athletico-PR",
                    "btgs": [{"fos": [combo]}],
                }],
            }]}]}],
        }

    def test_odd_boost_menor_ou_igual_a_original_e_descartada(self):
        # Item 2 e item 5 ao mesmo tempo: para a SportingTech não há "dois
        # campos que podem vir invertidos" (só existe `hO`, o resto é extraído
        # do texto via `_ERA_RE`) — a garantia de "sempre a maior" É este
        # guard. Um payload real nunca deveria mandar isto, mas se mandasse
        # (glitch/erro de arredondamento da casa), não pode virar oferta.
        combo = _combo(1, 1, "Mais de 2.5 Gols (Era 4.0)", 3.9, 1)
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo)).scrape())
        self.assertEqual(ofertas, [])

    def test_odd_boost_igual_a_original_tambem_e_descartada(self):
        combo = _combo(1, 1, "Mais de 2.5 Gols (Era 4.0)", 4.0, 1)
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo)).scrape())
        self.assertEqual(ofertas, [])

    def test_sem_evento_id_a_oferta_e_descartada_nao_quebra(self):
        # Item 3: `fId` ausente/falsy -> `_parse_combo` devolve None, o resto
        # do payload continua sendo processado sem exceção.
        combo = _combo(1, 1, "Mais de 2.5 Gols (Era 3.0)", 4.0, 1)
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo, fid=None)).scrape())
        self.assertEqual(ofertas, [])

    def test_sem_evento_id_zero_tambem_e_descartada(self):
        combo = _combo(1, 1, "Mais de 2.5 Gols (Era 3.0)", 4.0, 1)
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo, fid=0)).scrape())
        self.assertEqual(ofertas, [])

    def test_combo_sem_era_reconhecivel_e_descartado(self):
        combo = _combo(1, 1, "Mais de 2.5 Gols (sem era nenhuma)", 4.0, 1)
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo)).scrape())
        self.assertEqual(ofertas, [])

    def test_combo_sem_ho_e_descartado(self):
        combo = _combo(1, 1, "Mais de 2.5 Gols (Era 3.0)", None, 1)  # type: ignore[arg-type]
        ofertas = asyncio.run(ScraperFake(self._payload_com_combo(combo)).scrape())
        self.assertEqual(ofertas, [])


class TestSucessoVazioENaoQuebra(unittest.TestCase):
    """Itens 8 e 9 do checklist."""

    def test_no_data_found_vira_lista_vazia_nao_erro(self):
        payload = {
            "success": False,
            "responseCodes": [{"responseCode": 13, "responseKey": "NO_DATA_FOUND",
                                "responseMessage": "No data found", "params": None}],
            "data": [], "type": None,
        }
        ofertas = asyncio.run(ScraperFake(payload).scrape())
        self.assertEqual(ofertas, [])

    def test_dict_vazio_nao_quebra(self):
        ofertas = asyncio.run(ScraperFake({}).scrape())
        self.assertEqual(ofertas, [])

    def test_success_sem_data_nao_quebra(self):
        ofertas = asyncio.run(ScraperFake({"success": True}).scrape())
        self.assertEqual(ofertas, [])

    def test_estrutura_aninhada_vazia_nao_quebra(self):
        payload = {"success": True, "data": [{"cs": [{"sns": [{"fs": []}]}]}]}
        ofertas = asyncio.run(ScraperFake(payload).scrape())
        self.assertEqual(ofertas, [])

    def test_fixture_sem_btgs_nao_quebra(self):
        payload = {"success": True, "data": [{"cs": [{"sns": [{
            "fs": [{"fId": 1, "hcN": "A x B"}],
        }]}]}]}
        ofertas = asyncio.run(ScraperFake(payload).scrape())
        self.assertEqual(ofertas, [])

    def test_grupo_sem_fos_nao_quebra(self):
        payload = {"success": True, "data": [{"cs": [{"sns": [{
            "fs": [{"fId": 1, "hcN": "A x B", "btgs": [{}]}],
        }]}]}]}
        ofertas = asyncio.run(ScraperFake(payload).scrape())
        self.assertEqual(ofertas, [])


class TestTransporte(unittest.TestCase):
    """Itens 7 e 10 do checklist — exercitam o `_get_json` real (não o seam de
    payload), porque a lógica que converte erro de transporte em `ScraperError`
    mora exatamente aí (ADAPTER_CONTRACT.md §4)."""

    def test_http_nao_200_vira_scraper_error(self):
        with self.assertRaises(ScraperError):
            _scrape_com_sessao(response=FakeResponse(status_code=403))

    def test_http_500_vira_scraper_error(self):
        with self.assertRaises(ScraperError):
            _scrape_com_sessao(response=FakeResponse(status_code=500))

    def test_json_invalido_vira_scraper_error_com_mensagem_clara(self):
        # Distingue bloqueio de WAF (HTML no lugar de JSON) de "0 oferta hoje".
        with self.assertRaises(ScraperError):
            _scrape_com_sessao(response=FakeResponse(status_code=200, json_quebrado=True))

    def test_falha_de_rede_vira_scraper_error(self):
        with self.assertRaises(ScraperError):
            _scrape_com_sessao(excecao=RequestException("timeout"))


class TestNormalizarPerna(unittest.TestCase):
    """`_normalizar_perna`/`_GOLS_BARE_RE` isolados — a normalização que o
    market_parser depende para reconhecer o total de gols do jogo quando a
    EsportesDaSorte omite a palavra "total"."""

    def test_bare_gols_do_jogo_ganha_o_prefixo(self):
        self.assertEqual(_normalizar_perna("Mais de 2.5 Gols"), "Total de Gols: Mais de 2.5 Gols")

    def test_bare_gols_de_1o_tempo_ganha_o_prefixo(self):
        # Achado ao vivo em 2026-08-05: sem isto a perna nunca tinha "total"
        # em nenhum lugar da frase e morria em "mercado não reconhecido".
        self.assertEqual(
            _normalizar_perna("Mais de 0.5 Gols no 1º Tempo"),
            "Total de Gols: Mais de 0.5 Gols no 1º Tempo",
        )

    def test_bare_gols_de_intervalo_ganha_o_prefixo(self):
        self.assertEqual(
            _normalizar_perna("Menos de 3.5 Gols no Intervalo"),
            "Total de Gols: Menos de 3.5 Gols no Intervalo",
        )

    def test_bare_gols_de_2o_tempo_fica_de_fora_de_proposito(self):
        # `_parse_total_gols._sufixo` dobra qualquer período que não seja 1º
        # tempo em "" (jogo inteiro) — normalizar um bare de 2º tempo casaria
        # o total do 2º tempo contra a referência do jogo inteiro, a mesma
        # classe do incidente do Mirassol. Tem que continuar sem match.
        texto = "Mais de 1.5 Gols no 2º Tempo"
        self.assertEqual(_normalizar_perna(texto), texto)

    def test_gols_com_nome_de_time_fica_de_fora_de_proposito(self):
        texto = "Palmeiras Mais de 1.5 Gols"
        self.assertEqual(_normalizar_perna(texto), texto)

    def test_combo_de_varios_jogos_fica_de_fora_de_proposito(self):
        texto = "Mais de 1.5 Gols em Todas as Partidas"
        self.assertEqual(_normalizar_perna(texto), texto)


class TestCabecalhosObrigatorios(unittest.TestCase):
    """Regressão para o "sucesso vazio enganoso" documentado no topo do
    módulo: sem `origin`/`sec-fetch-*`/`encodedbody` a API responde 200 com
    NO_DATA_FOUND, não um erro — então só dá pra pegar essa regressão
    conferindo os headers, não o comportamento em runtime."""

    def test_headers_criticos_presentes(self):
        casa = CASAS_SPORTINGTECH[0]

        async def cenario():
            async with SportingTechScraper(casa) as s:
                return dict(s._session.headers)

        headers = {k.lower(): v for k, v in asyncio.run(cenario()).items()}
        for chave in ("origin", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
                      "encodedbody", "referer", "customorigin", "bragiurl", "languageid"):
            self.assertIn(chave, headers, f"header {chave!r} sumiu de SportingTechScraper")
        self.assertEqual(headers["origin"], casa.dominio)
        self.assertEqual(headers["customorigin"], casa.dominio)


class TestTabelaDeCasas(unittest.TestCase):
    def test_tabela_de_casas_nao_tem_nome_repetido(self):
        nomes = [c.nome for c in CASAS_SPORTINGTECH]
        self.assertEqual(len(nomes), len(set(nomes)))

    def test_tabela_de_casas_nao_tem_tenant_repetido(self):
        tenants = [c.tenant for c in CASAS_SPORTINGTECH]
        self.assertEqual(len(tenants), len(set(tenants)))

    def test_super_odds_url_usa_o_tenant_da_casa(self):
        casa = CasaSportingTech("Teste", "tenant-teste", "https://exemplo.bet.br")
        self.assertIn("/tenant-teste/", casa.super_odds_url)
        self.assertIn("esportes-super-odds/football/multis", casa.super_odds_url)


if __name__ == "__main__":
    unittest.main()
