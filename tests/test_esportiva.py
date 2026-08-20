"""Testes do scraper da Esportiva Bet (Altenar) e do suporte a várias casas."""

from __future__ import annotations

import asyncio
import copy
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from betano_superodds import config
from betano_superodds.diff import DiffResult, diff_offers
from betano_superodds.esportiva import CASA, EsportivaScraper, _e_turbinado
from betano_superodds.models import Offer, to_utc_iso
from betano_superodds.storage import Storage


class ScraperFake(EsportivaScraper):
    """Troca a rede por payloads fixos, no formato real da Altenar."""

    def __init__(self, listagem: dict, detalhe: dict | None = None,
                 casa: config.CasaAltenar | None = None) -> None:
        super().__init__(casa, session=object())  # type: ignore[arg-type]
        self._listagem, self._detalhe = listagem, detalhe or {}

    async def _get(self, path: str, **params):
        return self._detalhe if "Details" in path else self._listagem


def daqui(horas: float) -> str:
    """Kickoff relativo, no formato da Altenar (ISO com Z).

    Data fixa no fixture não serve mais: a sondagem agora escolhe os eventos
    pelo horário do jogo, então um `startDate` no passado faz o payload inteiro
    ser (corretamente) descartado — e o teste passaria a medir o calendário em
    vez do parser.
    """
    return (datetime.now(timezone.utc)
            + timedelta(hours=horas)).strftime("%Y-%m-%dT%H:%M:%SZ")


def listagem() -> dict:
    """Resposta relacional da Altenar: listas ligadas por id."""
    return {
        "events": [{"id": 999, "name": "Athletico-PR vs. Vitória",
                    "startDate": daqui(3), "champId": 7,
                    "competitorIds": [1, 2], "marketIds": [10, 11]}],
        "markets": [
            {"id": 10, "name": "1x2 - Odds Aumentadas", "typeId": 1,
             "oddIds": [100, 101, 102]},
            {"id": 11, "name": "Vencedor do encontro", "typeId": 1,
             "oddIds": [200, 201, 202]},
        ],
        "odds": [
            {"id": 100, "name": "Athletico-PR", "price": 1.7},
            {"id": 101, "name": "Empate", "price": 3.6},
            {"id": 102, "name": "Vitória", "price": 6.1},
            {"id": 200, "name": "Athletico-PR", "price": 1.55},
            {"id": 201, "name": "Empate", "price": 3.4},
            {"id": 202, "name": "Vitória", "price": 5.5},
        ],
        "competitors": [{"id": 1, "name": "Athletico-PR"}, {"id": 2, "name": "Vitória"}],
        "champs": [{"id": 7, "name": "Copa do Brasil"}],
    }


def detalhe() -> dict:
    return {
        "id": 999, "name": "Athletico-PR vs. Vitória",
        "startDate": daqui(3), "champ": {"name": "Copa do Brasil"},
        # `desktopOddIds` + `price` são o que permite reconstruir o mercado
        # COMPLETO (os dois lados), matéria prima do consenso entre casas.
        #
        # ⚠️ A forma aqui é a do payload REAL, conferida na API, e não a
        # intuitiva. Errar isso já custou um ciclo inteiro gravando zero
        # mercado sem nenhum erro aparecer:
        #   - o campo é `desktopOddIds`, LISTA DE LISTAS. `oddIds` (plano) é da
        #     listagem do `GetEvents`, não do detalhe;
        #   - metade dos mercados vem em `childMarkets` — inclusive escanteios
        #     e cartões, que são justamente os que não têm Pinnacle.
        #
        # O mercado 32 tem uma seleção suspensa (price 0) de propósito: é o
        # caso que destruiria a margem se entrasse no de-vig.
        "markets": [{"id": 30, "name": "Ambas equipes marcam",
                     "desktopOddIds": [[300], [302]]},
                    {"id": 31, "name": "Total de gols",
                     "desktopOddIds": [[301], [303]]}],
        "childMarkets": [{"id": 32, "shortName": "Total de cartões 3.5",
                          "desktopOddIds": [[304], [305]]},
                         {"id": 33, "name": "Total de escanteios",
                          "desktopOddIds": [[306], [307]]},
                         # Mesmo NOME do 33, linha diferente: se o consenso
                         # agrupasse por nome, os dois virariam um mercado de
                         # 4 lados com margem de ~100% e o de-vig descartaria
                         # tudo calado.
                         {"id": 34, "name": "Total de escanteios",
                          "desktopOddIds": [[308], [309]]}],
        "odds": [{"id": 300, "name": "Sim", "price": 1.80},
                 {"id": 301, "name": "Mais de 2.5", "price": 2.05},
                 {"id": 302, "name": "Não", "price": 1.95},
                 {"id": 303, "name": "Menos de 2.5", "price": 1.75},
                 {"id": 304, "name": "Mais de 3.5", "price": 1.90},
                 {"id": 305, "name": "Menos de 3.5", "price": 0},
                 {"id": 306, "name": "Mais de 9.5", "price": 1.83},
                 {"id": 307, "name": "Menos de 9.5", "price": 1.83},
                 {"id": 308, "name": "Mais de 10.5", "price": 2.40},
                 {"id": 309, "name": "Menos de 10.5", "price": 1.50}],
        "boosts": [{
            "id": 5001, "eventId": 999,
            "odds": [{"marketId": 30, "selectionId": 300},
                     {"marketId": 31, "selectionId": 301}],
            "price": 1.5556,
            # `endDate` propositalmente diferente do `startDate`: é o fim da
            # promoção, não o apito inicial. O `inicio_evento` não pode sair
            # daqui.
            "boostInfo": {"price": 1.75, "endDate": daqui(2), "betsLimit": 3330},
        }],
    }


class TestParse(unittest.TestCase):
    def _rodar(self, lst=None, det=None, casa=None) -> list[Offer]:
        # `det={}` é um caso legítimo (evento sem `boosts`), então não dá pra
        # usar `det or detalhe()` — o dict vazio cairia no default.
        sc = ScraperFake(lst or listagem(),
                         detalhe() if det is None else det, casa)
        return asyncio.run(sc._scrape_esporte(66))

    def test_reconhece_mercado_turbinado_pelo_nome(self):
        self.assertTrue(_e_turbinado("1x2 - Odds Aumentadas"))
        self.assertTrue(_e_turbinado("ODDS AUMENTADAS"))
        self.assertFalse(_e_turbinado("Vencedor do encontro"))
        self.assertFalse(_e_turbinado(None))

    def test_cada_casa_batiza_o_mercado_turbinado_do_seu_jeito(self):
        """Regressão: casar só "aumentad" zerava EstrelaBet e vupi."""
        for nome in ("Vencedor do encontro - Super Odds",      # EstrelaBet, vupi
                     "Vencedor do encontro - Odds Aumentadas",  # BateuBet
                     "1x2 - Odds Aumentadas",                   # Esportiva
                     "Odds turbinadas", "Boosted odds"):
            self.assertTrue(_e_turbinado(nome), nome)

    def test_mercado_comum_continua_fora(self):
        """A abertura do filtro não pode deixar mercado normal virar oferta."""
        for nome in ("Vencedor do encontro", "Total de gols", "Ambas equipes marcam",
                     "Chance dupla", "Resultado Correto"):
            self.assertFalse(_e_turbinado(nome), nome)

    def test_so_o_mercado_turbinado_vira_oferta(self):
        """O 1x2 normal está no mesmo payload e não pode virar oferta."""
        ofertas = self._rodar()
        do_1x2 = [o for o in ofertas if o.fonte == "esportiva_1x2"]
        self.assertEqual(len(do_1x2), 3)
        self.assertEqual({o.odd_boost for o in do_1x2}, {1.7, 3.6, 6.1})

    def test_separador_de_evento_vira_o_que_o_matcher_espera(self):
        """A Altenar usa "A vs. B"; o matcher quebra em " - "."""
        o = self._rodar()[0]
        self.assertEqual(o.evento, "Athletico-PR - Vitória")

    def test_campos_da_oferta(self):
        o = self._rodar()[0]
        self.assertEqual(o.casa, CASA)
        self.assertEqual(o.liga, "Copa do Brasil")
        self.assertEqual(o.valido_ate, listagem()["events"][0]["startDate"])
        self.assertIn("999", o.url)

    def test_combo_traz_as_duas_odds_e_o_ganho(self):
        combos = [o for o in self._rodar() if o.fonte == "esportiva_boost"]
        self.assertEqual(len(combos), 1)
        c = combos[0]
        self.assertEqual(c.odd_original, 1.5556)
        self.assertEqual(c.odd_boost, 1.75)
        self.assertAlmostEqual(c.boost_pct, 12.5, places=1)

    def test_pernas_usam_o_separador_do_parser(self):
        """O market_parser quebra as pernas por " + "."""
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        self.assertEqual(len(combo.mercado.split(" + ")), 2)
        self.assertIn("Ambas equipes marcam: Sim", combo.mercado)

    def test_inicio_do_combo_vem_do_jogo_nao_do_fim_da_promocao(self):
        """`boostInfo.endDate` é o fim da PROMOÇÃO. Usá-lo como kickoff erra nos
        dois sentidos — e é o kickoff que decide se o alerta ainda vale."""
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        evento = [o for o in self._rodar() if o.fonte == "esportiva_1x2"][0]
        self.assertEqual(combo.inicio_evento, evento.inicio_evento)
        self.assertNotEqual(combo.inicio_evento, to_utc_iso(detalhe()
                                                            ["boosts"][0]["boostInfo"]["endDate"]))

    def test_inicio_evento_e_gravado_em_utc(self):
        """A coluna é comparada e ordenada em SQL: offset misturado mente."""
        o = self._rodar()[0]
        self.assertTrue(o.inicio_evento.endswith("+00:00"), o.inicio_evento)

    def test_limite_de_aposta_e_capturado(self):
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        self.assertEqual(combo.limite_aposta, 3330.0)

    def _revalidar(self, mercado: str, det: dict | None = None) -> float | None:
        sc = ScraperFake(listagem(), detalhe() if det is None else det)
        return asyncio.run(sc.revalidar("999", mercado))

    def test_revalidar_acha_a_odd_atual_do_combo(self):
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        self.assertEqual(self._revalidar(combo.mercado), combo.odd_boost)

    def test_revalidar_ve_a_odd_nova(self):
        """A casa reajustou o boost: quem manda é o valor de agora, não o do banco."""
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        det = detalhe()
        det["boosts"][0]["boostInfo"]["price"] = 1.62   # era 1.75
        self.assertEqual(self._revalidar(combo.mercado, det), 1.62)

    def test_revalidar_recusa_quando_a_casa_move_a_linha(self):
        """Canário do alerta falso de 2026-08-04 (BetGorillas, Levski).

        A casa trocou escanteios Mais de 9.5 @ 2.42 por Mais de 9 @ 1.70. Não
        é a mesma aposta — o rótulo tem que deixar de bater, senão a
        revalidação confirmaria uma odd que o usuário não consegue pegar.
        """
        det = detalhe()
        det["odds"][1]["name"] = "Mais de 9"          # era "Mais de 2.5"
        combo = [o for o in self._rodar() if o.fonte == "esportiva_boost"][0]
        self.assertIsNone(self._revalidar(combo.mercado, det))

    def test_revalidar_cobre_o_mercado_turbinado_da_listagem(self):
        """Fonte `*_1x2`: a seleção também vem no detalhe do evento."""
        det = detalhe()
        det["markets"].append({"id": 40, "name": "1x2 - Odds Aumentadas",
                               "oddIds": [400]})
        det["odds"].append({"id": 400, "name": "Athletico-PR", "price": 1.9})
        self.assertEqual(self._revalidar("Resultado Final: Athletico-PR", det), 1.9)

    def test_revalidar_ignora_mercado_nao_turbinado(self):
        """Odd comum com o mesmo rótulo não pode confirmar uma oferta turbinada."""
        det = detalhe()
        det["markets"].append({"id": 41, "name": "1x2", "oddIds": [401]})
        det["odds"].append({"id": 401, "name": "Athletico-PR", "price": 1.9})
        self.assertIsNone(self._revalidar("Resultado Final: Athletico-PR", det))

    def test_oferta_de_boas_vindas_e_descartada(self):
        """`isWelcome` é promoção de conta nova, não boost recorrente.

        Medido em 2026-08-04: 1.71→50.00, 1.077→22.00, 1.95→19.00 — todo ganho
        acima de +500% no banco era `isWelcome`. Como a fila de avaliação
        ordena por `boost_pct`, deixá-las entrar faz a promoção inútil furar a
        fila na frente da oferta real.
        """
        det = detalhe()
        det["boosts"][0]["boostInfo"]["isWelcome"] = True
        det["boosts"][0]["boostInfo"]["price"] = 50.0
        self.assertEqual([o for o in self._rodar(det=det)
                          if o.fonte == "esportiva_boost"], [])

    def test_boost_recorrente_convive_com_o_de_boas_vindas(self):
        """O array mistura os dois: descartar um não pode levar o outro."""
        det = detalhe()
        welcome = copy.deepcopy(det["boosts"][0])
        welcome["id"] = 5002
        welcome["boostInfo"]["isWelcome"] = True
        welcome["boostInfo"]["price"] = 50.0
        det["boosts"].append(welcome)
        combos = [o for o in self._rodar(det=det) if o.fonte == "esportiva_boost"]
        self.assertEqual(len(combos), 1)
        self.assertEqual(combos[0].odd_boost, 1.75)

    def test_maior_preco_e_sempre_o_turbinado(self):
        """Se a API inverter os campos, o edge sairia invertido."""
        det = detalhe()
        det["boosts"][0]["price"] = 1.75           # trocados de propósito
        det["boosts"][0]["boostInfo"]["price"] = 1.5556
        combo = [o for o in self._rodar(det=det) if o.fonte == "esportiva_boost"][0]
        self.assertEqual(combo.odd_boost, 1.75)
        self.assertEqual(combo.odd_original, 1.5556)

    def test_boost_sem_ganho_real_e_descartado(self):
        """A Altenar também lista, no mesmo array `boosts`, combos onde
        `price` e `boostInfo.price` são IDÊNTICOS — boost sem ganho nenhum.
        Visto ao vivo em 2026-08-05: VaiDeBet evento 16201772/16201756
        (2.6 -> 2.6) e BateuBet evento 16633614 (3.0 -> 3.0), ambos com
        isWelcome=false. Sem este corte o parser cria uma Offer com
        odd_boost == odd_original, violando a invariante #3 do
        ADAPTER_CONTRACT.md (já respeitada por novibet.py/casadeaposta.py/
        sportingtech.py, mas não por esta casa)."""
        det = detalhe()
        det["boosts"][0]["price"] = 2.6
        det["boosts"][0]["boostInfo"]["price"] = 2.6
        self.assertEqual([o for o in self._rodar(det=det)
                          if o.fonte == "esportiva_boost"], [])

    def test_evento_sem_turbinado_ainda_busca_o_detalhe(self):
        """Regressão: 4Play e BetGorillas só têm boost no array `boosts`.

        Filtrar o detalhe por "tem mercado turbinado na listagem" fazia essas
        casas devolverem zero oferta para sempre — a listagem delas não dá
        pista nenhuma de que existe combo turbinado no evento.
        """
        lst = listagem()
        lst["markets"][0]["name"] = "1x2"          # nada turbinado na listagem
        ofertas = self._rodar(lst=lst)
        self.assertEqual([o.fonte for o in ofertas], ["esportiva_boost"])

    def test_sem_turbinado_e_sem_boost_nao_gera_nada(self):
        lst = listagem()
        lst["markets"][0]["name"] = "1x2"
        self.assertEqual(self._rodar(lst=lst, det={}), [])

    def test_evento_com_turbinado_tem_prioridade_no_orcamento(self):
        """O teto de detalhes é escasso: quem já mostrou boost vai primeiro."""
        lst = listagem()
        lst["events"].insert(0, {"id": 111, "name": "A vs. B", "champId": 7,
                                 "competitorIds": [1, 2], "marketIds": [11]})
        sc = ScraperFake(lst, detalhe())
        pedidos: list[int] = []
        original = sc._do_detalhe

        async def espiao(ev_id, ligas):
            pedidos.append(ev_id)
            return await original(ev_id, ligas)

        sc._do_detalhe = espiao  # type: ignore[method-assign]
        asyncio.run(sc._scrape_esporte(66))
        self.assertEqual(pedidos[0], 999)   # o que tem mercado turbinado

    def test_payload_vazio_nao_quebra(self):
        vazio = {"events": [], "markets": [], "odds": [], "competitors": [], "champs": []}
        self.assertEqual(self._rodar(lst=vazio), [])


class TestVariasCasas(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def _par(self) -> tuple[Offer, Offer]:
        comum = dict(fonte="mr12", evento_id="e1", evento="Celtic - Dundee",
                     mercado="Resultado Final: Celtic", odd_original=1.19,
                     odd_boost=1.21, url="http://x")
        return (Offer(casa="Betano", **comum), Offer(casa="Esportiva Bet", **comum))

    def test_mesma_oferta_em_casas_diferentes_tem_ids_diferentes(self):
        """Senão uma casa sobrescreveria a outra no banco."""
        a, b = self._par()
        self.assertNotEqual(a.offer_id, b.offer_id)

    def test_as_duas_convivem_no_banco(self):
        a, b = self._par()
        self.storage.apply_diff(DiffResult(novas=[a, b], alteradas=[],
                                           expiradas=[], inalteradas=[]))
        ativas = self.storage.active_offers()
        self.assertEqual(len(ativas), 2)
        self.assertEqual({r["casa"] for r in ativas.values()},
                         {"Betano", "Esportiva Bet"})

    def test_diff_nao_confunde_as_casas(self):
        a, b = self._par()
        self.storage.apply_diff(DiffResult(novas=[a, b], alteradas=[],
                                           expiradas=[], inalteradas=[]))
        # Só a Betano volta no ciclo seguinte: a outra expira, não "muda".
        r = diff_offers(self.storage.active_offers(), [a])
        self.assertEqual(len(r.expiradas), 1)
        self.assertEqual(r.expiradas[0]["casa"], "Esportiva Bet")
        self.assertEqual(len(r.alteradas), 0)

    def test_casa_chega_na_mensagem(self):
        from betano_superodds.notifier import TelegramNotifier
        _, eds = self._par()
        texto = TelegramNotifier.formatar_value(
            {**eds.to_row(), "odd_justa": 1.10, "edge_pct": 10.0,
             "tipo_mercado": "simples", "fonte_odd": "pinnacle",
             "threshold_usado": 5.0})
        self.assertIn("Esportiva Bet", texto)
        self.assertNotIn("BOOK: Betano", texto)


class TestAltenarMultiCasa(unittest.TestCase):
    """O mesmo parser serve 10 casas — só o `integration` muda."""

    def _ofertas(self, casa=None):
        return asyncio.run(ScraperFake(listagem(), detalhe(), casa)._scrape_esporte(66))

    def test_casa_padrao_continua_sendo_a_esportiva(self):
        """Compatibilidade: quem instancia sem argumento não muda de casa."""
        o = self._ofertas()[0]
        self.assertEqual(o.casa, CASA)
        self.assertEqual(o.fonte, "esportiva_1x2")

    def test_fonte_da_esportiva_nao_mudou_de_nome(self):
        """As ofertas já gravadas casam por `fonte`; renomear órfãos o banco."""
        fontes = {o.fonte for o in self._ofertas()}
        self.assertEqual(fontes, {"esportiva_1x2", "esportiva_boost"})

    def test_outra_casa_gera_casa_fonte_e_url_proprios(self):
        estrela = config.CasaAltenar("EstrelaBet", "estrelabet",
                                     "https://www.estrelabet.bet.br")
        o = self._ofertas(estrela)[0]
        self.assertEqual(o.casa, "EstrelaBet")
        self.assertEqual(o.fonte, "estrelabet_1x2")
        self.assertTrue(o.url.startswith("https://www.estrelabet.bet.br"))

    def test_payload_identico_em_casas_diferentes_nao_colide(self):
        """Duas casas com o mesmo jogo são ofertas distintas, não duplicata."""
        outra = config.CasaAltenar("BateuBet", "bateu", "https://bateu.bet.br")
        a = self._ofertas()[0]
        b = self._ofertas(outra)[0]
        self.assertNotEqual(a.offer_id, b.offer_id)

    def test_slug_vai_no_integration_da_request(self):
        """É o único parâmetro que distingue uma casa da outra na API."""
        casa = config.CasaAltenar("vupi", "vupi", "https://www.vupi.bet.br")
        sc = EsportivaScraper(casa)
        self.assertEqual(sc.casa.slug, "vupi")
        self.assertNotIn("integration", __import__(
            "betano_superodds.esportiva", fromlist=["PARAMS_BASE"]).PARAMS_BASE)

    def test_tabela_de_casas_nao_tem_slug_repetido(self):
        """Slug repetido faria duas casas raspar exatamente o mesmo feed."""
        slugs = [c.slug for c in config.CASAS_ALTENAR]
        self.assertEqual(len(slugs), len(set(slugs)))

    def test_tabela_de_casas_nao_tem_nome_repetido(self):
        """O nome entra no offer_id: repetido faria uma sobrescrever a outra."""
        nomes = [c.nome for c in config.CASAS_ALTENAR]
        self.assertEqual(len(nomes), len(set(nomes)))


class TestLimiteDeAposta(unittest.TestCase):
    """O teto que a casa impõe precisa chegar até o alerta.

    O campo era capturado pelo scraper e o notificador já sabia exibi-lo, mas
    ele não era gravado no banco — então o aviso nunca aparecia. Numa oferta
    de 1.29 -> 12.00, "apostar 5un" sem o teto é recomendação inexecutável.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def _oferta(self, limite: float | None = 3330.0) -> Offer:
        return Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="1",
                     evento="A - B", mercado="x + y", odd_original=1.29,
                     odd_boost=12.0, url="http://x", limite_aposta=limite)

    def test_limite_sobrevive_ao_banco(self):
        o = self._oferta()
        self.storage.apply_diff(DiffResult(novas=[o], alteradas=[],
                                           expiradas=[], inalteradas=[]))
        row = list(self.storage.active_offers().values())[0]
        self.assertEqual(row["limite_aposta"], 3330.0)

    def test_sem_limite_grava_null_nao_zero(self):
        """0 seria lido como "a casa não deixa apostar nada"."""
        o = self._oferta(limite=None)
        self.storage.apply_diff(DiffResult(novas=[o], alteradas=[],
                                           expiradas=[], inalteradas=[]))
        row = list(self.storage.active_offers().values())[0]
        self.assertIsNone(row["limite_aposta"])

    def test_limite_e_renovado_sem_a_odd_mudar(self):
        """A casa pode apertar o teto sem mexer na odd — o hash não muda."""
        antes = self._oferta(limite=3330.0)
        self.storage.apply_diff(DiffResult(novas=[antes], alteradas=[],
                                           expiradas=[], inalteradas=[]))
        depois = self._oferta(limite=50.0)
        self.assertEqual(antes.content_hash, depois.content_hash)
        self.storage.apply_diff(DiffResult(novas=[], alteradas=[],
                                           expiradas=[], inalteradas=[depois]))
        row = list(self.storage.active_offers().values())[0]
        self.assertEqual(row["limite_aposta"], 50.0)

    def test_banco_antigo_ganha_a_coluna(self):
        """Migração: quem já rodava não pode quebrar ao atualizar."""
        import sqlite3
        caminho = Path(self._tmp.name) / "velho.db"
        conn = sqlite3.connect(caminho)
        conn.execute("""
            CREATE TABLE offers (
                offer_id TEXT PRIMARY KEY, content_hash TEXT NOT NULL,
                casa TEXT NOT NULL DEFAULT 'Betano', fonte TEXT NOT NULL,
                evento_id TEXT NOT NULL, evento TEXT NOT NULL, liga TEXT,
                mercado TEXT NOT NULL, odd_original REAL, odd_boost REAL NOT NULL,
                boost_pct REAL, valido_ate TEXT, url TEXT,
                first_seen TEXT NOT NULL, last_seen TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1)
        """)
        conn.execute(
            "INSERT INTO offers VALUES ('a','h','Betano','mr12','1','A - B',NULL,"
            "'m',1.1,1.2,9.1,NULL,NULL,'2026-01-01','2026-01-01',1)")
        conn.commit()
        conn.close()

        with Storage(caminho) as st:
            row = st.active_offers()["a"]
            self.assertIsNone(row["limite_aposta"])   # antiga: sem valor
            st.apply_diff(DiffResult(novas=[self._oferta()], alteradas=[],
                                     expiradas=[], inalteradas=[]))
            novas = [r for r in st.active_offers().values() if r["casa"] == "VaiDeBet"]
            self.assertEqual(novas[0]["limite_aposta"], 3330.0)

    def test_aviso_de_limite_chega_na_mensagem(self):
        from betano_superodds.notifier import TelegramNotifier
        o = self._oferta()
        texto = TelegramNotifier.formatar_value({
            **o.to_row(), "odd_justa": 9.0, "edge_pct": 33.3,
            "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "stake_descricao": "5un (¼ Kelly, teto 5un)",
            "stake_unidades": 5.0,
        })
        self.assertIn("3.330,00", texto)
        self.assertIn("limita", texto)

    def test_sem_limite_nao_inventa_valor(self):
        from betano_superodds.notifier import TelegramNotifier
        texto = TelegramNotifier.formatar_value({
            **self._oferta(limite=None).to_row(), "odd_justa": 9.0,
            "edge_pct": 33.3, "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "stake_descricao": "5un", "stake_unidades": 5.0,
        })
        self.assertNotIn("limita esta aposta", texto)

    def test_boost_absurdo_avisa_mesmo_sem_o_teto_da_api(self):
        """A Altenar não publica teto; o aviso sai do tamanho do boost.

        1.29 -> 12.00 é +830%: promoção com aposta máxima baixa, não
        generosidade. Recomendar 5un sem avisar seria inexecutável.
        """
        from betano_superodds.notifier import TelegramNotifier
        o = self._oferta(limite=None)
        # `boost_pct` vem nulo aqui de propósito: o aviso precisa funcionar
        # derivando o ganho das odds, senão só valeria pras fontes de combo.
        self.assertIsNone(o.boost_pct)
        self.assertGreater(o.ganho_pct, config.BOOST_ALTO_AVISO_PCT)
        texto = TelegramNotifier.formatar_value({
            **o.to_row(), "odd_justa": 9.0, "edge_pct": 33.3,
            "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "stake_descricao": "5un", "stake_unidades": 5.0,
        })
        self.assertIn("confira o limite", texto)

    def test_boost_normal_nao_recebe_o_aviso(self):
        """+20% é boost comum — poluir todo alerta mataria o sinal."""
        from betano_superodds.notifier import TelegramNotifier
        o = Offer(casa="BateuBet", fonte="bateu_boost", evento_id="1",
                  evento="A - B", mercado="x", odd_original=2.0, odd_boost=2.4,
                  url="http://x")
        texto = TelegramNotifier.formatar_value({
            **o.to_row(), "odd_justa": 2.2, "edge_pct": 9.1,
            "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "stake_descricao": "2un", "stake_unidades": 2.0,
        })
        self.assertNotIn("confira o limite", texto)

    def test_teto_real_tem_precedencia_sobre_o_palpite(self):
        from betano_superodds.notifier import TelegramNotifier
        texto = TelegramNotifier.formatar_value({
            **self._oferta(limite=50.0).to_row(), "odd_justa": 9.0,
            "edge_pct": 33.3, "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "stake_descricao": "5un", "stake_unidades": 5.0,
        })
        self.assertIn("limita esta aposta a 50,00", texto)
        self.assertNotIn("confira o limite", texto)


class TestSondaProfunda(unittest.TestCase):
    """Regressão: o boost nem sempre está no topo da listagem.

    A VaiDeBet tinha 77 combos turbinados nos 40 primeiros jogos — todos a
    partir do índice 20 — e devolvia ZERO oferta, porque o orçamento de
    detalhes parava no 11. O offset gira o ponto de partida a cada visita.
    """

    def _listagem_larga(self, n: int = 40) -> dict:
        """n eventos sem nada turbinado na listagem — como a VaiDeBet."""
        return {
            "events": [{"id": 1000 + i, "name": f"A{i} vs. B{i}",
                        "champId": 7, "competitorIds": [1, 2], "marketIds": []}
                       for i in range(n)],
            "markets": [], "odds": [], "competitors": [], "champs": [],
        }

    def _sondados(self, offset: int, teto: int = 12) -> list[int]:
        sc = ScraperFake(self._listagem_larga(), {})
        sc.offset = offset
        pedidos: list[int] = []

        async def espiao(ev_id, ligas):
            pedidos.append(ev_id)
            return []

        sc._do_detalhe = espiao  # type: ignore[method-assign]
        antigo = config.ALTENAR_MAX_DETALHES
        config.ALTENAR_MAX_DETALHES = teto
        try:
            asyncio.run(sc._scrape_esporte(66))
        finally:
            config.ALTENAR_MAX_DETALHES = antigo
        return pedidos

    def test_offset_zero_sonda_o_topo(self):
        self.assertEqual(self._sondados(0), [1000 + i for i in range(12)])

    def test_offset_alcanca_o_que_o_teto_nunca_pegaria(self):
        """Com offset 24, chega no índice 20+ que zerava a VaiDeBet."""
        self.assertIn(1030, self._sondados(24))

    def test_a_janela_inteira_e_coberta_dando_a_volta(self):
        janela = config.ALTENAR_JANELA_SONDA
        teto = config.ALTENAR_MAX_DETALHES
        vistos: set[int] = set()
        for visita in range(-(-janela // teto)):
            vistos.update(self._sondados(visita * teto, teto))
        esperado = {1000 + i for i in range(min(janela, 40))}
        self.assertTrue(esperado <= vistos)

    def test_offset_maior_que_a_janela_nao_quebra(self):
        self.assertEqual(len(self._sondados(9999)), 12)

    def _sondados_com_horario(self, eventos: list[tuple[int, float | None]],
                              teto: int = 12) -> list[int]:
        """`eventos` são pares (id, horas até o kickoff); None = sem startDate."""
        listagem = {
            "events": [
                {"id": ev_id, "name": f"A{ev_id} vs. B{ev_id}", "champId": 7,
                 "competitorIds": [1, 2], "marketIds": [],
                 **({} if horas is None else {"startDate": daqui(horas)})}
                for ev_id, horas in eventos
            ],
            "markets": [], "odds": [], "competitors": [], "champs": [],
        }
        sc = ScraperFake(listagem, {})
        pedidos: list[int] = []

        async def espiao(ev_id, ligas):
            pedidos.append(ev_id)
            return []

        sc._do_detalhe = espiao  # type: ignore[method-assign]
        antigo = config.ALTENAR_MAX_DETALHES
        config.ALTENAR_MAX_DETALHES = teto
        try:
            asyncio.run(sc._scrape_esporte(66))
        finally:
            config.ALTENAR_MAX_DETALHES = antigo
        return pedidos

    def test_orcamento_vai_para_quem_comeca_antes(self):
        """A ordem da API não tem relação com o horário do jogo."""
        self.assertEqual(
            self._sondados_com_horario([(1, 20), (2, 1), (3, 5)]),
            [2, 3, 1])

    def test_jogo_em_andamento_nao_consome_detalhe(self):
        self.assertEqual(
            self._sondados_com_horario([(1, -0.5), (2, 3)]), [2])

    def test_jogo_alem_do_horizonte_fica_para_depois(self):
        """A Pinnacle quase não publica mercado tão cedo — a avaliação falharia
        de qualquer jeito, e o detalhe é caro."""
        alem = config.ALTENAR_HORIZONTE_HORAS + 5
        self.assertEqual(self._sondados_com_horario([(1, alem), (2, 3)]), [2])

    def test_evento_sem_horario_vai_pro_fim_mas_nao_some(self):
        """Casa que não publica horário na listagem não pode perder cobertura."""
        self.assertEqual(
            self._sondados_com_horario([(1, None), (2, 6), (3, 2)]),
            [3, 2, 1])

    def test_listagem_menor_que_o_teto_nao_repete_evento(self):
        sc = ScraperFake({"events": [{"id": 1, "name": "A vs. B", "champId": 7,
                                      "competitorIds": [1, 2], "marketIds": []}],
                          "markets": [], "odds": [], "competitors": [], "champs": []},
                         {})
        sc.offset = 7
        pedidos: list[int] = []

        async def espiao(ev_id, ligas):
            pedidos.append(ev_id)
            return []

        sc._do_detalhe = espiao  # type: ignore[method-assign]
        asyncio.run(sc._scrape_esporte(66))
        self.assertEqual(pedidos, [1])


class TestSondaDirecionadaPelaFila(unittest.TestCase):
    """Uma fatia do orçamento de detalhes vai pros eventos que a AVALIAÇÃO
    precisa precificar, não só pra onde há pista de boost.

    Medido no banco vivo: das 509 ofertas ativas com perna sem cobertura, 31%
    não tinham NENHUMA outra casa no pool pro evento delas e 58% tinham menos
    que `CONSENSO_MIN_CASAS_PROP`. O pool era subproduto puro da caça a boost —
    nada olhava a fila.
    """

    def _listagem(self, eventos: list[tuple[int, str, float | None]],
                  turbinados: list[int] | None = None) -> dict:
        """`eventos` são triplas (id, nome, horas até o kickoff)."""
        turbinados = turbinados or []
        return {
            "events": [
                {"id": ev_id, "name": nome, "champId": 7,
                 "competitorIds": [1, 2],
                 "marketIds": [9000 + ev_id] if ev_id in turbinados else [],
                 **({} if horas is None else {"startDate": daqui(horas)})}
                for ev_id, nome, horas in eventos
            ],
            "markets": [{"id": 9000 + i, "name": "1x2 - Odds Aumentadas"}
                        for i in turbinados],
            "odds": [], "competitors": [], "champs": [],
        }

    def _sondados(self, listagem: dict, alvos: list[dict],
                  teto: int = 12, vagas: int = 4) -> list[int]:
        sc = ScraperFake(listagem, {})
        sc.alvos = alvos
        pedidos: list[int] = []

        async def espiao(ev_id, ligas):
            pedidos.append(ev_id)
            return []

        sc._do_detalhe = espiao  # type: ignore[method-assign]
        antigos = (config.ALTENAR_MAX_DETALHES, config.ALTENAR_DETALHES_FILA)
        config.ALTENAR_MAX_DETALHES = teto
        config.ALTENAR_DETALHES_FILA = vagas
        try:
            asyncio.run(sc._scrape_esporte(66))
        finally:
            (config.ALTENAR_MAX_DETALHES,
             config.ALTENAR_DETALHES_FILA) = antigos
        return pedidos

    def test_evento_da_fila_entra_na_frente_da_janela(self):
        """Com teto de 2, sem a fatia o evento 30 (fim da listagem) nunca
        seria sondado."""
        listagem = self._listagem([(10, "A vs. B", 1), (20, "C vs. D", 2),
                                   (30, "Norwich vs. West Bromwich", 3)])
        pedidos = self._sondados(
            listagem,
            [{"evento": "Norwich - West Bromwich", "inicio_evento": daqui(3)}],
            teto=2, vagas=1)
        self.assertIn(30, pedidos)

    def test_boost_na_listagem_continua_vindo_primeiro(self):
        """Cobertura de consenso só vale pra oferta que existe — quem já mostra
        mercado turbinado é pista concreta e não pode perder a vez."""
        listagem = self._listagem(
            [(10, "A vs. B", 5), (30, "Norwich vs. West Bromwich", 3)],
            turbinados=[10])
        pedidos = self._sondados(
            listagem,
            [{"evento": "Norwich - West Bromwich", "inicio_evento": daqui(3)}],
            teto=2, vagas=1)
        self.assertEqual(pedidos[0], 10)

    def test_a_fatia_sai_de_dentro_do_orcamento(self):
        """O custo por ciclo não pode subir: o teto continua sendo o teto."""
        listagem = self._listagem([(i, f"A{i} vs. B{i}", 2) for i in range(10, 20)])
        alvos = [{"evento": f"A{i} - B{i}", "inicio_evento": daqui(2)}
                 for i in range(10, 20)]
        self.assertEqual(len(self._sondados(listagem, alvos, teto=3, vagas=2)), 3)

    def test_muitos_boosts_nao_engolem_a_reserva_da_fila(self):
        """`com_boost` não tinha teto próprio antes do corte final — casa com
        muitos boosts na listagem (visto ao vivo: EstrelaBet com 36 boosts
        num único ciclo) engolia sozinha o orçamento inteiro, e a reserva de
        `ALTENAR_DETALHES_FILA` nunca era alcançada mesmo tendo sido
        calculada. Com teto=12/vagas=4, 20 eventos turbinados (bem acima do
        teto) não podem impedir o alvo da fila de entrar."""
        turbinados = list(range(100, 120))   # 20, bem acima do teto de 12
        listagem = self._listagem(
            [(i, f"A{i} vs. B{i}", 5) for i in turbinados]
            + [(30, "Norwich vs. West Bromwich", 3)],
            turbinados=turbinados,
        )
        pedidos = self._sondados(
            listagem,
            [{"evento": "Norwich - West Bromwich", "inicio_evento": daqui(3)}],
            teto=12, vagas=4)
        self.assertIn(30, pedidos)
        self.assertEqual(len(pedidos), 12, "o teto total continua valendo")

    def test_sem_alvo_de_fila_boost_continua_sem_teto_proprio(self):
        """O corte é CONDICIONAL: em ciclo sem alvo de fila casável, `com_boost`
        não perde nenhum espaço — não vale desperdiçar descoberta de oferta
        num cenário que é a maioria dos ciclos."""
        turbinados = list(range(100, 115))   # 15, acima do teto de 12
        listagem = self._listagem(
            [(i, f"A{i} vs. B{i}", 5) for i in turbinados], turbinados=turbinados)
        pedidos = self._sondados(listagem, [], teto=12, vagas=4)
        self.assertEqual(len(pedidos), 12)
        self.assertTrue(all(p in turbinados for p in pedidos))

    def test_evento_da_fila_nao_paga_detalhe_duas_vezes(self):
        """Um evento pode estar em `com_boost` E na fila; sem dedup ele
        consumiria duas vagas do mesmo orçamento."""
        listagem = self._listagem([(10, "Norwich vs. West Bromwich", 2),
                                   (20, "C vs. D", 3)], turbinados=[10])
        pedidos = self._sondados(
            listagem,
            [{"evento": "Norwich - West Bromwich", "inicio_evento": daqui(2)}],
            teto=4, vagas=2)
        self.assertEqual(len(pedidos), len(set(pedidos)))
        self.assertIn(20, pedidos)

    def test_alvo_que_nao_casa_nao_gasta_vaga(self):
        listagem = self._listagem([(10, "A vs. B", 2)])
        pedidos = self._sondados(
            listagem,
            [{"evento": "Jogo Que Nao Existe - Outro", "inicio_evento": daqui(2)}],
            teto=4, vagas=2)
        self.assertEqual(pedidos, [10])

    def test_guardas_do_matcher_valem_aqui_tambem(self):
        """Casar errado aqui não gera edge falso, mas gasta o request no jogo
        errado — que é exatamente o problema que a fatia existe pra resolver.
        Sem o veto de UF de `matcher.uf_conflita`, isto casaria."""
        sc = ScraperFake(self._listagem([(10, "Botafogo-SP vs. Ferroviaria", 2)]), {})
        sc.alvos = [{"evento": "Botafogo-RJ - Ferroviaria",
                     "inicio_evento": daqui(2)}]
        eventos = {e["id"]: e for e in sc._listagem["events"]}
        self.assertEqual(sc._eventos_da_fila(eventos), [])

    def test_alvo_alem_do_horizonte_nao_gasta_vaga(self):
        """A tolerância do matcher mede a CONCORDÂNCIA entre as duas datas, não
        a distância até agora: alvo e listagem concordam num jogo de daqui a
        três dias. Quem corta por distância é `_por_kickoff`."""
        alem = config.ALTENAR_HORIZONTE_HORAS + 5
        sc = ScraperFake(self._listagem([(10, "A vs. B", alem)]), {})
        sc.alvos = [{"evento": "A - B", "inicio_evento": daqui(alem)}]
        eventos = {e["id"]: e for e in sc._listagem["events"]}
        self.assertEqual(sc._eventos_da_fila(eventos), [])

    def test_sem_alvos_o_comportamento_e_o_de_antes(self):
        listagem = self._listagem([(10, "A vs. B", 1), (20, "C vs. D", 2)])
        self.assertEqual(self._sondados(listagem, [], teto=12), [10, 20])


class TestCursorDeOffset(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.db")
        self.casa = config.CasaAltenar("X", "x", "https://x.bet.br")

    def tearDown(self) -> None:
        self.storage.close()
        self._tmp.cleanup()

    def test_avanca_a_cada_visita(self):
        from betano_superodds.main import avancar_offset
        passo = config.ALTENAR_MAX_DETALHES
        self.assertEqual(avancar_offset(self.storage, self.casa), 0)
        self.assertEqual(avancar_offset(self.storage, self.casa), passo)
        self.assertEqual(avancar_offset(self.storage, self.casa), passo * 2)

    def test_da_a_volta_na_janela(self):
        from betano_superodds.main import avancar_offset
        vistos = [avancar_offset(self.storage, self.casa) for _ in range(20)]
        self.assertTrue(all(0 <= v < config.ALTENAR_JANELA_SONDA for v in vistos))
        self.assertIn(0, vistos[1:])   # voltou ao início em algum momento

    def test_cada_casa_tem_cursor_proprio(self):
        """Senão uma casa herdaria o offset da outra e sondaria torto."""
        from betano_superodds.main import avancar_offset
        outra = config.CasaAltenar("Y", "y", "https://y.bet.br")
        avancar_offset(self.storage, self.casa)
        avancar_offset(self.storage, self.casa)
        self.assertEqual(avancar_offset(self.storage, outra), 0)


class TestRodizio(unittest.TestCase):
    """O rodízio distribui o custo de request sem perder cobertura."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self._tmp.name) / "t.db")
        self._casas = config.CASAS_ALTENAR_ATIVAS
        self._n = config.CASAS_POR_CICLO
        config.CASAS_ALTENAR_ATIVAS = tuple(
            config.CasaAltenar(f"Casa{i}", f"c{i}", f"https://c{i}.bet.br")
            for i in range(5)
        )

    def tearDown(self) -> None:
        config.CASAS_ALTENAR_ATIVAS = self._casas
        config.CASAS_POR_CICLO = self._n
        self.storage.close()
        self._tmp.cleanup()

    def _ciclos(self, n: int) -> list[list[str]]:
        from betano_superodds.main import casas_do_ciclo
        return [[c.slug for c in casas_do_ciclo(self.storage)] for _ in range(n)]

    def test_gira_sem_repetir_dentro_da_volta(self):
        config.CASAS_POR_CICLO = 2
        self.assertEqual(self._ciclos(3), [["c0", "c1"], ["c2", "c3"], ["c4", "c0"]])

    def test_toda_casa_e_visitada_dentro_de_uma_volta(self):
        config.CASAS_POR_CICLO = 2
        vistos = {s for ciclo in self._ciclos(3) for s in ciclo}
        self.assertEqual(vistos, {"c0", "c1", "c2", "c3", "c4"})

    def test_cursor_sobrevive_a_reinicio(self):
        """Sem persistir, todo restart bateria sempre nas mesmas primeiras casas."""
        config.CASAS_POR_CICLO = 2
        self._ciclos(1)
        self.assertEqual(self.storage.get_estado("altenar_cursor"), "2")
        self.assertEqual(self._ciclos(1), [["c2", "c3"]])

    def test_zero_significa_todas_de_uma_vez(self):
        config.CASAS_POR_CICLO = 0
        self.assertEqual(len(self._ciclos(1)[0]), 5)

    def test_teto_maior_que_a_lista_nao_duplica(self):
        config.CASAS_POR_CICLO = 99
        self.assertEqual(len(self._ciclos(1)[0]), 5)

    def test_lista_vazia_nao_quebra(self):
        config.CASAS_ALTENAR_ATIVAS = ()
        self.assertEqual(self._ciclos(1), [[]])


class TestDiffComRodizio(unittest.TestCase):
    """Regressão: sem escopo de casa, o rodízio expiraria o mundo todo."""

    def _oferta(self, casa: str, mercado: str = "Resultado Final: Celtic") -> Offer:
        return Offer(casa=casa, fonte="x", evento_id="e1", evento="Celtic - Dundee",
                     mercado=mercado, odd_original=1.19, odd_boost=1.21, url="http://x")

    def _salvas(self, *ofertas: Offer) -> dict[str, dict]:
        return {o.offer_id: {**o.to_row(), "active": 1} for o in ofertas}

    def test_casa_fora_do_ciclo_nao_expira(self):
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        r = diff_offers(self._salvas(a, b), [a], casas_raspadas={"Betano"})
        self.assertEqual(r.expiradas, [])
        self.assertEqual(len(r.fora_do_ciclo), 1)
        self.assertEqual(r.fora_do_ciclo[0]["casa"], "EstrelaBet")

    def test_casa_raspada_que_perdeu_a_oferta_expira_normalmente(self):
        """A proteção não pode virar desculpa pra nunca expirar nada."""
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        r = diff_offers(self._salvas(a, b), [b],
                        casas_raspadas={"Betano", "EstrelaBet"})
        self.assertEqual(len(r.expiradas), 1)
        self.assertEqual(r.expiradas[0]["casa"], "Betano")

    def test_ativas_contam_quem_esta_dormindo(self):
        """Senão o log diria que metade das ofertas sumiu a cada ciclo."""
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        r = diff_offers(self._salvas(a, b), [a], casas_raspadas={"Betano"})
        self.assertEqual(r.total_ativas, 2)

    def test_dormindo_nao_conta_como_mudanca(self):
        """Casa fora do ciclo não pode fazer o ciclo parecer movimentado."""
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        r = diff_offers(self._salvas(a, b), [a], casas_raspadas={"Betano"})
        self.assertFalse(r.has_changes)

    def test_combo_nao_sondado_nao_expira(self):
        """Regressão: a janela girando fazia o combo da fatia anterior expirar
        e renascer no ciclo seguinte — realertando a mesma oferta."""
        combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                      evento="A - B", mercado="x + y", odd_original=2.0,
                      odd_boost=2.5, url="http://x")
        r = diff_offers(self._salvas(combo), [], casas_raspadas={"VaiDeBet"},
                        escopo_detalhe=set())     # nenhum evento sondado
        self.assertEqual(r.expiradas, [])
        self.assertEqual(len(r.fora_do_ciclo), 1)

    def test_combo_sondado_que_sumiu_expira(self):
        """A proteção vale só para quem não foi olhado."""
        combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                      evento="A - B", mercado="x + y", odd_original=2.0,
                      odd_boost=2.5, url="http://x")
        r = diff_offers(self._salvas(combo), [], casas_raspadas={"VaiDeBet"},
                        escopo_detalhe={("VaiDeBet", "777")})
        self.assertEqual(len(r.expiradas), 1)

    def test_oferta_de_listagem_expira_mesmo_sem_escopo(self):
        """A listagem vem inteira toda visita: ausência ali é sumiço real."""
        do_1x2 = Offer(casa="VaiDeBet", fonte="vaidebet_1x2", evento_id="777",
                       evento="A - B", mercado="Resultado Final: A",
                       odd_original=None, odd_boost=2.5, url="http://x")
        r = diff_offers(self._salvas(do_1x2), [], casas_raspadas={"VaiDeBet"},
                        escopo_detalhe=set())
        self.assertEqual(len(r.expiradas), 1)

    def test_escopo_nao_salva_combo_de_casa_nao_raspada(self):
        """As duas guardas são independentes e ambas precisam valer."""
        combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                      evento="A - B", mercado="x + y", odd_original=2.0,
                      odd_boost=2.5, url="http://x")
        r = diff_offers(self._salvas(combo), [], casas_raspadas={"Betano"},
                        escopo_detalhe={("VaiDeBet", "777")})
        self.assertEqual(r.expiradas, [])

    def test_oferta_vencida_expira_mesmo_sem_ter_sido_sondada(self):
        """Rede de segurança: jogo que já começou está morto, olhado ou não."""
        combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                      evento="A - B", mercado="x + y", odd_original=2.0,
                      odd_boost=2.5, url="http://x",
                      valido_ate="2020-01-01T00:00:00Z")
        r = diff_offers(self._salvas(combo), [], casas_raspadas={"VaiDeBet"},
                        escopo_detalhe=set())
        self.assertEqual(len(r.expiradas), 1)

    def test_oferta_futura_nao_sondada_continua_viva(self):
        combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                      evento="A - B", mercado="x + y", odd_original=2.0,
                      odd_boost=2.5, url="http://x",
                      valido_ate="2099-01-01T00:00:00Z")
        r = diff_offers(self._salvas(combo), [], casas_raspadas={"VaiDeBet"},
                        escopo_detalhe=set())
        self.assertEqual(r.expiradas, [])

    def test_valido_ate_ilegivel_nao_expira_por_chute(self):
        for ruim in ("", None, "amanhã", "2026-13-45"):
            combo = Offer(casa="VaiDeBet", fonte="vaidebet_boost", evento_id="777",
                          evento="A - B", mercado="x + y", odd_original=2.0,
                          odd_boost=2.5, url="http://x", valido_ate=ruim)
            r = diff_offers(self._salvas(combo), [], casas_raspadas={"VaiDeBet"},
                            escopo_detalhe=set())
            self.assertEqual(r.expiradas, [], f"valido_ate={ruim!r}")

    def test_casa_fora_do_ciclo_com_oferta_vencida_ainda_expira(self):
        """Vencimento vale mesmo para casa que ninguém visitou."""
        o = Offer(casa="EstrelaBet", fonte="estrelabet_1x2", evento_id="1",
                  evento="A - B", mercado="m", odd_original=None, odd_boost=2.0,
                  url="http://x", valido_ate="2020-01-01T00:00:00Z")
        r = diff_offers(self._salvas(o), [], casas_raspadas={"Betano"})
        self.assertEqual(len(r.expiradas), 1)

    def test_sem_o_argumento_o_comportamento_antigo_vale(self):
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        r = diff_offers(self._salvas(a, b), [a])
        self.assertEqual(len(r.expiradas), 1)
        self.assertEqual(r.fora_do_ciclo, [])

    def test_casa_que_falhou_no_ciclo_nao_entra_em_raspadas(self):
        """`coletar` só marca a casa como raspada se a captura deu certo —
        marcar antes faria uma falha de rede expirar o catálogo dela."""
        a, b = self._oferta("Betano"), self._oferta("EstrelaBet")
        # EstrelaBet caiu: não está em `raspadas`, então nada dela expira.
        r = diff_offers(self._salvas(a, b), [a], casas_raspadas={"Betano"})
        self.assertEqual(r.expiradas, [])


class TestMercadosCombinados(unittest.TestCase):
    """Regressão: casar só um pedaço de mercado combinado inventa edge.

    A Esportiva Bet vende "1x2 e ambas equipes marcam" como UMA seleção. O
    parser casava o pedaço "1x2 ... <time>", ignorava o resto e comparava a odd
    do combinado (4.20) com a odd justa do 1X2 puro (1.83) — +130% de edge
    falso, com alerta e stake de 5 unidades.
    """

    def _legs(self, texto):
        from betano_superodds.value.market_parser import parse_leg
        return parse_leg(texto)

    def test_1x2_com_btts_nunca_vira_1x2_puro(self):
        """O que causou o +130% foi comparar a odd do combinado (4.20) com a
        justa do 1X2 puro (1.83). A recusa era a saída segura enquanto não
        havia referência; hoje a Pinnacle publica `Both Teams To Score/Winner`,
        que é exatamente esta aposta, então dá pra precificar de verdade.

        O que não pode voltar em hipótese alguma é o mercado virar `h2h`.
        """
        leg = self._legs("1x2 e ambas equipes marcam: Vélez Sarsfield e não")
        self.assertEqual(leg.market_key, "btts_vencedor")
        self.assertEqual(leg.selecao, "No & {time:Vélez Sarsfield}")

    def test_1x2_com_btts_de_segundo_tempo_continua_recusado(self):
        """Não existe versão por tempo desse mercado na Pinnacle — casar com o
        do jogo inteiro seria restrito-contra-amplo de novo."""
        leg = self._legs("2º tempo - 1x2 e ambas equipes marcam: Independiente e não")
        self.assertFalse(leg.suportado)

    def test_variantes_combinadas_recusadas(self):
        for texto in [
            "2º tempo - 1x2 e ambas equipes marcam: Independiente e não",
            "1º tempo - Chance dupla e ambas equipes marcam: Empate/San Lorenzo e sim",
            "1x2 e total de gols: Flamengo e Mais de 2.5",
            "Resultado e ambas equipes marcam: Empate e sim",
        ]:
            leg = self._legs(texto)
            self.assertFalse(leg.suportado, texto)

    def test_total_por_equipe_sem_hifen_nao_vira_total_do_jogo(self):
        """Regressão real: alerta de +141.8% com stake no teto.

        "Mirassol total de gols: Mais de 1.5" (3.70 na BateuBet) era casado
        com `totals:1.5` — o over 1.5 da PARTIDA, cuja justa é 1.53. O over
        1.5 só do Mirassol vale ~3.6. Comparar um mercado restrito com a
        referência de um mercado amplo infla o edge inteiro.
        """
        for texto, time in [
            ("Mirassol total de gols: Mais de 1.5", "Mirassol"),
            ("Palmeiras total de gols: Mais de 2.5", "Palmeiras"),
            ("Cruzeiro MG total de gols: Mais de 2.5", "Cruzeiro MG"),
            ("Athletico-PR total de gols: Menos de 0.5", "Athletico-PR"),
            ("Santos - Total de Gols: Mais de 0.5", "Santos"),
            ("Flamengo (F) - Total de Gols Mais de 0.5", "Flamengo (F)"),
        ]:
            leg = self._legs(texto)
            self.assertTrue(leg.suportado, texto)
            self.assertTrue(leg.market_key.startswith("team_total"),
                            f"{texto} -> {leg.market_key}")
            self.assertEqual(leg.time_nome, time, texto)

    def test_total_do_jogo_continua_sendo_total_do_jogo(self):
        """A correção não pode empurrar o total da partida pra team_total."""
        for texto in [
            "Total de Gols Mais de 2.5",
            "Total de gols: Mais de 2.5",
            "Total de gols (incluindo linhas Asiáticas): Mais de 2.5",
            "Mais/Menos Gols: Mais de 3.5",
        ]:
            leg = self._legs(texto)
            self.assertTrue(leg.suportado, texto)
            self.assertTrue(leg.market_key.startswith("totals"),
                            f"{texto} -> {leg.market_key}")
            self.assertIsNone(leg.time_nome, texto)

    def test_qualificador_de_periodo_nao_e_nome_de_time(self):
        """"1º tempo - total de gols" é total do jogo no 1T, não de equipe."""
        leg = self._legs("1º tempo - total de gols: Mais de 0.5")
        self.assertTrue(leg.suportado)
        self.assertEqual(leg.market_key, "totals_1t:0.5")
        self.assertIsNone(leg.time_nome)

    def test_total_por_equipe_no_primeiro_tempo(self):
        leg = self._legs("1º tempo - Grêmio total de gols: Mais de 0.5")
        self.assertTrue(leg.suportado)
        self.assertTrue(leg.market_key.startswith("team_total_1t"))
        self.assertEqual(leg.time_nome, "Grêmio")

    def test_link_do_alerta_aponta_a_casa_certa(self):
        """O link dizia "Abrir na Betano" em oferta de qualquer casa."""
        from betano_superodds.notifier import TelegramNotifier
        texto = TelegramNotifier.formatar_value({
            "casa": "BateuBet", "evento": "Grêmio - Mirassol",
            "mercado": "Mirassol total de gols: Mais de 1.5",
            "odd_boost": 3.70, "odd_justa": 3.60, "edge_pct": 2.8,
            "tipo_mercado": "simples", "fonte_odd": "pinnacle",
            "threshold_usado": 5.0, "url": "https://bateu.bet.br/x",
        })
        self.assertIn("Abrir na BateuBet", texto)
        self.assertNotIn("Abrir na Betano", texto)

    def test_mercados_simples_continuam_passando(self):
        """A guarda não pode derrubar o que já funcionava."""
        for texto, chave in [
            ("Resultado Final: Celtic", "h2h"),
            ("Total de Gols Mais de 2.5", "totals:2.5"),
            ("Escanteios Mais de 9.5", "corners:9.5"),
            ("Ambas equipes Marcam Sim", "btts"),
            ("Total de Games Mais de 22.5", "games:22.5"),
        ]:
            leg = self._legs(texto)
            self.assertTrue(leg.suportado, texto)
            self.assertEqual(leg.market_key, chave)


class TestMercadosParaConsenso(unittest.TestCase):
    """Extração do mercado COMPLETO, não só da perna turbinada.

    Sem os dois lados não há de-vig, e sem de-vig não há como precificar prop
    nenhum — a Pinnacle não publica nenhum deles. O payload já trazia tudo; o
    parser é que descartava.
    """

    def _mercados(self, det=None):
        sc = ScraperFake(listagem(), detalhe() if det is None else det)
        asyncio.run(sc._scrape_esporte(66))
        return sc.mercados_vistos

    def test_colhe_os_dois_lados_do_mercado(self):
        m = self._mercados()
        btts = {x.selecao: x.preco
                for x in m if x.market_nome == "Ambas equipes marcam"}
        self.assertEqual(btts, {"Sim": 1.80, "Não": 1.95})

    def test_grava_a_casa_e_o_evento(self):
        um = self._mercados()[0]
        self.assertEqual(um.evento_id, "999")
        self.assertTrue(um.casa)

    def test_mercado_com_selecao_suspensa_e_descartado(self):
        """Preço 0 é seleção suspensa. Entrar no de-vig como probabilidade
        infinita destruiria a margem do mercado inteiro — e o mercado ainda
        ficaria com um lado só, que não serve pra de-vig nenhum."""
        nomes = {x.market_nome for x in self._mercados()}
        self.assertNotIn("Total de cartões 3.5", nomes)

    def test_colhe_mercado_mesmo_sem_boost_no_evento(self):
        """O consenso precisa do preço das casas que NÃO turbinaram — são elas
        que formam a referência contra a qual a turbinada é medida."""
        det = detalhe()
        det["boosts"] = []
        self.assertTrue(self._mercados(det),
                        "evento sem boost não pode deixar de alimentar o consenso")

    def test_payload_sem_mercados_nao_quebra(self):
        self.assertEqual(self._mercados({}), [])

    def test_le_childmarkets_tambem(self):
        """Escanteios e cartões moram em `childMarkets`. Ler só `markets`
        deixava de fora justamente os mercados que a Pinnacle não cobre."""
        nomes = {x.market_nome for x in self._mercados()}
        self.assertIn("Total de escanteios", nomes)

    def test_linhas_diferentes_do_mesmo_mercado_ficam_separadas(self):
        """Mesmo nome, `market_id` diferente: são mercados distintos e não
        podem virar um só, senão a margem sai errada e o de-vig recusa."""
        escanteios = [x for x in self._mercados()
                      if x.market_nome == "Total de escanteios"]
        self.assertEqual(len({x.market_id for x in escanteios}), 2)

    def test_grava_identidade_do_evento(self):
        """Nome/kickoff/liga são o que permite casar este `evento_id` com o
        de uma casa não-Altenar (ponte fuzzy, etapa futura). Sem isto só uma
        fração dos eventos do pool tinha nome recuperável."""
        um = self._mercados()[0]
        self.assertEqual(um.evento, "Athletico-PR - Vitória")
        self.assertEqual(um.liga, "Copa do Brasil")
        self.assertIsNotNone(um.inicio_evento)

    def test_identidade_bate_com_a_da_oferta_correspondente(self):
        """`_ofertas_do_detalhe` e `_mercados_do_detalhe` compartilham o mesmo
        helper de propósito: se normalizassem o nome de jeitos diferentes, a
        ponte fuzzy compararia texto que `offers` nunca produz."""
        ofertas = self._rodar_ofertas()
        mercado = self._mercados()[0]
        boost = next(o for o in ofertas if o.fonte == self.casa_boost)
        self.assertEqual(mercado.evento, boost.evento)
        self.assertEqual(mercado.liga, boost.liga)

    def _rodar_ofertas(self):
        sc = ScraperFake(listagem(), detalhe())
        self.casa_boost = sc.casa.fonte_boost
        return asyncio.run(sc._scrape_esporte(66))


if __name__ == "__main__":
    unittest.main(verbosity=2)
