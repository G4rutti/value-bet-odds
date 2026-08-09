"""Testes do scraper da Novibet.

Fixtures capturadas ao vivo em 2026-08-05 (curl_cffi puro, fora do scraper,
`impersonate="chrome"`) contra o hub "Odds Turbinadas" de verdade — não são
payloads inventados. `esportes_reais()`/`arvore_real()` são a resposta real
(trimada de campos que o parser não lê, como `imageId`/`regionImageId`) de
`/spt/feed/navigation/coupon/v2/4324/6685003[/<ids>]`; `ticket_combo_real()` e
`ticket_single_perdido()` vêm de um `prebuiltTicket` de verdade do leaf
7197958 ("Brasil - Copa do Brasil"), inclusive os dois bugs de payload
achados na revalidação (emoji 🚀 dentro de `marketInstanceCaption` e a chave
`offer` singular vs `offers` plural) — ver os comentários em cada fixture.
"""

from __future__ import annotations

import asyncio
import unittest

from curl_cffi.requests.exceptions import RequestException

from betano_superodds.models import Offer
from betano_superodds.novibet import (
    BASE_URL,
    CASA,
    HUB_URL,
    ROOT_MENU_ID,
    TURBINADAS_LOCATION_ID,
    NovibetScraper,
)
from betano_superodds.scraper import ScraperError


# --- fixtures: capturas reais -------------------------------------------

def esportes_reais() -> dict:
    """Resposta real de `.../coupon/v2/4324/6685003/` SEM ids de esporte.

    Confirma o que o docstring do scraper documenta: sem os ids ecoados de
    volta, `subItems` vem vazio — só dá pra descobrir QUAIS esportes têm
    turbinada agora, não as competições dentro de cada um.
    """
    return {
        "items": [
            {"marketViewGroupId": 7079633, "marketViewGroupSysname": "ACCUMULATOR_GOALS",
             "caption": "Futebol", "subItems": []},
        ],
    }


def arvore_real() -> dict:
    """Mesma chamada, agora ecoando o id do esporte — as competições aparecem.

    Os 4 ids de competição (7197958, 7158567, 7157063, 7205146) são reais,
    capturados no mesmo momento que os tickets abaixo.
    """
    return {
        "items": [
            {"marketViewGroupId": 7079633, "caption": "Futebol", "subItems": [
                {"marketViewGroupId": 7197958, "caption": "Brasil - Copa do Brasil", "subItems": []},
                {"marketViewGroupId": 7158567, "caption": "Liga Europa", "subItems": []},
                {"marketViewGroupId": 7157063, "caption": "Liga Conferência", "subItems": []},
                {"marketViewGroupId": 7205146, "caption": "Leagues Cup 2026", "subItems": []},
            ]},
        ],
    }


def _header(evento_id: int, c1: str, c2: str, liga: str = "Copa do Brasil") -> dict:
    """Pedaço de `header` comum aos tickets — extraído do payload real."""
    return {
        "fullLocationPath": {"locationPathItems": [
            {"caption": "Brasil", "level": "Region"},
            {"caption": liga, "level": "Competition"},
        ]},
        "additionalCaptions": {"competitor1": c1, "competitor2": c2},
        "betContextId": evento_id,
        "expireTimeUTC": "2026-08-06T23:00:00+00:00",
    }


def ticket_combo_real() -> dict:
    """Combo "BetBuilder" real: Vitória BA - Athletico Paranaense PR, 3.30 -> 3.63.

    ⚠️ `marketInstanceCaption` traz "Resultado Final 🚀" — o emoji (U+1F680)
    é decoração da PRÓPRIA Novibet pra marcar qual perna do combo está
    turbinada, confirmado byte a byte no payload cru (não é mojibake local).
    Ele quebra `value/market_parser._parse_resultado`, cujo regex exige
    `resultado\\s+final\\s*:` — o `\\s*` não cobre o emoji entre "Final" e
    ":", então a perna mais comum e mais simples (1X2) vira "mercado não
    reconhecido" pra praticamente toda oferta da casa. Ver relatório.
    """
    return {
        "betContextId": 47489677, "isSuspended": False, "boostPercentage": 10.0,
        "header": _header(47303097, "Vitória BA", "Athletico Paranaense PR"),
        "price": {"value": 3.3}, "boostedPrice": {"value": 3.63},
        "selections": [{
            "betContextId": 47303097,
            "betContextCaption": "Vitória BA - Athletico Paranaense PR",
            "discriminator": "BetBuilder",
            "offers": [
                {"marketInstanceCaption": "Resultado Final \U0001F680",
                 "betInstanceCaption": "Vitória BA"},
                {"marketInstanceCaption": "Martins Fogaça de Paulο Matheuzinho - Chutes no gol \U0001F680",
                 "betInstanceCaption": "Mais de 0,5"},
            ],
        }],
    }


def ticket_single_perdido() -> dict:
    """Ticket tipo "Single" real: Corinthians SP - Internacional RS, 3.25 -> 3.57.

    ⚠️ GAP CONHECIDO (achado nesta revalidação, 2026-08-05): tickets
    `discriminator: "Single"` (não são combo — aqui é "Múltiplo resultado
    correto", um mercado só) trazem a oferta em `selecao["offer"]`
    (SINGULAR, um dict). `_parse_ticket` só lê `selecao.get("offers")`
    (PLURAL, uma lista — formato dos combos "BetBuilder"). Resultado:
    `pernas` fica vazio e o ticket inteiro é descartado, sem log de aviso
    nenhum. Confirmado ao vivo: dos 6 `prebuiltTickets` reais do leaf
    7197958, só este (o único "Single") sumiu das 47 ofertas do scrape em
    produção — os outros 5 ("BetBuilder") saíram normalmente.
    """
    return {
        "betContextId": 47489677, "isSuspended": False, "boostPercentage": 10.0,
        "header": _header(47303095, "Corinthians SP", "Internacional RS"),
        "price": {"value": 3.25}, "boostedPrice": {"value": 3.57},
        "selections": [{
            "betContextId": 47303095,
            "betContextCaption": "Corinthians SP - Internacional RS",
            "discriminator": "Single",
            "offer": {  # singular -- é isto que `_parse_ticket` não lê
                "marketInstanceCaption": "Múltiplo resultado correto",
                "betInstanceCaption": "1 - 0, 2 - 0, 3 - 0",
            },
        }],
    }


def ticket_multi_jogo() -> dict:
    """Combo multi-jogo sintético, inspirado no "Festival de Gols" real da
    Novibet (2026-08-07): "Mais de 2,5" em jogos DIFERENTES, cada `selecao`
    com seu próprio `betContextId`/`betContextCaption`. 2 jogos bastam pra
    exercitar a detecção (`len(jogos_distintos) > 1`) — o real tinha 3.

    O bug que isto reproduz: o parser antigo pegava só `selections[0]` e
    tratava as 2 pernas como se fossem do mesmo jogo, o que rio abaixo
    (`pipeline.py`/`fair_odds.py`) precificava as duas contra o mesmo
    matchup da Pinnacle.
    """
    return {
        "betContextId": 90000001, "isSuspended": False, "boostPercentage": 10.0,
        "header": _header(90000001, "FC Cincinnati", "Pumas UNAM"),
        "price": {"value": 3.45}, "boostedPrice": {"value": 4.10},
        "selections": [
            {
                "betContextId": 90000002,
                "betContextCaption": "FC Cincinnati - Pumas UNAM",
                "discriminator": "BetBuilder",
                "offers": [
                    {"marketInstanceCaption": "Total de Gols",
                     "betInstanceCaption": "Mais de 2,5"},
                ],
            },
            {
                "betContextId": 90000003,
                "betContextCaption": "Tigres UANL - Minnesota United FC",
                "discriminator": "BetBuilder",
                "offers": [
                    {"marketInstanceCaption": "Total de Gols",
                     "betInstanceCaption": "Mais de 2,5"},
                ],
            },
        ],
    }


def grupo_real(tickets: list[dict]) -> dict:
    return {"caption": "Copa do Brasil", "prebuiltTickets": tickets}


# --- seam de teste (contrato §7.1) --------------------------------------

class ScraperFake(NovibetScraper):
    """Troca a rede por payloads fixos, sobrescrevendo só `_get_json`
    (o único método de rede do scraper) — `scrape()` roda de verdade."""

    def __init__(self, esportes: dict | None = None, arvore: dict | None = None,
                 grupos_por_leaf: dict | None = None) -> None:
        super().__init__(session=object())  # sessão nunca usada de verdade
        self._esportes = esportes if esportes is not None else esportes_reais()
        self._arvore = arvore if arvore is not None else arvore_real()
        self._grupos_por_leaf = grupos_por_leaf or {}
        self._coupon_prefix = (
            f"{BASE_URL}/spt/feed/navigation/coupon/v2/{ROOT_MENU_ID}/{TURBINADAS_LOCATION_ID}"
        )
        self._marketviews_prefix = f"{BASE_URL}/spt/feed/marketviews/location/v2/{ROOT_MENU_ID}/"

    async def _get_json(self, url: str):
        if url == f"{self._coupon_prefix}/":
            return self._esportes
        if url.startswith(self._coupon_prefix):
            return self._arvore
        if url.startswith(self._marketviews_prefix):
            leaf_id = int(url[len(self._marketviews_prefix):].rstrip("/"))
            payload = self._grupos_por_leaf.get(leaf_id, [])
            if isinstance(payload, BaseException):
                raise payload
            return payload
        raise AssertionError(f"URL inesperada no teste: {url}")


class TestNovibetScrape(unittest.TestCase):
    LEAF_COPA_DO_BRASIL = 7197958

    def _rodar(self, esportes=None, arvore=None, grupos=None) -> list[Offer]:
        sc = ScraperFake(esportes=esportes, arvore=arvore, grupos_por_leaf=grupos)
        return asyncio.run(sc.scrape())

    # --- caso 1: payload feliz -------------------------------------------

    def test_payload_feliz_gera_a_oferta_esperada(self):
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_combo_real()])]}
        ofertas = self._rodar(grupos=grupos)
        self.assertEqual(len(ofertas), 1)
        o = ofertas[0]
        self.assertEqual(o.casa, CASA)
        self.assertEqual(o.fonte, "novibet_prebuilt_ticket")
        self.assertEqual(o.evento, "Vitória BA - Athletico Paranaense PR")
        self.assertEqual(o.evento_id, "47303097")
        self.assertEqual(o.liga, "Copa do Brasil")
        self.assertEqual(o.odd_original, 3.3)
        self.assertEqual(o.odd_boost, 3.63)
        self.assertAlmostEqual(o.boost_pct, 10.0)
        self.assertEqual(o.url, HUB_URL)
        self.assertEqual(o.valido_ate, "2026-08-06T23:00:00+00:00")
        self.assertEqual(o.inicio_evento, "2026-08-06T23:00:00+00:00")

    def test_pernas_do_combo_usam_o_separador_do_parser(self):
        """Caso 6 do checklist: a Novibet já entrega as pernas separadas por
        seleção (uma por `offer` dentro do array `offers`); o adapter as
        junta com " + ", que é o que `market_parser.SEPARADOR_PERNAS` espera."""
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_combo_real()])]}
        o = self._rodar(grupos=grupos)[0]
        self.assertEqual(len(o.mercado.split(" + ")), 2)
        self.assertIn("Resultado Final", o.mercado)
        self.assertIn("Chutes no gol", o.mercado)

    # --- caso 2: odd_boost <= odd_original ---------------------------------

    def test_odd_boost_menor_ou_igual_a_original_e_descartada(self):
        ticket = ticket_combo_real()
        ticket["boostedPrice"] = {"value": ticket["price"]["value"]}  # igual, não maior
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket])]}
        self.assertEqual(self._rodar(grupos=grupos), [])

    # --- caso 3: sem evento_id -----------------------------------------

    def test_sem_evento_id_reconhecivel_e_descartada(self):
        ticket = ticket_combo_real()
        ticket["header"]["betContextId"] = None
        ticket["selections"][0]["betContextId"] = None
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket])]}
        self.assertEqual(self._rodar(grupos=grupos), [])

    # --- caso 4: boas-vindas / conta nova -------------------------------
    #
    # Não se aplica hoje: nas capturas reais desta revalidação (2026-08-05,
    # 47 ofertas de verdade) nenhum `prebuiltTicket` trouxe um campo
    # equivalente a `isWelcome` da Altenar — a Novibet não parece distinguir
    # boost de boas-vindas dos demais neste endpoint. O contrato (§7.3, item
    # 4) só exige o filtro "SE a casa distinguir isso no payload"; aqui não
    # há evidência de que distinga, então não há o que testar sem inventar
    # payload.

    # --- caso 5: odd_boost sempre a maior -------------------------------

    def test_price_maior_que_boostedprice_e_descartada_nao_invertida(self):
        """Diferença de comportamento em relação à Altenar: a invariante #1
        do contrato ("nunca confiar na ordem dos campos, comparar e pegar o
        maior") é implementada na Altenar com um `max()`/`min()` explícito.
        A Novibet NÃO compara — ela confia que `price` é sempre a original e
        `boostedPrice` sempre a turbinada. Se os dois viessem trocados, o
        resultado atual é SEGURO (a oferta é descartada por
        `odd_boost <= odd_original`, não vira edge invertido) mas diverge da
        letra do contrato. Este teste documenta o comportamento atual —
        descarte, não inversão — ver relatório para o patch sugerido.
        """
        ticket = ticket_combo_real()
        ticket["price"], ticket["boostedPrice"] = ticket["boostedPrice"], ticket["price"]
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket])]}
        self.assertEqual(self._rodar(grupos=grupos), [])

    # --- ticket suspenso -------------------------------------------------

    def test_ticket_suspenso_e_filtrado(self):
        ticket = ticket_combo_real()
        ticket["isSuspended"] = True
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket])]}
        self.assertEqual(self._rodar(grupos=grupos), [])

    # --- caso 9: payload vazio/malformado não quebra ----------------------

    def test_hub_sem_esportes_devolve_lista_vazia(self):
        """Caso 8 do checklist (equivalente Novibet do "sucesso vazio"): hub
        sem nada turbinado agora é `[]` com log informativo, não erro."""
        self.assertEqual(self._rodar(esportes={"items": []}), [])

    def test_arvore_sem_competicoes_nao_quebra(self):
        self.assertEqual(self._rodar(arvore={"items": []}), [])

    def test_grupo_com_ticket_malformado_nao_quebra(self):
        grupos = {self.LEAF_COPA_DO_BRASIL: [{"caption": "X", "prebuiltTickets": [{}]}]}
        self.assertEqual(self._rodar(grupos=grupos), [])

    def test_leaf_totalmente_vazio_nao_quebra(self):
        grupos = {self.LEAF_COPA_DO_BRASIL: []}
        self.assertEqual(self._rodar(grupos=grupos), [])

    def test_uma_competicao_falhando_nao_derruba_as_outras(self):
        """Invariante #6 do contrato, na escala de "competição dentro da
        casa": `asyncio.gather(..., return_exceptions=True)` isola a falha."""
        grupos = {
            self.LEAF_COPA_DO_BRASIL: RuntimeError("500 na Novibet"),
            7158567: [grupo_real([ticket_combo_real()])],
        }
        ofertas = self._rodar(grupos=grupos)
        self.assertEqual(len(ofertas), 1)

    # --- gap conhecido: ticket "Single" -----------------------------------

    def test_ticket_tipo_single_e_recuperado(self):
        """Corrigido: `_ofertas_da_selecao` cai pra `offer` (singular) quando
        `offers` (plural) não existe — ver `ticket_single_perdido()` acima."""
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_single_perdido()])]}
        ofertas = self._rodar(grupos=grupos)
        self.assertEqual(len(ofertas), 1)
        self.assertIn("Múltiplo resultado correto: 1 - 0, 2 - 0, 3 - 0", ofertas[0].mercado)

    def test_emoji_de_boost_da_novibet_e_removido_do_mercado(self):
        """Corrigido: `_limpar_rotulo` tira o emoji 🚀 (badge de boost da
        própria Novibet, confirmado no payload cru) antes de montar
        `Offer.mercado` — sem isso, `market_parser._parse_resultado` não
        casava "Resultado Final 🚀: <Time>", a perna mais comum da casa."""
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_combo_real()])]}
        o = self._rodar(grupos=grupos)[0]
        self.assertNotIn("\U0001F680", o.mercado)
        self.assertIn("Resultado Final: Vitória BA", o.mercado)

    def test_emoji_removido_ainda_casa_no_market_parser(self):
        """Fim a fim: sem o emoji, a perna de 1X2 mais comum da casa deixa
        de morrer em "mercado não reconhecido"."""
        from betano_superodds.value.market_parser import parse_leg
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_combo_real()])]}
        o = self._rodar(grupos=grupos)[0]
        pernas = [parse_leg(p) for p in o.mercado.split(" + ")]
        resultado = next(p for p in pernas if p.texto.startswith("Resultado Final"))
        self.assertTrue(resultado.suportado, resultado.motivo)

    # --- combo multi-jogo (regressão 2026-08-07, "Festival de Gols") -----

    def test_ticket_multi_jogo_prefixa_cada_perna_com_o_jogo(self):
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_multi_jogo()])]}
        o = self._rodar(grupos=grupos)[0]
        pernas = o.mercado.split(" + ")
        self.assertEqual(len(pernas), 2)
        self.assertTrue(pernas[0].startswith("[FC Cincinnati - Pumas UNAM] "), pernas[0])
        self.assertTrue(pernas[1].startswith("[Tigres UANL - Minnesota United FC] "), pernas[1])
        self.assertIn("Total de Gols: Mais de 2,5", pernas[0])
        self.assertIn("Total de Gols: Mais de 2,5", pernas[1])

    def test_ticket_multi_jogo_evento_agregado_nao_e_so_o_primeiro_jogo(self):
        """Antes do fix, `evento` virava só `selections[0].betContextCaption`
        — o segundo jogo do combo desaparecia até do cabeçalho do card."""
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_multi_jogo()])]}
        o = self._rodar(grupos=grupos)[0]
        self.assertIn("FC Cincinnati - Pumas UNAM", o.evento)
        self.assertIn("Tigres UANL - Minnesota United FC", o.evento)

    def test_ticket_jogo_unico_nao_ganha_prefixo(self):
        """Não-regressão: oferta de jogo único gera o MESMO `mercado` de
        sempre, sem colchete nenhum — `offer_id` (que depende de `mercado`)
        tem que ficar estável, senão o catálogo inteiro realertaria."""
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_combo_real()])]}
        o = self._rodar(grupos=grupos)[0]
        self.assertNotIn("[", o.mercado)
        self.assertEqual(o.evento, "Vitória BA - Athletico Paranaense PR")

    def test_ticket_multi_jogo_pernas_carregam_evento_texto(self):
        """Fim a fim com o market_parser: o prefixo vira `Leg.evento_texto`,
        não fica preso no texto — é o que `pipeline.py` usa pra casar cada
        perna com o jogo certo na Pinnacle."""
        from betano_superodds.value.market_parser import parse_mercado
        grupos = {self.LEAF_COPA_DO_BRASIL: [grupo_real([ticket_multi_jogo()])]}
        o = self._rodar(grupos=grupos)[0]
        legs = parse_mercado(o.mercado)
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].evento_texto, "FC Cincinnati - Pumas UNAM")
        self.assertEqual(legs[1].evento_texto, "Tigres UANL - Minnesota United FC")
        # E o resto da perna continua parseando normalmente, sem o prefixo.
        self.assertEqual(legs[0].market_key, "totals:2.5")
        self.assertTrue(legs[0].suportado, legs[0].motivo)


# --- seam de rede em isolamento (casos 7 e 10 do checklist) -------------

class _FakeResponse:
    def __init__(self, status_code: int = 200, payload=None, json_falha: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_falha = json_falha

    def json(self):
        if self._json_falha:
            raise ValueError("resposta não é JSON válido")
        return self._payload


class _FakeSession:
    """Sessão mínima só pra exercitar `_get_json` -- não é `curl_cffi` de
    verdade. Existe porque `ScraperFake` sobrescreve `_get_json` e por isso
    não exercita a lógica de status HTTP / parsing de JSON que mora nele."""

    def __init__(self, resposta=None, excecao: Exception | None = None) -> None:
        self._resposta, self._excecao = resposta, excecao

    async def get(self, url, params=None):
        if self._excecao is not None:
            raise self._excecao
        return self._resposta


class TestGetJson(unittest.TestCase):
    def test_http_nao_200_vira_scrapererror(self):
        sc = NovibetScraper(session=_FakeSession(_FakeResponse(status_code=500)))
        with self.assertRaises(ScraperError):
            asyncio.run(sc._get_json("http://x/"))

    def test_json_invalido_vira_scrapererror_com_mensagem_clara(self):
        """Caso 10 do checklist: distinguir bloqueio de WAF (HTML no lugar
        de JSON) de "0 ofertas hoje" — as duas não podem parecer a mesma coisa."""
        sc = NovibetScraper(session=_FakeSession(_FakeResponse(status_code=200, json_falha=True)))
        with self.assertRaises(ScraperError) as cm:
            asyncio.run(sc._get_json("http://x/"))
        self.assertIn("JSON", str(cm.exception))

    def test_falha_de_rede_vira_scrapererror(self):
        sc = NovibetScraper(session=_FakeSession(excecao=RequestException("timeout")))
        with self.assertRaises(ScraperError):
            asyncio.run(sc._get_json("http://x/"))

    def test_payload_feliz_devolve_o_json_decodificado(self):
        payload = {"items": []}
        sc = NovibetScraper(session=_FakeSession(_FakeResponse(status_code=200, payload=payload)))
        self.assertEqual(asyncio.run(sc._get_json("http://x/")), payload)


if __name__ == "__main__":
    unittest.main(verbosity=2)
