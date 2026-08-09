"""Testes do avaliador de value bet: de-vig, parser, matcher e thresholds."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from betano_superodds.value import config, consenso, stake
from betano_superodds.value.fair_odds import calcular_odd_justa, prob_da_perna, remover_vig
from betano_superodds.value.market_parser import Leg, parse_leg, parse_mercado, tipo_mercado
from betano_superodds.value.matcher import encontrar_evento, normalizar, score_times
from betano_superodds.value.modelo_gols import _melhor_total, ajustar, matriz_placares
from betano_superodds.value.models import Market, Matchup, american_para_decimal
from betano_superodds.value.pipeline import avaliar_oferta
from betano_superodds.value.value_calc import (avaliar_value, calcular_edge,
                                                classe_mercado_da_perna,
                                                classificar_confianca,
                                                threshold_para)

HOJE = datetime(2026, 8, 5, 20, 0, tzinfo=timezone.utc)


def matchup_exemplo() -> Matchup:
    """Celtic x Dundee com as odds reais da Pinnacle (já em decimal)."""
    m = Matchup(id=1, league="Scotland - Premiership", home_team="Celtic",
                away_team="Dundee", commence_time=HOJE)
    m.markets["h2h"] = Market(key="h2h", label="Resultado Final",
                              outcomes={"home": 1.1647, "draw": 8.52, "away": 13.94})
    m.markets["totals:2.5"] = Market(key="totals:2.5", label="Total de Gols 2.5",
                                     outcomes={"over": 1.30, "under": 3.60})
    m.markets["corners:9.5"] = Market(key="corners:9.5", label="Escanteios 9.5",
                                      outcomes={"over": 1.90, "under": 1.95})
    m.markets["btts"] = Market(key="btts", label="Both Teams To Score?",
                               outcomes={"Yes": 2.10, "No": 1.75})
    return m


class TestConversao(unittest.TestCase):
    def test_american_para_decimal(self):
        self.assertAlmostEqual(american_para_decimal(256), 3.56, places=2)
        self.assertAlmostEqual(american_para_decimal(-607), 1.1647, places=3)
        self.assertAlmostEqual(american_para_decimal(100), 2.0, places=3)
        self.assertIsNone(american_para_decimal(None))
        self.assertIsNone(american_para_decimal(0))


class TestDeVig(unittest.TestCase):
    def test_remove_margem_e_soma_um(self):
        market = matchup_exemplo().markets["h2h"]
        probs = remover_vig(market)
        self.assertIsNotNone(probs)
        soma = sum(p.probabilidade for p in probs.values())
        self.assertAlmostEqual(soma, 1.0, places=9, msg="probabilidades devem somar 1")

    def test_valores_conferem_com_calculo_manual(self):
        probs = remover_vig(matchup_exemplo().markets["h2h"])
        self.assertAlmostEqual(probs["home"].odd_justa, 1.22, places=2)
        self.assertAlmostEqual(probs["draw"].odd_justa, 8.93, places=2)
        self.assertAlmostEqual(probs["away"].odd_justa, 14.60, places=1)

    def test_odd_justa_e_sempre_maior_que_a_crua(self):
        """Tirar a margem só pode aumentar a odd — se diminuir, o sinal está trocado."""
        market = matchup_exemplo().markets["h2h"]
        probs = remover_vig(market)
        for nome, odd_crua in market.outcomes.items():
            self.assertGreater(probs[nome].odd_justa, odd_crua)

    def test_margem_absurda_e_rejeitada(self):
        ruim = Market(key="h2h", label="x", outcomes={"home": 1.01, "draw": 1.01, "away": 1.01})
        self.assertIsNone(remover_vig(ruim), "overround de ~197% tem que ser descartado")

    def test_mercado_incompleto_e_rejeitado(self):
        self.assertIsNone(remover_vig(Market(key="h2h", label="x", outcomes={"home": 1.5})))


class TestMarketParser(unittest.TestCase):
    def test_resultado_final_por_nome(self):
        leg = parse_leg("Resultado Final: Celtic")
        self.assertTrue(leg.suportado)
        self.assertEqual(leg.market_key, "h2h")
        self.assertEqual(leg.time_nome, "Celtic")

    def test_resultado_final_numerico(self):
        self.assertEqual(parse_leg("Resultado Final 1").selecao, "home")
        self.assertEqual(parse_leg("Resultado Final 2").selecao, "away")
        self.assertEqual(parse_leg("Resultado Final: Empate").selecao, "draw")

    def test_total_gols_e_escanteios(self):
        gols = parse_leg("Total de Gols Mais de 2.5")
        self.assertEqual((gols.market_key, gols.selecao), ("totals:2.5", "over"))
        corner = parse_leg("Escanteios Mais de 9.5")
        self.assertEqual((corner.market_key, corner.selecao), ("corners:9.5", "over"))

    def test_btts(self):
        self.assertEqual(parse_leg("Ambas equipes Marcam Sim").selecao, "Yes")
        self.assertEqual(parse_leg("Ambas equipes Marcam Não").selecao, "No")

    def test_primeiro_tempo_vira_sufixo(self):
        self.assertEqual(parse_leg("Resultado do 1° Tempo Empate").market_key, "h2h_1t")

    def test_cartoes_nao_confundem_com_btts(self):
        """"Ambas as equipes receberão um cartão" contém "Ambas as equipes"."""
        leg = parse_leg("Ambas as equipes receberão um cartão Sim")
        self.assertFalse(leg.suportado)
        self.assertEqual(leg.motivo, "cartões")

    def test_mercados_sem_referencia(self):
        for texto, motivo in [
            ("Total de Cartões Mais de 5.5", "cartões"),
            ("Chutes no gol Mais de 7.5", "chutes no gol"),
            ("Marcar em qualquer momento K. Høgh (Celtic)", "artilheiro (prop de jogador)"),
        ]:
            leg = parse_leg(texto)
            self.assertFalse(leg.suportado, texto)
            self.assertEqual(leg.motivo, motivo)

    def test_grafia_altenar_do_total_da_partida(self):
        """A Altenar omite "de gols" quando o total é o da partida.

        O parser nasceu com a grafia da Betano; as 9 casas Altenar entraram
        depois. `Total de Gols Mais de 2.5` passava e `Total: Mais de 2.5`, o
        mesmo mercado, caía em "não reconhecido".
        """
        for texto, sel in [("Total: Mais de 2.5", "over"),
                           ("Total: Menos de 2.5", "under")]:
            leg = parse_leg(texto)
            self.assertTrue(leg.suportado, texto)
            self.assertEqual(leg.market_key, "totals:2.5")
            self.assertEqual(leg.selecao, sel)

        self.assertEqual(parse_leg("1º tempo - total: Mais de 1.5").market_key,
                         "totals_1t:1.5")

    def test_grafia_altenar_do_total_por_equipe(self):
        """"1 total"/"2 total": o lado já vem no rótulo, sem nome de time —
        então a chave sai resolvida, sem o placeholder `{lado}`."""
        casa = parse_leg("1 total: Mais de 1.5")
        self.assertEqual(casa.market_key, "team_total:home:1.5")
        self.assertEqual(casa.selecao, "over")
        fora = parse_leg("2 total: Menos de 1.5")
        self.assertEqual(fora.market_key, "team_total:away:1.5")

    def test_grafia_altenar_do_1x2(self):
        self.assertEqual(parse_leg("1x2: Empate").selecao, "draw")
        self.assertEqual(parse_leg("1x2: 1").selecao, "home")
        self.assertEqual(parse_leg("1x2: 2").selecao, "away")
        self.assertEqual(parse_leg("1x2: Levski Sófia").time_nome, "Levski Sófia")
        self.assertEqual(parse_leg("1º tempo - 1x2: Empate").market_key, "h2h_1t")

    def test_vencedor_do_encontro_nao_engole_o_rotulo(self):
        """Regressão silenciosa: `vencedor\\s+(.+)$` capturava "do encontro:
        Atlético MG" como nome de time. A perna ficava marcada como suportada
        e depois morria em "sem odd na Pinnacle", sem nunca acusar erro."""
        leg = parse_leg("Vencedor do encontro: Atlético MG")
        self.assertEqual(leg.time_nome, "Atlético MG")
        self.assertEqual(parse_leg("Vencedor do encontro: 1").selecao, "home")

    def test_perna_de_bet_builder_sem_mercado_e_recusada(self):
        """"Mais de 0.5 + Mais de 0.5" é oferta real da Esportiva: duas pernas
        de mercados DIFERENTES cujo nome se perdeu no payload. Lidas como
        total de gols da partida dariam duas probabilidades de ~0.95 e um edge
        inventado — mesma classe do falso +141% do Mirassol."""
        legs = parse_mercado("Mais de 0.5 + Mais de 0.5")
        self.assertEqual(len(legs), 2)
        for leg in legs:
            self.assertFalse(leg.suportado)
            self.assertEqual(leg.motivo, "seleção sem mercado (bet-builder)")

    def test_prop_de_jogador_do_bet_builder(self):
        """Rótulos sem nome de mercado cujo `sv` traz `ls:player:NNN`.
        A Pinnacle não publica artilheiro — não há referência possível."""
        for texto in ("Qualq. Altura", "Qualq. Momento", "Primeiro", "Último"):
            leg = parse_leg(texto)
            self.assertFalse(leg.suportado, texto)
            self.assertEqual(leg.motivo, "artilheiro (prop de jogador)")

    def test_total_de_equipe_por_nome_continua_valendo(self):
        """A abertura da grafia não pode reabrir o falso +141%: total de UMA
        equipe tem que continuar indo pra team_total, nunca pro total da partida."""
        leg = parse_leg("Mirassol total de gols: Mais de 1.5")
        self.assertEqual(leg.market_key, "team_total:{lado}:1.5")
        self.assertEqual(leg.time_nome, "Mirassol")

    def test_split_de_combo(self):
        legs = parse_mercado("Total de Gols Mais de 2.5 + Escanteios Mais de 9.5 + Resultado Final 1")
        self.assertEqual(len(legs), 3)
        self.assertEqual(tipo_mercado(legs), "combo")
        self.assertTrue(all(l.suportado for l in legs))

    def test_prefixo_de_jogo_vira_evento_texto(self):
        """`novibet.py` prefixa pernas de combo multi-jogo com "[Jogo] " —
        `parse_mercado` extrai isso pra `Leg.evento_texto` e segue parseando
        o resto normalmente, sem o colchete atrapalhar o mercado."""
        legs = parse_mercado(
            "[FC Cincinnati - Pumas UNAM] Total de Gols: Mais de 2,5 + "
            "[Tigres UANL - Minnesota United FC] Total de Gols: Mais de 2,5"
        )
        self.assertEqual(len(legs), 2)
        self.assertEqual(legs[0].evento_texto, "FC Cincinnati - Pumas UNAM")
        self.assertEqual(legs[1].evento_texto, "Tigres UANL - Minnesota United FC")
        for leg in legs:
            self.assertEqual(leg.market_key, "totals:2.5")
            self.assertEqual(leg.selecao, "over")
            self.assertTrue(leg.suportado, leg.motivo)
            self.assertNotIn("[", leg.texto)

    def test_perna_sem_prefixo_nao_ganha_evento_texto(self):
        """Não-regressão: oferta comum (jogo único) não tem `evento_texto`
        nenhum — é o `None` que faz `pipeline.py`/`fair_odds.py` tratarem a
        oferta como sempre trataram."""
        legs = parse_mercado("Resultado Final: Celtic + Total de Gols Mais de 2.5")
        self.assertTrue(all(l.evento_texto is None for l in legs))


class TestMatcher(unittest.TestCase):
    def test_normalizacao_remove_acento_e_sufixo(self):
        self.assertEqual(normalizar("São Paulo FC"), "sao paulo")
        self.assertEqual(normalizar("CR Flamengo"), "flamengo")

    def test_score_usa_o_pior_lado(self):
        """Um lado certo não pode carregar um lado errado."""
        score = score_times("Flamengo", "Palmeiras", "Flamengo", "Santos")
        self.assertLess(score, 80, "confronto diferente não pode passar do threshold")

    def test_match_com_nomes_diferentes(self):
        alvo = Matchup(id=9, league="L", home_team="Flamengo", away_team="Palmeiras",
                       commence_time=HOJE)
        r = encontrar_evento("CR Flamengo - SE Palmeiras", HOJE.isoformat(), [alvo])
        self.assertIsNotNone(r)
        self.assertEqual(r.matchup.id, 9)

    def test_recusa_quando_confianca_e_baixa(self):
        outro = Matchup(id=9, league="L", home_team="Grêmio", away_team="Internacional",
                        commence_time=HOJE)
        self.assertIsNone(encontrar_evento("Flamengo - Palmeiras", HOJE.isoformat(), [outro]))

    def test_data_distante_descarta_o_confronto(self):
        """Mesmos times, outra rodada — não pode casar."""
        antigo = Matchup(id=9, league="L", home_team="Flamengo", away_team="Palmeiras",
                         commence_time=HOJE - timedelta(days=30))
        self.assertIsNone(encontrar_evento("Flamengo - Palmeiras", HOJE.isoformat(), [antigo]))

    def test_detecta_mandante_invertido(self):
        alvo = Matchup(id=9, league="L", home_team="Palmeiras", away_team="Flamengo",
                       commence_time=HOJE)
        r = encontrar_evento("Flamengo - Palmeiras", HOJE.isoformat(), [alvo])
        self.assertIsNotNone(r)
        self.assertTrue(r.invertido)


class TestMatcherNomesReais(unittest.TestCase):
    """Casos colhidos do banco em 2026-08-04 — todos eram o MESMO jogo e eram
    rejeitados por 3 a 20 pontos de score. Juntos valiam 16pp de cobertura."""

    CASOS = (
        ("SK Brann - Apollon Limassol", "Brann", "Apollon Limassol"),
        ("Vitória BA - Athletico-PR", "Vitoria", "Athletico Paranaense"),
        ("Boca Juniors - Estudiantes LP", "Boca Juniors", "Estudiantes de La Plata"),
        ("Vila Nova - Sport", "Vila Nova", "Sport Recife"),
        ("Botafogo-SP - América-MG", "Botafogo SP", "America Mineiro"),
        ("Jagiellonia Bialystok - Glasgow Rangers", "Jagiellonia Bialystok", "Rangers"),
        ("Operário-PR - São Bernardo", "Operario Ferroviario", "Sao Bernardo"),
        ("AGF Aarhus - Sabah FC", "AGF", "Sabah FK"),
        ("K-League Allstars - Manchester City", "K League All Stars", "Manchester City"),
        ("Tiago João Luís Pereira - Max Basing", "Tiago Pereira", "Max Basing"),
    )

    def test_casa_o_jogo_certo(self):
        for evento, casa, fora in self.CASOS:
            with self.subTest(evento=evento):
                alvo = Matchup(id=1, league="L", home_team=casa, away_team=fora,
                               commence_time=HOJE)
                r = encontrar_evento(evento, HOJE.isoformat(), [alvo])
                self.assertIsNotNone(r, f"{evento} deixou de casar com {casa} x {fora}")


class TestMatcherGuardas(unittest.TestCase):
    """O preço de aceitar subconjunto e apagar a UF.

    Sem estas guardas a mudança do matcher troca perda de cobertura por edge
    falso — comparar a odd de um jogo contra a odd justa de outro.
    """

    def test_homonimo_de_estado_diferente_nao_casa(self):
        """Botafogo-SP e Botafogo-RJ viram o mesmo texto depois de normalizar,
        e podem jogar no mesmo dia — a janela de data não separa."""
        rj = Matchup(id=1, league="Brazil - Serie A", home_team="Botafogo RJ",
                     away_team="Fluminense", commence_time=HOJE)
        self.assertIsNone(
            encontrar_evento("Botafogo-SP - Fluminense", HOJE.isoformat(), [rj]))

    def test_uf_so_de_um_lado_nao_e_conflito(self):
        """A Pinnacle escreve "Vitoria" seco; exigir a sigla nos dois lados
        devolveria a perda que a normalização acabou de resolver."""
        alvo = Matchup(id=1, league="Brazil - Cup", home_team="Vitoria",
                       away_team="Athletico Paranaense", commence_time=HOJE)
        self.assertIsNotNone(
            encontrar_evento("Vitória BA - Athletico-PR", HOJE.isoformat(), [alvo]))

    def test_feminino_nao_casa_com_masculino(self):
        """`RUIDO` come "women"/"feminino", então os nomes ficam idênticos.
        A casa marca no NOME do time, a Pinnacle marca na LIGA."""
        masc = Matchup(id=1, league="Brazil - Serie A", home_team="Corinthians",
                       away_team="Palmeiras", commence_time=HOJE)
        self.assertIsNone(
            encontrar_evento("Corinthians (F) - Palmeiras (F)", HOJE.isoformat(), [masc]))

    def test_masculino_nao_casa_com_liga_feminina(self):
        fem = Matchup(id=1, league="Brazil - Serie A1 Women", home_team="Corinthians",
                      away_team="Palmeiras", commence_time=HOJE)
        self.assertIsNone(
            encontrar_evento("Corinthians - Palmeiras", HOJE.isoformat(), [fem]))

    def test_feminino_casa_com_liga_feminina(self):
        fem = Matchup(id=1, league="WNBA", home_team="Atlanta Dream",
                      away_team="Phoenix Mercury", commence_time=HOJE)
        r = encontrar_evento("Atlanta Dream (F) - Phoenix Mercury (F)",
                             HOJE.isoformat(), [fem])
        self.assertIsNotNone(r)

    def test_confronto_diferente_continua_recusado(self):
        """A guarda principal não pode ter afrouxado: token_set não vale de nada
        se "Palmeiras" passar a casar com "Santos"."""
        outro = Matchup(id=1, league="L", home_team="Flamengo", away_team="Santos",
                        commence_time=HOJE)
        self.assertIsNone(
            encontrar_evento("Flamengo - Palmeiras", HOJE.isoformat(), [outro]))

    def test_nome_que_some_na_normalizacao_nao_casa_com_qualquer_um(self):
        """"FC" vira string vazia; comparar vazio com vazio daria 100."""
        alvo = Matchup(id=1, league="L", home_team="FC", away_team="SC",
                       commence_time=HOJE)
        self.assertIsNone(encontrar_evento("FC - SC", HOJE.isoformat(), [alvo]))


def matchup_com_especiais() -> Matchup:
    """Celtic x Dundee com os especiais no formato REAL da Pinnacle.

    Os desfechos são indexados pelo nome do time — "Celtic 2, Dundee 1",
    "Draw Or Dundee" — e é isso que os moldes do parser precisam produzir.

    Um mercado PARCIAL não serve de fixture: o de-vig exige que os desfechos
    cubram o espaço todo, senão a margem sai muito negativa e ele descarta —
    com razão. Por isso as grades abaixo são completas, e a chance dupla é
    derivada do mesmo 1X2 do `matchup_exemplo` (com a mesma margem), que é o
    que permite conferir uma contra a outra.
    """
    m = matchup_exemplo()
    m.markets["correct_score"] = Market(
        key="correct_score", label="Correct Score",
        outcomes={"Celtic 1, Dundee 0": 7.12, "Celtic 2, Dundee 0": 5.79,
                  "Celtic 2, Dundee 1": 9.26, "Celtic 3, Dundee 0": 7.72,
                  "Celtic 3, Dundee 1": 10.29, "Celtic 0, Dundee 0": 23.15,
                  "Celtic 1, Dundee 1": 13.23, "Celtic 4, Dundee 0": 13.23,
                  "Celtic 4, Dundee 1": 18.52, "Celtic 3, Dundee 2": 30.86,
                  "Celtic 0, Dundee 1": 46.30, "Celtic 5, Dundee 0": 18.52,
                  "Celtic 2, Dundee 2": 23.15, "Celtic 1, Dundee 2": 30.86})
    m.markets["ht_ft"] = Market(
        key="ht_ft", label="Half-Time/Full-Time",
        outcomes={"Celtic - Celtic": 1.699, "Celtic - Draw": 31.15,
                  "Celtic - Dundee": 93.46, "Draw - Celtic": 5.19,
                  "Draw - Draw": 13.35, "Draw - Dundee": 23.36,
                  "Dundee - Celtic": 18.69, "Dundee - Draw": 46.73,
                  "Dundee - Dundee": 18.69})
    m.markets["double_chance"] = Market(
        key="double_chance", label="Double Chance",
        outcomes={"Celtic Or Draw": 1.0246, "Draw Or Dundee": 5.288,
                  "Celtic Or Dundee": 1.0749})
    m.markets["btts_vencedor"] = Market(
        key="btts_vencedor", label="Both Teams To Score/Winner",
        outcomes={"Yes & Celtic": 3.14, "No & Celtic": 1.814,
                  "Yes & Draw": 15.72, "No & Draw": 18.87,
                  "Yes & Dundee": 23.58, "No & Dundee": 31.45})
    return m


class TestEspeciais(unittest.TestCase):
    """Mercados que a Pinnacle publica inteiros, com preço próprio."""

    def test_placar_exato(self):
        r = calcular_odd_justa(parse_mercado("Resultado Correto: 2:1"),
                               matchup_com_especiais())
        self.assertTrue(r.ok)
        self.assertEqual(r.tipo_mercado, "simples")

    def test_intervalo_final(self):
        for texto in ("Intervalo/final do jogo: 1/1",
                      "Intervalo/final do jogo: Empate/1",
                      "Intervalo/final do jogo: Empate/empate"):
            r = calcular_odd_justa(parse_mercado(texto), matchup_com_especiais())
            self.assertTrue(r.ok, texto)

    def test_1x2_com_btts_usa_o_mercado_proprio(self):
        """Era recusado por não haver referência; a Pinnacle publica."""
        r = calcular_odd_justa(
            parse_mercado("1x2 e ambas equipes marcam: Celtic e sim"),
            matchup_com_especiais())
        self.assertTrue(r.ok)

    def test_molde_sem_desfecho_correspondente_nao_inventa(self):
        """Placar que a Pinnacle não publica tem que falhar, não aproximar."""
        r = calcular_odd_justa(parse_mercado("Resultado Correto: 4:3"),
                               matchup_com_especiais())
        self.assertFalse(r.ok)

    def test_chance_dupla_bate_com_o_1x2_devigado(self):
        """A chance dupla não é partição: os três desfechos somam ~2, não ~1.

        Normalizar pra 1 daria margem de ~109%, o mercado seria descartado
        como corrompido e a chance dupla nunca teria referência. A conferência
        honesta é contra o 1X2 da mesma partida, que é independente disto.
        """
        m = matchup_com_especiais()
        probs_1x2 = remover_vig(m.markets["h2h"])
        esperado = (probs_1x2["home"].probabilidade + probs_1x2["draw"].probabilidade)

        r = calcular_odd_justa(parse_mercado("Chance dupla: Celtic ou empate"), m)
        self.assertTrue(r.ok)
        self.assertAlmostEqual(1 / r.odd_justa, esperado, places=3)

    def test_chance_dupla_resolve_as_duas_ordens(self):
        """A Pinnacle fixa a ordem (mandante primeiro) e o parser não sabe
        quem é o mandante — manda as duas e só uma existe."""
        m = matchup_com_especiais()
        for texto in ("Chance dupla: Empate ou Dundee",
                      "Chance dupla: Dundee ou empate"):
            self.assertTrue(calcular_odd_justa(parse_mercado(texto), m).ok, texto)

    def test_dupla_chance_com_as_palavras_invertidas(self):
        """A CasaDeAposta escreve "Dupla chance"; era "mercado não reconhecido"."""
        m = matchup_com_especiais()
        self.assertTrue(
            calcular_odd_justa(parse_mercado("Dupla chance: Celtic ou empate"), m).ok)

    def test_chance_dupla_em_codigo(self):
        """A Betano escreve "Chance Dupla X2", sem os nomes."""
        m = matchup_com_especiais()
        probs = remover_vig(m.markets["h2h"])
        for texto, esperado in (
            ("Chance Dupla 1X", probs["home"].probabilidade + probs["draw"].probabilidade),
            ("Chance Dupla X2", probs["draw"].probabilidade + probs["away"].probabilidade),
            ("Chance Dupla 12", probs["home"].probabilidade + probs["away"].probabilidade),
        ):
            r = calcular_odd_justa(parse_mercado(texto), m)
            self.assertTrue(r.ok, texto)
            self.assertAlmostEqual(1 / r.odd_justa, esperado, places=3, msg=texto)

    def test_codigo_repetido_nao_e_chance_dupla(self):
        """"11" não é um lado duplo — não pode virar mercado."""
        self.assertFalse(parse_leg("Chance Dupla 11").suportado)


def matchup_gols_exatos_time() -> Matchup:
    """Corinthians x Internacional, com os DOIS mercados de gols exatos do
    1º tempo presentes ao mesmo tempo: o AMPLO (jogo inteiro) e o RESTRITO
    (só o Internacional, visitante).

    É a única forma de provar que o resolvedor escolhe o certo — se só o
    restrito existisse, a perna passaria mesmo com a chave errada (bastaria
    não existir `exact_goals_1t` pra "sem cobertura" virar "sem opção de
    errar"). Números reais do caso relatado (log de produção, 2026-08):
    boost 3.45, crua do Inter 3.820, justa errada 2.79 (calculada contra o
    mercado amplo) — a correta fica ACIMA da crua, nunca abaixo.
    """
    m = Matchup(id=2, league="Brasil - Série A", home_team="Corinthians",
                away_team="Internacional", commence_time=HOJE)
    m.markets["exact_goals_1t"] = Market(
        key="exact_goals_1t", label="Exact Total Goals 1st Half",
        outcomes={"0": 3.22, "1": 2.59, "2": 4.16, "3": 9.36, "4+": 20.54})
    m.markets["team_exact_goals_1t:away"] = Market(
        key="team_exact_goals_1t:away", label="Internacional Goals 1st Half",
        outcomes={"0": 1.346, "1": 3.820, "2": 17.400})
    return m


def matchup_gols_exatos_time_walsall() -> Matchup:
    """Walsall (mandante) x adversário — mesmo par de mercados, outro jogo.

    Segunda amostra pra provar que a correção generaliza e não é coincidência
    de um jogo só. Números do print do dono (2026-08).
    """
    m = Matchup(id=3, league="England - League Two", home_team="Walsall",
                away_team="Adversário", commence_time=HOJE)
    m.markets["exact_goals_1t"] = Market(
        key="exact_goals_1t", label="Exact Total Goals 1st Half",
        outcomes={"0": 3.22, "1": 2.59, "2": 4.16, "3": 9.36, "4+": 20.54})
    m.markets["team_exact_goals_1t:home"] = Market(
        key="team_exact_goals_1t:home", label="Walsall Goals 1st Half",
        outcomes={"0": 1.304, "1": 3.950, "2": 18.000})
    return m


class TestGolsExatosDeTime(unittest.TestCase):
    """Regressão do bug de 2026-08: "<Time> gols exatos: N" casando com o
    mercado do JOGO INTEIRO em vez do mercado restrito ao time citado.

    Ver `market_parser._parse_especiais` (regex ancorada) e
    `pinnacle._mapear_special`/`_resolver_team_goals` (mapeamento do special
    parametrizado por time). A skill `value-bet-methodology` documenta a
    classe: mercado restrito casado com referência ampla infla o edge.
    """

    def test_parser_reconhece_time_na_frente_de_gols_exatos(self):
        leg = parse_leg("1º tempo - Internacional gols exatos: 1")
        self.assertTrue(leg.suportado)
        self.assertTrue(leg.market_key.startswith("team_exact_goals_1t"))
        self.assertEqual(leg.time_nome, "Internacional")
        self.assertEqual(leg.selecao, "1")

    def test_justa_do_internacional_usa_o_mercado_do_time_nao_do_jogo(self):
        leg = parse_leg("1º tempo - Internacional gols exatos: 1")
        p = prob_da_perna(leg, matchup_gols_exatos_time())
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p.odd_justa, 4.058, places=2)
        self.assertGreater(p.odd_justa, 3.820, "justa tem que ficar acima da crua")

    def test_regressao_nunca_mais_2_79(self):
        """2.79 é a justa do mercado ERRADO (agregado). Nunca pode reaparecer."""
        leg = parse_leg("1º tempo - Internacional gols exatos: 1")
        p = prob_da_perna(leg, matchup_gols_exatos_time())
        self.assertIsNotNone(p)
        self.assertNotAlmostEqual(p.odd_justa, 2.79, places=1)

    def test_walsall_mesmo_padrao_outro_jogo(self):
        leg = parse_leg("1º tempo - Walsall gols exatos: 1")
        self.assertEqual(leg.time_nome, "Walsall")
        self.assertTrue(leg.market_key.startswith("team_exact_goals_1t"))
        p = prob_da_perna(leg, matchup_gols_exatos_time_walsall())
        self.assertIsNotNone(p)
        self.assertAlmostEqual(p.odd_justa, 4.249, places=2)
        self.assertGreater(p.odd_justa, 3.950)

    def test_sem_time_continua_pegando_o_mercado_do_jogo(self):
        """Caso base: sem time na frente, nada pode quebrar."""
        leg = parse_leg("1º tempo - gols exatos: 1")
        self.assertEqual(leg.market_key, "exact_goals_1t")
        self.assertIsNone(leg.time_nome)
        p = prob_da_perna(leg, matchup_gols_exatos_time())
        self.assertIsNotNone(p)
        # É o mercado amplo mesmo — não precisa bater com o valor do time.
        self.assertGreater(p.odd_justa, 2.59)

    def test_guarda_recusa_perna_com_time_sem_componente_de_time_na_chave(self):
        """Simula a CLASSE do bug diretamente: uma perna com `time_nome`
        preenchido mas `market_key` de mercado de partição sem sufixo de
        lado. Tem que morrer aqui, mesmo que o mercado exista na Pinnacle."""
        m = matchup_gols_exatos_time()
        m.markets["totals_1t:1.5"] = Market(
            key="totals_1t:1.5", label="Total de Gols 1.5 (1ºT)",
            outcomes={"over": 1.90, "under": 1.95})
        leg_ruim = Leg(texto="fake", market_key="totals_1t:1.5", selecao="over",
                       time_nome="Internacional", suportado=True)
        self.assertIsNone(prob_da_perna(leg_ruim, m))

    def test_guarda_nao_bloqueia_h2h_com_time_nome(self):
        """h2h/handicap usam `time_nome` pra escolher o LADO — não precisam
        (nem podem) ganhar sufixo de time na chave. A guarda não pode
        derrubar esse caso legítimo."""
        m = matchup_exemplo()
        leg = parse_leg("Resultado Final: Celtic")
        self.assertEqual(leg.time_nome, "Celtic")
        self.assertIsNotNone(prob_da_perna(leg, m))

    def test_todos_os_mercados_das_fixtures_tem_justa_maior_ou_igual_a_crua(self):
        """Generaliza `TestDeVig.test_odd_justa_e_sempre_maior_que_a_crua`
        pra TODOS os mercados de todas as fixtures, não só o h2h."""
        matchups = [matchup_exemplo(), matchup_com_especiais(),
                    matchup_gols_exatos_time(), matchup_gols_exatos_time_walsall()]
        checados = 0
        for m in matchups:
            for chave, market in m.markets.items():
                probs = remover_vig(market)
                if not probs:
                    continue
                for nome, odd_crua in market.outcomes.items():
                    checados += 1
                    self.assertGreaterEqual(
                        probs[nome].odd_justa, odd_crua - 1e-9,
                        msg=f"{chave}:{nome} — justa {probs[nome].odd_justa} "
                            f"< crua {odd_crua}")
        self.assertGreater(checados, 20, "cobertura da varredura caiu demais")


class TestModeloGols(unittest.TestCase):
    """A matriz de placares derivada do 1X2 + over/under."""

    def _matchup_sem_especiais(self) -> Matchup:
        """Jogo equilibrado, com só o que a Pinnacle publica em todo jogo."""
        m = Matchup(id=7, league="L", home_team="Casa", away_team="Fora",
                    commence_time=HOJE)
        m.markets["h2h"] = Market(key="h2h", label="1X2",
                                  outcomes={"home": 2.40, "draw": 3.59, "away": 2.91})
        m.markets["totals:2.5"] = Market(key="totals:2.5", label="Total 2.5",
                                         outcomes={"over": 1.95, "under": 1.95})
        return m

    def test_matriz_soma_um(self):
        m = matriz_placares(1.5, 1.2, -0.05)
        self.assertAlmostEqual(sum(sum(l) for l in m), 1.0, places=9)

    def test_ajuste_reproduz_o_1x2_que_o_alimentou(self):
        """Se o modelo não reproduz o mercado de onde saiu, não há motivo pra
        confiar no placar exato que ele inventa — é o que `MODELO_ERRO_MAX` faz."""
        matchup = self._matchup_sem_especiais()
        modelo = ajustar(matchup)
        self.assertIsNotNone(modelo)
        alvo = remover_vig(matchup.markets["h2h"])
        obtido = modelo.p_resultado()
        for lado in ("home", "draw", "away"):
            self.assertAlmostEqual(obtido[lado], alvo[lado].probabilidade, places=2)

    def test_ajuste_reproduz_o_over_under(self):
        """O 1X2 sozinho fixa a diferença entre os times, não o total de gols."""
        matchup = self._matchup_sem_especiais()
        modelo = ajustar(matchup)
        acima = sum(modelo.matriz[h][a] for h in range(9) for a in range(9)
                    if h + a > 2.5)
        self.assertAlmostEqual(acima, 0.5, places=2)

    def test_sem_1x2_nao_ha_modelo(self):
        m = Matchup(id=8, league="L", home_team="A", away_team="B",
                    commence_time=HOJE)
        self.assertIsNone(ajustar(m))

    def test_linha_de_quarto_e_ignorada(self):
        """3.25 é meia aposta em 3.0 e meia em 3.5 — a probabilidade dela é
        mistura, não `P(total > 3.25)`. Usá-la enviesava o λ."""
        m = self._matchup_sem_especiais()
        del m.markets["totals:2.5"]
        m.markets["totals:3.25"] = Market(key="totals:3.25", label="Total 3.25",
                                          outcomes={"over": 1.95, "under": 1.95})
        self.assertIsNone(_melhor_total(m, ""))

    def test_modelo_so_entra_quando_a_pinnacle_nao_publica(self):
        """Preço observado sempre ganha do derivado."""
        m = matchup_com_especiais()
        r = calcular_odd_justa(parse_mercado("Resultado Correto: 2:1"), m)
        self.assertEqual(r.fonte_odd, "pinnacle")
        self.assertFalse(r.derivada)

    def test_placar_sem_mercado_cai_no_modelo_e_marca_a_fonte(self):
        m = self._matchup_sem_especiais()
        r = calcular_odd_justa(parse_mercado("Resultado Correto: 1:1"), m)
        self.assertTrue(r.ok)
        self.assertTrue(r.derivada)
        self.assertEqual(r.fonte_odd, "modelo")

    def test_modelo_usa_threshold_proprio(self):
        """Erro de modelo em cima do erro de de-vig — o piso tem que subir."""
        self.assertGreater(config.EDGE_MIN_MODELO, config.EDGE_MIN_SIMPLES)
        self.assertEqual(
            threshold_para("simples", fonte_odd="modelo"), config.EDGE_MIN_MODELO)
        # E vence o de combo: quem manda é a perna pior.
        self.assertEqual(
            threshold_para("combo", fonte_odd="modelo"), config.EDGE_MIN_MODELO)

    def _oferta_de_modelo(self) -> tuple[dict, Matchup]:
        """Oferta cuja justa só pode vir do modelo, com edge folgado.

        11.0 (não 40.0 como antes): a justa do modelo pra este placar fica
        perto de 7.74, e 40.0 dava edge de ~417% — acima do NOVO teto de
        sanidade (EDGE_TETO_SANIDADE=50%, ver value_calc.py), que zeraria
        `is_value` incondicionalmente e mediria o filtro errado. 11.0 fica
        folgado acima de EDGE_MIN_MODELO (35%) e abaixo do teto de sanidade.
        """
        m = self._matchup_sem_especiais()
        return ({"evento": "Casa - Fora", "mercado": "Resultado Correto: 1:1",
                 "odd_boost": 11.0, "valido_ate": HOJE.isoformat()}, m)

    def test_trava_impede_edge_de_modelo_de_virar_alerta(self):
        """A trava é o que separa "cobertura" de "aposta".

        Com o modelo ainda não calibrado, a oferta é avaliada, o edge é
        calculado e gravado — mas `is_value` sai falso, e é `is_value` que o
        alertador lê. Ligar isso sem calibrar é como nasce edge falso.
        """
        oferta, matchup = self._oferta_de_modelo()
        original = config.MODELO_ALERTA_ATIVO
        # Placar exato é `simples` e a justa passa longe de 5 — o ODD_JUSTA_MAX
        # barraria a oferta antes da trava e o teste mediria o filtro errado.
        # Aqui interessa só a trava do modelo; o teto tem teste próprio.
        teto = config.ODD_JUSTA_MAX
        try:
            config.ODD_JUSTA_MAX = 0
            config.MODELO_ALERTA_ATIVO = False
            r = avaliar_oferta(oferta, [matchup])
            self.assertEqual(r["status"], "avaliada")
            self.assertEqual(r["fonte_odd"], "modelo")
            self.assertGreater(r["edge_pct"], config.EDGE_MIN_MODELO)
            self.assertFalse(r["is_value"], "modelo alertou com a trava ligada")

            config.MODELO_ALERTA_ATIVO = True
            self.assertTrue(avaliar_oferta(oferta, [matchup])["is_value"])
        finally:
            config.MODELO_ALERTA_ATIVO = original
            config.ODD_JUSTA_MAX = teto

    def _favorito_pesado(self) -> Matchup:
        """Jogo desequilibrado: empate raro, muitos gols esperados."""
        m = Matchup(id=9, league="L", home_team="Forte", away_team="Fraco",
                    commence_time=HOJE)
        m.markets["h2h"] = Market(key="h2h", label="1X2",
                                  outcomes={"home": 1.12, "draw": 9.50, "away": 21.0})
        m.markets["totals:2.5"] = Market(key="totals:2.5", label="Total 2.5",
                                         outcomes={"over": 1.40, "under": 3.00})
        m.markets["btts"] = Market(key="btts", label="Both Teams To Score?",
                                   outcomes={"Yes": 2.30, "No": 1.65})
        return m

    def test_combo_correlacionado_nao_passa_por_independencia(self):
        """Regressão do +60.2% falso de 2026-08-04.

        "1x2: Empate + Total: Mais de 2.5" num jogo com favorito pesado:
        independência dava justa 14.98 contra odd 24 e virava VALUE. Mas
        empate com 3+ gols é 2-2 ou 3-3 — bem mais raro que o produto sugere.
        O erro da independência é SEMPRE na direção que infla o edge.
        """
        m = self._favorito_pesado()
        legs = parse_mercado("1x2: Empate + Total: Mais de 2.5")
        r = calcular_odd_justa(legs, m)
        self.assertTrue(r.ok)

        produto = 1.0
        for perna in r.pernas:
            if not perna.derivada:
                produto *= perna.probabilidade
        self.assertLess(1 / r.odd_justa, produto,
                        "a conjunta corrigida tem que ser MENOR que o produto")
        self.assertEqual(r.fonte_odd, "modelo")

    def test_correlacao_nunca_melhora_a_odd_do_apostador(self):
        """A correção fica com a MENOR das duas de propósito. Deixar o modelo
        aumentar a probabilidade seria trocar preço observado por estimativa
        em troca de um edge maior — exatamente o negócio errado."""
        m = self._favorito_pesado()
        # Pernas positivamente correlacionadas: o modelo daria conjunta MAIOR.
        legs = parse_mercado("1x2: 1 + Total: Mais de 2.5")
        r = calcular_odd_justa(legs, m)
        self.assertTrue(r.ok)
        produto = 1.0
        for perna in r.pernas:
            if not perna.derivada:
                produto *= perna.probabilidade
        # `odd_justa` é arredondada a 4 casas, então a folga é do arredondamento.
        self.assertLessEqual(1 / r.odd_justa, produto * 1.001)
        self.assertEqual(r.fonte_odd, "pinnacle", "não devia ter usado o modelo")

    def test_correcao_alcanca_o_subconjunto_que_a_matriz_cobre(self):
        """Escanteios a matriz não expressa — mas as pernas de gols sim, e é
        entre elas que mora a pior correlação. Meia correção > nenhuma."""
        m = self._favorito_pesado()
        m.markets["corners:9.5"] = Market(key="corners:9.5", label="Escanteios",
                                          outcomes={"over": 1.90, "under": 1.95})
        legs = parse_mercado(
            "Total: Mais de 2.5 + Ambas equipes marcam: Não + Escanteios Mais de 9.5")
        r = calcular_odd_justa(legs, m)
        self.assertTrue(r.ok)
        self.assertEqual(r.fonte_odd, "modelo")

    def test_stake_de_modelo_leva_desconto(self):
        com = stake.calcular(3.0, 2.0, tipo_mercado="simples", derivada=True)
        sem = stake.calcular(3.0, 2.0, tipo_mercado="simples", derivada=False)
        self.assertLess(com.unidades, sem.unidades)
        self.assertIn("modelo", com.descontos)


class TestOddJusta(unittest.TestCase):
    def test_simples(self):
        legs = parse_mercado("Resultado Final: Celtic")
        r = calcular_odd_justa(legs, matchup_exemplo())
        self.assertTrue(r.ok)
        self.assertEqual(r.tipo_mercado, "simples")
        self.assertAlmostEqual(r.odd_justa, 1.22, places=2)

    def test_combo_multiplica_probabilidades(self):
        legs = parse_mercado("Total de Gols Mais de 2.5 + Escanteios Mais de 9.5")
        r = calcular_odd_justa(legs, matchup_exemplo())
        self.assertTrue(r.ok)
        self.assertEqual(r.tipo_mercado, "combo")
        # produto das probabilidades de-vigadas de cada perna
        p = 1.0
        for perna in r.pernas:
            p *= perna.probabilidade
        self.assertAlmostEqual(r.odd_justa, 1 / p, places=3)
        self.assertGreater(r.odd_justa, 1.30, "combo tem que pagar mais que uma perna só")

    def test_perna_sem_cobertura_bloqueia_tudo(self):
        legs = parse_mercado("Resultado Final 1 + Total de Cartões Mais de 5.5")
        r = calcular_odd_justa(legs, matchup_exemplo())
        self.assertFalse(r.ok)
        self.assertIsNone(r.odd_justa, "não pode inventar número")
        self.assertIn("cartões", r.motivo_falha)

    def test_mercado_ausente_no_jogo_bloqueia(self):
        legs = parse_mercado("Total de Gols Mais de 4.5")  # linha que o jogo não tem
        r = calcular_odd_justa(legs, matchup_exemplo())
        self.assertFalse(r.ok)
        self.assertIn("sem odd na Pinnacle", r.motivo_falha)

    def test_combo_multi_jogo_usa_o_matchup_de_cada_perna(self):
        """Regressão do "Festival de Gols" (Novibet, 2026-08-07): 3 pernas
        "Mais de 2,5" em 3 jogos diferentes eram todas precificadas contra o
        MESMO matchup (o do primeiro jogo), multiplicando a mesma
        probabilidade 3x — edge fantasma. Com `matchups_por_jogo`, cada
        perna usa a probabilidade do SEU jogo."""
        jogo_a = matchup_exemplo()  # over 2.5 -> p ~ 0.667 (odd crua 1.30)
        jogo_b = Matchup(id=2, league="X", home_team="Tigres UANL",
                         away_team="Minnesota United FC", commence_time=HOJE)
        jogo_b.markets["totals:2.5"] = Market(
            key="totals:2.5", label="Total de Gols 2.5",
            outcomes={"over": 2.20, "under": 1.65})  # over bem menos provável

        legs = parse_mercado(
            "[Celtic - Dundee] Total de Gols Mais de 2.5 + "
            "[Tigres UANL - Minnesota United FC] Total de Gols Mais de 2.5"
        )
        matchups_por_jogo = {
            "Celtic - Dundee": jogo_a,
            "Tigres UANL - Minnesota United FC": jogo_b,
        }
        r = calcular_odd_justa(legs, jogo_a, matchups_por_jogo=matchups_por_jogo)
        self.assertTrue(r.ok, r.motivo_falha)

        # A justa correta é o produto das duas probabilidades DE-VIGADAS,
        # cada uma do seu próprio jogo — não o quadrado da probabilidade de
        # um jogo só (o bug: usar `jogo_a` pras duas pernas).
        p_a = prob_da_perna(legs[0], jogo_a).probabilidade
        p_b = prob_da_perna(legs[1], jogo_b).probabilidade
        justa_certa = 1.0 / (p_a * p_b)
        justa_errada_bug = 1.0 / (p_a * p_a)
        self.assertAlmostEqual(r.odd_justa, justa_certa, places=3)
        self.assertNotAlmostEqual(r.odd_justa, justa_errada_bug, places=1)

    def test_combo_multi_jogo_sem_mapa_nao_muda_comportamento(self):
        """`matchups_por_jogo` ausente (o caso comum, oferta de jogo único)
        tem que dar exatamente o resultado de sempre — mudança aditiva."""
        legs = parse_mercado("Total de Gols Mais de 2.5 + Escanteios Mais de 9.5")
        com_none = calcular_odd_justa(legs, matchup_exemplo(), matchups_por_jogo=None)
        sem_arg = calcular_odd_justa(legs, matchup_exemplo())
        self.assertEqual(com_none.odd_justa, sem_arg.odd_justa)


class TestValueCalc(unittest.TestCase):
    def test_edge(self):
        self.assertAlmostEqual(calcular_edge(5.00, 3.85), 29.87, places=1)
        self.assertAlmostEqual(calcular_edge(2.00, 2.00), 0.0, places=6)

    def test_threshold_simples(self):
        self.assertTrue(avaliar_value(2.20, 2.00, "simples").is_value)   # +10%
        self.assertFalse(avaliar_value(2.05, 2.00, "simples").is_value)  # +2.5%

    def test_combo_exige_mais_edge(self):
        """Mesmo edge: passa em simples, reprova em combo."""
        edge_10 = (2.20, 2.00)
        self.assertTrue(avaliar_value(*edge_10, "simples").is_value)
        self.assertFalse(avaliar_value(*edge_10, "combo").is_value)
        self.assertGreater(
            avaliar_value(*edge_10, "combo").threshold_usado,
            avaliar_value(*edge_10, "simples").threshold_usado,
        )

    def test_edge_negativo(self):
        r = avaliar_value(1.80, 2.00, "simples")
        self.assertLess(r.edge_pct, 0)
        self.assertFalse(r.is_value)


class TestPipeline(unittest.TestCase):
    def _oferta(self, mercado="Resultado Final: Celtic", boost=1.50):
        return {"evento": "Celtic - Dundee FC", "mercado": mercado,
                "odd_original": 1.19, "odd_boost": boost,
                "valido_ate": HOJE.isoformat(), "url": "http://x"}

    def test_avalia_fim_a_fim(self):
        r = avaliar_oferta(self._oferta(), [matchup_exemplo()])
        self.assertEqual(r["status"], "avaliada")
        self.assertAlmostEqual(r["odd_justa"], 1.22, places=2)
        self.assertTrue(r["is_value"], "1.50 contra justa 1.22 é value claro")
        self.assertEqual(r["fonte_odd"], "pinnacle")

    def test_preserva_campos_da_oferta(self):
        r = avaliar_oferta(self._oferta(), [matchup_exemplo()])
        self.assertEqual(r["evento"], "Celtic - Dundee FC")
        self.assertEqual(r["url"], "http://x")

    def test_sem_match_de_evento(self):
        outro = Matchup(id=2, league="L", home_team="Grêmio", away_team="Internacional",
                        commence_time=HOJE)
        r = avaliar_oferta(self._oferta(), [outro])
        self.assertEqual(r["status"], "sem_match_evento")
        self.assertIsNone(r["odd_justa"])
        self.assertIsNone(r["is_value"])

    def test_sem_odd_justa_quando_perna_nao_tem_referencia(self):
        r = avaliar_oferta(self._oferta(mercado="Total de Cartões Mais de 5.5"),
                           [matchup_exemplo()])
        self.assertEqual(r["status"], "sem_odd_justa")
        self.assertIsNone(r["odd_justa"])

    def _jogo_b(self) -> Matchup:
        m = Matchup(id=2, league="X", home_team="Tigres UANL",
                    away_team="Minnesota United FC", commence_time=HOJE)
        m.markets["totals:2.5"] = Market(key="totals:2.5", label="Total de Gols 2.5",
                                         outcomes={"over": 2.20, "under": 1.65})
        return m

    def test_multi_jogo_casa_cada_perna_com_seu_jogo(self):
        """Cada perna com prefixo "[Jogo] " casa com O SEU matchup — não
        com o da oferta. `matches_por_jogo` sai preenchido no resultado."""
        mercado = ("[Celtic - Dundee] Total de Gols Mais de 2.5 + "
                  "[Tigres UANL - Minnesota United FC] Total de Gols Mais de 2.5")
        r = avaliar_oferta(self._oferta(mercado=mercado, boost=6.0),
                           [matchup_exemplo(), self._jogo_b()])
        self.assertEqual(r["status"], "avaliada")
        self.assertEqual(len(r["matches_por_jogo"]), 2)
        jogos = {m["jogo"] for m in r["matches_por_jogo"]}
        self.assertEqual(jogos, {"Celtic - Dundee", "Tigres UANL - Minnesota United FC"})

    def test_multi_jogo_uma_perna_sem_match_derruba_a_oferta_inteira(self):
        """Combo meio-casado é exatamente o cenário que fabrica edge falso
        — se UM jogo do combo não casa, a oferta inteira cai, mesmo que a
        outra perna tivesse referência perfeita."""
        mercado = ("[Celtic - Dundee] Total de Gols Mais de 2.5 + "
                  "[Time Fantasma - Outro Fantasma] Total de Gols Mais de 2.5")
        r = avaliar_oferta(self._oferta(mercado=mercado, boost=6.0),
                           [matchup_exemplo()])
        self.assertEqual(r["status"], "sem_match_evento")
        self.assertIn("Time Fantasma", r["motivo"])
        self.assertIsNone(r["odd_justa"])


class StorageFake:
    """Devolve linhas de `mercados_casa` sem banco.

    A assinatura é a de `Storage.mercados_para_consenso` — se ela mudar, estes
    testes param de refletir a realidade e é bom que quebrem.
    """

    def __init__(self, linhas: list[dict]) -> None:
        self.linhas = linhas
        self.pedidos: list[tuple[str, float]] = []

    def mercados_para_consenso(self, evento_id: str, janela_horas: float):
        self.pedidos.append((evento_id, janela_horas))
        return list(self.linhas)


def _linhas_casa(casa: str, over: float, under: float,
                 mercado: str = "Total de cartões 3.5",
                 linha: str = "3.5", market_id: str = "1") -> list[dict]:
    return [
        {"casa": casa, "market_id": market_id, "market_nome": mercado,
         "selecao": f"Mais de {linha}", "preco": over},
        {"casa": casa, "market_id": market_id, "market_nome": mercado,
         "selecao": f"Menos de {linha}", "preco": under},
    ]


class TestConsenso(unittest.TestCase):
    """A referência de quando a Pinnacle não publica o mercado.

    Ela não publica prop nenhum no futebol (conferido na API: só mercado de
    gol), e é exatamente onde mora ~25% do que o usuário aposta. O consenso é
    uma referência mais fraca de propósito — os testes aqui cuidam de que ela
    seja fraca de um jeito previsível, não de que seja boa.
    """

    def _tres_casas(self) -> list[dict]:
        """Nome histórico, conteúdo hoje maior: `CONSENSO_MIN_CASAS` subiu de
        3 pra 5 e "Total de cartões" é classe `prop`
        (`CONSENSO_MIN_CASAS_PROP=6`) — ver value/config.py e
        `consenso._eh_mercado_prop`. Seis casas, preços espalhados de
        propósito: o ponto de vários destes testes é distinguir CASA de
        PREÇO (`n_casas` x `n_precos`).
        """
        return (_linhas_casa("VaiDeBet", 1.90, 1.90)
                + _linhas_casa("EstrelaBet", 1.95, 1.85)
                + _linhas_casa("vupi", 1.88, 1.92)
                + _linhas_casa("BateuBet", 1.92, 1.88)
                + _linhas_casa("4Play", 1.87, 1.93)
                + _linhas_casa("GingaBet", 1.93, 1.87))

    def test_mediana_de_tres_casas(self):
        r = consenso.calcular(self._tres_casas(), "Total de cartões 3.5",
                              "Mais de 3.5")
        self.assertIsNotNone(r)
        self.assertEqual(r.n_casas, 6)
        self.assertTrue(r.prob.consenso, "a perna tem que se declarar consenso")
        # Preços espalhados simetricamente em torno de 1.90/1.90 (justa 2.00).
        self.assertAlmostEqual(r.prob.odd_justa, 2.0, delta=0.06)

    def test_abaixo_do_minimo_nao_forma_consenso(self):
        """Duas casas não são consenso — e Altenar copia catálogo entre casas."""
        original = config.CONSENSO_MIN_CASAS
        try:
            config.CONSENSO_MIN_CASAS = 3
            linhas = _linhas_casa("VaiDeBet", 1.90, 1.90) + _linhas_casa("vupi", 1.90, 1.90)
            self.assertIsNone(consenso.calcular(linhas, "Total de cartões 3.5",
                                                "Mais de 3.5"))
        finally:
            config.CONSENSO_MIN_CASAS = original

    def test_casa_maluca_nao_arrasta_a_mediana(self):
        """O motivo de ser mediana e não média."""
        normal = consenso.calcular(self._tres_casas(), "Total de cartões 3.5",
                                   "Mais de 3.5")
        com_maluca = consenso.calcular(
            self._tres_casas() + _linhas_casa("BetGorillas", 12.0, 1.02),
            "Total de cartões 3.5", "Mais de 3.5")
        self.assertEqual(com_maluca.n_casas, 7)
        self.assertAlmostEqual(normal.prob.odd_justa, com_maluca.prob.odd_justa,
                               delta=0.15)

    def test_mercado_de_um_lado_so_e_ignorado(self):
        """Sem os dois lados não dá pra medir a margem — a casa sai da conta."""
        meio = [{"casa": "BetPix365", "market_id": "1",
                 "market_nome": "Total de cartões 3.5",
                 "selecao": "Mais de 3.5", "preco": 1.90}]
        r = consenso.calcular(self._tres_casas() + meio,
                              "Total de cartões 3.5", "Mais de 3.5")
        self.assertEqual(r.n_casas, 6, "casa sem os dois lados entrou no consenso")

    def test_a_propria_casa_sai_da_conta(self):
        """A casa avaliada não pode ser sua própria referência: incluí-la puxa
        a mediana na direção da oferta e encolhe o edge.

        Sete casas de propósito — excluindo uma ainda sobram as 6 do mínimo de
        prop, senão o teste passaria pelo motivo errado (falta de casas, não
        exclusão).
        """
        sete = self._tres_casas() + _linhas_casa("OutraBet", 1.90, 1.90)
        r = consenso.calcular(sete, "Total de cartões 3.5", "Mais de 3.5",
                              excluir_casa="vupi")
        self.assertEqual(r.n_casas, 6)
        self.assertNotIn("vupi", r.casas)

    def test_excluir_casa_pode_derrubar_o_consenso(self):
        """Com 6 casas (mínimo de prop) e uma sendo a própria, sobram 5 —
        abaixo do mínimo de 6 pra cartão."""
        self.assertIsNone(consenso.calcular(
            self._tres_casas(), "Total de cartões 3.5", "Mais de 3.5",
            excluir_casa="vupi"))

    def test_grafia_diferente_entre_casas_ainda_casa(self):
        """As casas compartilham catálogo, não a grafia."""
        linhas = (_linhas_casa("VaiDeBet", 1.90, 1.90, "Total de cartões 3.5")
                  + _linhas_casa("EstrelaBet", 1.95, 1.85, "Total de Cartões 3.5")
                  + _linhas_casa("vupi", 1.88, 1.92, "TOTAL DE CARTOES 3.5")
                  + _linhas_casa("BateuBet", 1.92, 1.88, "total de cartoes 3.5")
                  + _linhas_casa("4Play", 1.87, 1.93, "Total de Cartoes 3.5")
                  + _linhas_casa("GingaBet", 1.93, 1.87, "total De Cartões 3.5"))
        r = consenso.calcular(linhas, "Total de Cartões 3.5", "mais de 3.5")
        self.assertIsNotNone(r)
        self.assertEqual(r.n_casas, 6)

    def test_outra_linha_do_mesmo_mercado_nao_polui_o_de_vig(self):
        """A casa publica "Total de escanteios" uma vez por linha, todas com o
        mesmo nome. Agrupar por nome daria um mercado de 4 lados, margem de
        ~100%, e o de-vig recusaria tudo — o consenso morreria calado.

        Escanteios não é classe `prop` (só cartão/chute ao gol/artilheiro/
        handicap são) — usa o mínimo genérico (`CONSENSO_MIN_CASAS=5`), não o
        de prop.
        """
        linhas = []
        for casa in ("VaiDeBet", "EstrelaBet", "vupi", "BateuBet", "4Play"):
            linhas += _linhas_casa(casa, 1.90, 1.90, "Total de escanteios",
                                   linha="9.5", market_id="33")
            linhas += _linhas_casa(casa, 2.40, 1.50, "Total de escanteios",
                                   linha="10.5", market_id="34")
        r = consenso.calcular(linhas, "Total de escanteios", "Mais de 9.5")
        self.assertIsNotNone(r, "as duas linhas se anularam no de-vig")
        self.assertEqual(r.n_casas, 5)
        self.assertAlmostEqual(r.prob.odd_justa, 2.0, delta=0.05)

    def test_conta_precos_distintos_e_nao_so_casas(self):
        """O número que importa pra confiar no consenso.

        Medido no banco: 90.3% das seleções cotadas por 2+ casas Altenar têm
        preço IDÊNTICO — elas são skins do mesmo feed. "N casas" com um preço
        só é uma fonte contada N vezes, e o alerta tem que dizer isso.
        """
        mesmo_feed = (_linhas_casa("VaiDeBet", 1.90, 1.90)
                      + _linhas_casa("EstrelaBet", 1.90, 1.90)
                      + _linhas_casa("4Play", 1.90, 1.90)
                      + _linhas_casa("BateuBet", 1.90, 1.90)
                      + _linhas_casa("GingaBet", 1.90, 1.90)
                      + _linhas_casa("vupi", 1.90, 1.90))
        r = consenso.calcular(mesmo_feed, "Total de cartões 3.5", "Mais de 3.5")
        self.assertEqual(r.n_casas, 6)
        self.assertEqual(r.n_precos, 1, "seis skins do mesmo feed não são 6 fontes")
        self.assertFalse(r.independente)

        r2 = consenso.calcular(self._tres_casas(), "Total de cartões 3.5",
                               "Mais de 3.5")
        self.assertEqual(r2.n_precos, 6)
        self.assertTrue(r2.independente)

    def test_threshold_de_consenso_e_maior_que_o_de_pinnacle(self):
        """Casa mole pode estar errada junto — o piso tem que subir."""
        self.assertGreater(config.EDGE_MIN_CONSENSO, config.EDGE_MIN_SIMPLES)
        self.assertEqual(threshold_para("simples", fonte_odd="consenso"),
                         config.EDGE_MIN_CONSENSO)


class TestConsensoNoPipeline(unittest.TestCase):
    """Fim a fim: prop que hoje morre em `sem cobertura` vira oferta avaliada."""

    def _oferta_de_cartao(self, boost=2.85):
        # 2.85 (não 3.60 como antes): cartão é classe `prop` — edge tem que
        # ficar acima de EDGE_MIN_PROP (30%) mas abaixo de EDGE_TETO_SANIDADE
        # (50%), senão o teto de sanidade marca a oferta como suspeita em vez
        # de deixá-la passar como value limpo (ver value_calc.py).
        return {"evento": "Celtic - Dundee FC", "evento_id": "777",
                "casa": "Betano", "mercado": "Total de cartões 3.5: Mais de 3.5",
                "odd_original": 2.50, "odd_boost": boost,
                "valido_ate": HOJE.isoformat(), "url": "http://x"}

    def _storage(self):
        # Seis casas: "Total de cartões" é classe `prop`
        # (`CONSENSO_MIN_CASAS_PROP=6`), não o mínimo genérico de 5.
        return StorageFake(_linhas_casa("VaiDeBet", 1.90, 1.90)
                           + _linhas_casa("EstrelaBet", 1.95, 1.85)
                           + _linhas_casa("vupi", 1.88, 1.92)
                           + _linhas_casa("BateuBet", 1.92, 1.88)
                           + _linhas_casa("4Play", 1.87, 1.93)
                           + _linhas_casa("GingaBet", 1.93, 1.87))

    def test_sem_storage_continua_morrendo_por_cobertura(self):
        """O comportamento antigo tem que sobreviver intacto sem consenso."""
        r = avaliar_oferta(self._oferta_de_cartao(), [matchup_exemplo()])
        self.assertEqual(r["status"], "sem_odd_justa")
        self.assertIn("cartões", r["motivo"])

    def test_com_consenso_a_oferta_e_avaliada(self):
        r = avaliar_oferta(self._oferta_de_cartao(), [matchup_exemplo()],
                           storage=self._storage())
        self.assertEqual(r["status"], "avaliada")
        self.assertEqual(r["fonte_odd"], "consenso")
        self.assertEqual(r["n_casas_consenso"], 6)
        self.assertTrue(r["por_consenso"])
        # Cartão é classe `prop`: o piso real é EDGE_MIN_PROP (30%), não o
        # EDGE_MIN_CONSENSO (8%) genérico — 2.85 contra justa ~2.00 supera os
        # dois com folga, sem passar do teto de sanidade (50%).
        self.assertEqual(r["classe_mercado"], "prop")
        self.assertGreater(r["edge_pct"], config.EDGE_MIN_PROP)
        self.assertIsNone(r["flag"])
        self.assertEqual(r["confianca"], "baixa")
        self.assertTrue(r["is_value"])

    def test_usa_o_threshold_de_consenso_e_nao_o_de_simples(self):
        """Edge que passaria num mercado da Pinnacle não basta aqui."""
        # justa ~2.00; 2.20 dá ~+10%: acima de EDGE_MIN_SIMPLES (5%), abaixo
        # do piso real de cartão (EDGE_MIN_PROP, 30% — cartão é classe `prop`).
        r = avaliar_oferta(self._oferta_de_cartao(boost=2.20),
                           [matchup_exemplo()], storage=self._storage())
        self.assertEqual(r["fonte_odd"], "consenso")
        self.assertGreater(r["edge_pct"], config.EDGE_MIN_SIMPLES)
        self.assertLess(r["edge_pct"], config.EDGE_MIN_PROP)
        self.assertFalse(r["is_value"])

    def test_pinnacle_tem_precedencia_sobre_consenso(self):
        """Consenso é o que se usa na falta de fonte sharp, não no lugar dela."""
        oferta = {"evento": "Celtic - Dundee FC", "evento_id": "777",
                  "casa": "Betano", "mercado": "Resultado Final: Celtic",
                  "odd_original": 1.19, "odd_boost": 1.50,
                  "valido_ate": HOJE.isoformat(), "url": "http://x"}
        r = avaliar_oferta(oferta, [matchup_exemplo()], storage=self._storage())
        self.assertEqual(r["fonte_odd"], "pinnacle")
        self.assertFalse(r["por_consenso"])

    def test_consenso_desconta_o_stake(self):
        r = avaliar_oferta(self._oferta_de_cartao(), [matchup_exemplo()],
                           storage=self._storage())
        self.assertIn("consenso de casas", r["stake_descricao"])

    def test_janela_pedida_e_a_configurada(self):
        st = self._storage()
        avaliar_oferta(self._oferta_de_cartao(), [matchup_exemplo()], storage=st)
        self.assertEqual(st.pedidos[0], ("777", config.CONSENSO_JANELA_HORAS))


class TestTetoDeOddJusta(unittest.TestCase):
    """O teto que impede o feed de virar só zebra.

    Celtic x Dundee é o caso exato da reclamação: "melhor time do mundo x time
    do bairro, aposte no bairro". Dundee paga 13.94 na Pinnacle e sai com justa
    ~14.6 — qualquer boost em cima disso gera edge alto e aposta que não faz
    sentido nenhum.
    """

    def _zebra(self, boost=20.0):
        return {"evento": "Celtic - Dundee FC", "mercado": "Resultado Final: Dundee",
                "odd_original": 15.0, "odd_boost": boost,
                "valido_ate": HOJE.isoformat(), "url": "http://x"}

    def test_teto_barra_zebra(self):
        """Avalia, calcula e grava o edge — mas `is_value` sai falso."""
        original = config.ODD_JUSTA_MAX
        try:
            config.ODD_JUSTA_MAX = 5.0
            r = avaliar_oferta(self._zebra(), [matchup_exemplo()])
            self.assertEqual(r["status"], "avaliada")
            self.assertGreater(r["odd_justa"], 5.0)
            self.assertGreater(r["edge_pct"], config.EDGE_MIN_SIMPLES,
                               "o caso só tem graça se o edge passaria sem o teto")
            self.assertFalse(r["is_value"], "zebra alertou com o teto ligado")

            # Sobe o teto acima da justa: o mesmo edge volta a valer. Prova que
            # quem barrou foi o teto, não outro filtro do caminho.
            config.ODD_JUSTA_MAX = 30.0
            self.assertTrue(avaliar_oferta(self._zebra(), [matchup_exemplo()])["is_value"])
        finally:
            config.ODD_JUSTA_MAX = original

    def test_zero_desliga_o_teto(self):
        original = config.ODD_JUSTA_MAX
        try:
            config.ODD_JUSTA_MAX = 0
            self.assertTrue(avaliar_oferta(self._zebra(), [matchup_exemplo()])["is_value"])
        finally:
            config.ODD_JUSTA_MAX = original

    def test_ofertada_alta_com_justa_baixa_continua_passando(self):
        """O corte é na JUSTA, não na ofertada — senão vira teto de odd.

        "Menos de 2.5" tem justa ~3.8; ofertada 5.60 fica acima do teto de 5.0
        e ainda assim tem que alertar. É a aposta que o usuário quer manter.
        (5.60, não 6.00 como antes: 6.00 dava edge de 59%, acima do NOVO teto
        de sanidade — EDGE_TETO_SANIDADE=50% — que é um filtro DIFERENTE
        deste, com teste próprio em TestTetoDeSanidade. Este teste é só sobre
        justa-vs-ofertada.)
        """
        oferta = {"evento": "Celtic - Dundee FC", "mercado": "Total: Menos de 2.5",
                  "odd_original": 4.50, "odd_boost": 5.60,
                  "valido_ate": HOJE.isoformat(), "url": "http://x"}
        original = config.ODD_JUSTA_MAX
        try:
            config.ODD_JUSTA_MAX = 5.0
            r = avaliar_oferta(oferta, [matchup_exemplo()])
            self.assertEqual(r["status"], "avaliada")
            self.assertLess(r["odd_justa"], 5.0)
            self.assertGreater(r["odd_boost"], 5.0, "a ofertada tem que estar acima do teto")
            self.assertTrue(r["is_value"])
        finally:
            config.ODD_JUSTA_MAX = original

    def test_combo_nao_e_afetado(self):
        """No combo a justa é o produto das pernas e sai alta por construção.

        Quem segura combo é o EDGE_MIN_COMBO, não este teto.
        """
        # Pernas sem correlação de gols de propósito: combo de 1X2 + total de
        # gols leva correção do modelo, cai em `fonte_odd=modelo` e morre na
        # trava do MODELO_ALERTA_ATIVO — o teto nem chegaria a ser exercido.
        oferta = {"evento": "Celtic - Dundee FC",
                  "mercado": "Resultado Final: Dundee + Escanteios Mais de 9.5",
                  "odd_original": 25.0, "odd_boost": 40.0,
                  "valido_ate": HOJE.isoformat(), "url": "http://x"}
        original = config.ODD_JUSTA_MAX
        try:
            config.ODD_JUSTA_MAX = 5.0
            r = avaliar_oferta(oferta, [matchup_exemplo()])
            self.assertEqual(r["status"], "avaliada")
            self.assertEqual(r["tipo_mercado"], "combo")
            self.assertGreater(r["odd_justa"], 5.0)
            self.assertTrue(r["is_value"], "o teto não pode alcançar combo")
        finally:
            config.ODD_JUSTA_MAX = original


class TestSegundoTempoNaoCasaComOJogoInteiro(unittest.TestCase):
    """A Pinnacle só publica período 0 (jogo inteiro) e 1 (1º tempo/intervalo)
    — não existe bloco de 2º tempo pra referenciar. Achado ao vivo em
    2026-08-05 (revalidação de adapters): sem esta guarda, `_sufixo()` só
    reconhece o qualificador de 1º tempo e um texto de 2º tempo cai com
    sufixo "" — casando contra o mercado do JOGO INTEIRO por engano, a mesma
    classe de bug do falso +141,8% do Mirassol (mercado restrito casado com
    referência ampla). Medido no banco real: 52 pernas de 2º tempo em pelo
    menos 3 casas (EstrelaBet, BetPix365, CasaDeAposta) seriam casadas
    indevidamente sem esta guarda."""

    def test_variantes_de_2o_tempo_sao_recusadas(self):
        for texto in [
            "2º tempo - ambas equipes marcam: Sim",       # EstrelaBet -> btts
            "2º tempo - total: Mais de 1.5",              # BetPix365 -> totals
            "2º tempo - Mallorca total: Mais de 0.5",     # BetPix365 -> team_total
            "Vencedor (2º tempo): Mirassol",              # CasaDeAposta -> h2h
            "Total de gols (2º tempo): Menos de 1.5",     # CasaDeAposta -> totals
            "Segundo tempo - total de gols: Mais de 0.5",
        ]:
            leg = self._legs(texto)
            self.assertFalse(leg.suportado, texto)

    def test_1o_tempo_continua_funcionando(self):
        """A guarda não pode confundir "1º tempo" com "2º tempo" — "1x2"
        contém um "2", por exemplo, e não pode disparar a rejeição."""
        for texto, chave in [
            ("1º tempo - total de gols: Mais de 0.5", "totals_1t:0.5"),
            ("1º tempo - 1x2: Empate", "h2h_1t"),
        ]:
            leg = self._legs(texto)
            self.assertTrue(leg.suportado, texto)
            self.assertEqual(leg.market_key, chave)

    @staticmethod
    def _legs(texto):
        from betano_superodds.value.market_parser import parse_leg
        return parse_leg(texto)


class TestGrafiasReaisDaNovibetCasaDeApostaESportingTech(unittest.TestCase):
    """Achados reais na revalidação de adapters de 2026-08-05: cada casa
    própria (Novibet, CasaDeAposta, SportingTech) fala português (e às vezes
    inglês) de um jeito que o parser, calibrado pra Betano/Altenar, não
    previa. Nenhum mercado novo na Pinnacle — só grafia."""

    @staticmethod
    def _leg(texto):
        from betano_superodds.value.market_parser import parse_leg
        return parse_leg(texto)

    # --- "Resultado:" bare e "ou empate" (CasaDeAposta) --------------------

    def test_resultado_bare_vira_h2h(self):
        leg = self._leg("Resultado: Remo")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h", "Remo"))

    def test_resultado_ou_empate_vira_dupla_chance_nao_h2h(self):
        """Sem isto, "Resultado: Juventude ou empate" viraria h2h com
        time_nome "Juventude ou empate" — o fuzzy match parcial em
        "Juventude" poderia até "funcionar" por acidente, mas pra um mercado
        ERRADO (vitória simples em vez de dupla chance)."""
        leg = self._leg("Resultado: Juventude ou empate")
        self.assertEqual(leg.market_key, "double_chance")
        self.assertNotEqual(leg.market_key, "h2h")

    # --- "Vencedor" com qualificador entre o verbo e o ":" ------------------

    def test_vencedor_com_qualificador_entre_parenteses(self):
        """"Vencedor (1º tempo): Grêmio" (CasaDeAposta) — antes o time_nome
        saía "(1º tempo): Grêmio", que nunca casa com time nenhum."""
        leg = self._leg("Vencedor (1º tempo): Grêmio")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h_1t", "Grêmio"))

    def test_vencedor_da_partida_novibet(self):
        leg = self._leg("Vencedor da Partida: Alex Michelsen")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h", "Alex Michelsen"))

    def test_vencedor_do_jogo_novibet(self):
        leg = self._leg("Vencedor do Jogo: CHI Sky")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h", "CHI Sky"))

    def test_vencedor_sem_dois_pontos_continua_funcionando(self):
        """Regressão: "Vencedor <Nome>" sem ":" nenhum (grafia da Betano)
        não pode quebrar com a adição do branch baseado em `rpartition`."""
        leg = self._leg("Vencedor Matteo Berrettini")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h", "Matteo Berrettini"))

    # --- "Resultado ao Intervalo:" (Novibet) --------------------------------

    def test_resultado_ao_intervalo_vira_h2h_1t(self):
        leg = self._leg("Resultado ao Intervalo: Sheriff Tiraspol")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h_1t", "Sheriff Tiraspol"))

    # --- HT/FT: três grafias diferentes -------------------------------------

    def test_ht_ft_grafia_da_novibet(self):
        """"Intervalo/Final : X / Y" — sem "do jogo", espaço antes do ":"."""
        leg = self._leg("Intervalo/Final : Ajax / Ajax")
        self.assertEqual(leg.market_key, "ht_ft")

    def test_ht_ft_grafia_da_sportingtech(self):
        """"Intervalo/Final - X/X" — "-" em vez de ":"."""
        leg = self._leg("Intervalo/Final - Cruzeiro/Cruzeiro")
        self.assertEqual(leg.market_key, "ht_ft")

    def test_ht_ft_combinado_com_placar_continua_recusado(self):
        """"...resultado exato: 0:0 2:0" é HT+FT com placar exato dos dois —
        mercado que a Pinnacle não publica. Não pode virar `ht_ft` simples."""
        leg = self._leg("Intervalo/final do jogo resultado exato: 0:0 2:0")
        self.assertFalse(leg.suportado)

    # --- Chance dupla: Χ grego e notação mista -------------------------------

    def test_chance_dupla_chi_grego(self):
        leg = self._leg("Chance Dupla: Χ2")
        self.assertEqual(leg.market_key, "double_chance")

    def test_chance_dupla_1x_latino(self):
        leg = self._leg("Chance Dupla: 1X")
        self.assertEqual(leg.market_key, "double_chance")

    # --- total de gols por equipe: "Gol(s) <Time> (<período>):" -----------

    def test_gols_time_primeiro_tempo_casadeaposta(self):
        leg = self._leg("Gols Vitória (1º tempo): Mais de 0.5")
        self.assertTrue(leg.suportado)
        self.assertTrue(leg.market_key.startswith("team_total_1t"))
        self.assertEqual(leg.time_nome, "Vitória")

    def test_gol_time_segundo_tempo_continua_recusado(self):
        """"Gol Inter (2º tempo): Mais de 0.5" — 2º tempo já é recusado bem
        antes, em `parse_leg` (`SEGUNDO_TEMPO`)."""
        leg = self._leg("Gol Inter (2º tempo): Mais de 0.5")
        self.assertFalse(leg.suportado)

    # --- tênis: "Total de Games :" com dois pontos, "Resultado Correto" ----

    def test_total_de_games_com_dois_pontos_novibet(self):
        leg = self._leg("Total de Games : Mais de 20,5")
        self.assertEqual((leg.market_key, leg.selecao), ("games:20.5", "over"))

    def test_resultado_correto_sem_exato_nem_sets_novibet(self):
        leg = self._leg("Resultado Correto : 2 - 0")
        self.assertEqual((leg.market_key, leg.selecao), ("sets_spread:-1.5", "home"))

    # --- basquete: props sem "total de" e em inglês (Novibet) --------------

    def test_points_em_ingles_sem_total_de(self):
        leg = self._leg("Kamilla Cardoso - Points : Over 15.5")
        self.assertEqual(leg.market_key, "player:pontos:kamilla cardoso:15.5")
        self.assertEqual(leg.selecao, "over")

    def test_rebotes_bare_sem_total_de(self):
        leg = self._leg("Nneka Ogwumike - Rebotes : Mais de 8,5")
        self.assertEqual(leg.market_key, "player:rebotes:nneka ogwumike:8.5")

    def test_assistencias_bare_sem_total_de(self):
        leg = self._leg("Sonia Citron - Assistências : Mais de 3,5")
        self.assertEqual(leg.market_key, "player:assistencias:sonia citron:3.5")

    def test_triplos_grafia_da_novibet(self):
        """"Total de cestas de três pontos marcadas" não pode casar com a
        estatística "pontos" só porque a frase contém a palavra "pontos"."""
        leg = self._leg("Sabrina Ionescu - Total de cestas de três pontos marcadas : Mais de 3,5")
        self.assertEqual(leg.market_key, "player:triplos:sabrina ionescu:3.5")

    def test_linha_em_ingles_sem_de(self):
        """"Over 15.5" (inglês) não tem o "de" que "mais de"/"menos de"
        (português) sempre têm — `_linha` tinha o "de" como obrigatório pros
        dois casos."""
        from betano_superodds.value.market_parser import _linha
        self.assertEqual(_linha("Over 15.5"), ("over", 15.5))
        self.assertEqual(_linha("Under 8.5"), ("under", 8.5))
        self.assertEqual(_linha("Mais de 2.5"), ("over", 2.5))

    # --- SportingTech: "Para Ganhar" / "Para Ter o Maior Número de
    # Escanteios" ------------------------------------------------------------

    def test_para_ganhar_vira_h2h(self):
        leg = self._leg("Grêmio Para Ganhar")
        self.assertEqual((leg.market_key, leg.time_nome), ("h2h", "Grêmio"))

    def test_para_ganhar_um_dos_tempos_continua_recusado(self):
        """Vencer UM dos dois tempos é probabilidade conjunta que a Pinnacle
        não publica — mesma família de "marcar em ambos os tempos". O
        ancoramento de "Para Ganhar" em `$` não pode deixar isto passar."""
        leg = self._leg("Vitória Para Ganhar Um Dos Tempos")
        self.assertFalse(leg.suportado)

    def test_para_ter_o_maior_numero_de_escanteios_vira_corners_h2h(self):
        leg = self._leg("Vitória Para Ter o Maior Número de Escanteios")
        self.assertEqual((leg.market_key, leg.time_nome),
                         ("corners_h2h", "Vitória"))

    def test_para_ambos_os_times_marcarem_ja_funcionava(self):
        """Grafia da SportingTech pro BTTS — confirmação de que a regex
        ampla já existente (`amb[ao]s...marca(?:m|rem)`) cobre isto sem
        precisar de mudança."""
        leg = self._leg("Para Ambos os Times Marcarem")
        self.assertEqual((leg.market_key, leg.selecao), ("btts", "Yes"))


class TestTenis(unittest.TestCase):
    """Tênis: 46% das pernas capturadas, e antes eram descartadas em bloco."""

    def _partida(self) -> Matchup:
        m = Matchup(id=50, league="Montreal Masters", home_team="Matteo Berrettini",
                    away_team="Mariano Navone", commence_time=HOJE, sport="tennis")
        m.markets["games:22.5"] = Market(key="games:22.5", label="Total de Games 22.5",
                                         outcomes={"over": 1.95, "under": 1.87})
        m.markets["games_s1:9.5"] = Market(key="games_s1:9.5", label="Games Set 1",
                                           outcomes={"over": 2.05, "under": 1.78})
        m.markets["games_spread:-2.5"] = Market(key="games_spread:-2.5", label="Handicap",
                                                outcomes={"home": 1.90, "away": 1.92})
        m.markets["h2h"] = Market(key="h2h", label="Vencedor",
                                  outcomes={"home": 1.56, "away": 2.58})
        m.markets["sets_h2h_s1"] = Market(key="sets_h2h_s1", label="Vencedor Set 1",
                                          outcomes={"home": 1.62, "away": 2.40})
        m.markets["sets_total:2.5"] = Market(key="sets_total:2.5", label="Total de Sets",
                                             outcomes={"over": 2.46, "under": 1.55})
        m.markets["sets_spread:-1.5"] = Market(key="sets_spread:-1.5", label="Handicap Sets",
                                               outcomes={"home": 2.40, "away": 1.62})
        return m

    def test_games_da_partida_e_do_set(self):
        self.assertEqual(parse_leg("Games Mais de 22.5").market_key, "games:22.5")
        self.assertEqual(parse_leg("Total de Games Mais de 22.5").market_key, "games:22.5")
        leg = parse_leg("Total de Games no Set (Set 1) Mais de 9.5")
        self.assertEqual((leg.market_key, leg.selecao), ("games_s1:9.5", "over"))

    def test_games_escritos_do_jeito_da_altenar(self):
        """"jogos" no lugar de "games" e o set por extenso. Mesmo mercado, e a
        Pinnacle publica os dois — a perna morria em "não reconhecido"."""
        for texto, chave, lado in (
            ("Total jogos: Menos de 25.5", "games:25.5", "under"),
            ("Total jogos: Mais de 25.5", "games:25.5", "over"),
            ("Primeiro set - total jogos: Mais de 10.5", "games_s1:10.5", "over"),
            ("Segundo set - total jogos: Menos de 9.5", "games_s2:9.5", "under"),
            ("Terceiro set - total jogos: Mais de 8.5", "games_s3:8.5", "over"),
        ):
            leg = parse_leg(texto)
            self.assertEqual((leg.market_key, leg.selecao), (chave, lado), texto)

    def test_games_do_jeito_da_altenar_casa_com_o_mercado(self):
        leg = parse_leg("Primeiro set - total jogos: Mais de 9.5")
        self.assertIsNotNone(prob_da_perna(leg, self._partida()))

    def test_total_de_games_do_jogador_grafia_da_vupi(self):
        """"<Jogador> total jogos: Mais de N" (vupi) é total de games DO
        JOGADOR, não da partida — a Pinnacle não publica isso
        (`_mapear_tenis` só tem total da partida). Antes desta perna caía em
        "mercado não reconhecido" porque o regex de baixo é ancorado em
        `^\\s*total`, sem prever nome na frente; agora cai em "sem
        cobertura" com o motivo certo, e sobretudo NÃO é lida como se fosse
        o total da PARTIDA (o que produziria edge sobre o mercado errado)."""
        for texto in ("Matteo Arnaldi total jogos: Mais de 12.5",
                      "Hubert Hurkacz total jogos: Menos de 12.5"):
            leg = parse_leg(texto)
            self.assertFalse(leg.suportado, texto)
            self.assertEqual(leg.motivo, "total de games do jogador não coberto pela Pinnacle")
            self.assertIsNone(leg.market_key)

    def test_total_de_games_da_partida_sem_jogador_continua_indo_pro_mercado_certo(self):
        """Não-regressão: a variante SEM jogador na frente continua indo
        pro total da PARTIDA (`games:X`), como sempre foi."""
        for texto, sel in (("Total jogos: Mais de 24.5", "over"),
                           ("Total jogos: Menos de 24.5", "under"),
                           ("Total de jogos: Mais de 24.5", "over")):
            leg = parse_leg(texto)
            self.assertEqual((leg.market_key, leg.selecao), ("games:24.5", sel), texto)
            self.assertTrue(leg.suportado, texto)

    def test_handicap_com_dois_pontos_e_parenteses_grafia_da_vupi(self):
        """A vupi escreve o handicap de games/sets com ":" e a linha entre
        parênteses — a Betano escreve sem os dois. Achado real no banco."""
        for texto, chave, jogador, linha in (
            ("Handicap de sets: Brandon Nakashime (-1.5)",
             "sets_spread", "Brandon Nakashime", -1.5),
            ("Primeiro set - handicap de jogos: Flavio Cobolli (-1.5)",
             "games_spread_s1", "Flavio Cobolli", -1.5),
            ("Segundo set - handicap de jogos: Joao Fonseca (+2.5)",
             "games_spread_s2", "Joao Fonseca", 2.5),
        ):
            leg = parse_leg(texto)
            self.assertEqual(
                (leg.market_key, leg.time_nome, leg.handicap_linha),
                (chave, jogador, linha), texto)

    def test_handicap_de_sets_da_vupi_casa_com_o_mercado(self):
        leg = parse_leg("Handicap de sets: Matteo Berrettini (-1.5)")
        self.assertIsNotNone(prob_da_perna(leg, self._partida()))

    def test_vencedor_do_set_nao_vira_1x2_de_futebol(self):
        """"Vencedor do Set (Set 1) X" casa com o `vencedor\\b` do parser de futebol."""
        leg = parse_leg("Vencedor do Set (Set 1) Rafael Jodar")
        self.assertEqual(leg.market_key, "sets_h2h_s1")
        self.assertEqual(leg.time_nome, "Rafael Jodar")

    def test_vencedor_da_partida_usa_a_chave_h2h(self):
        """No tênis o vencedor vem de `units: Sets`, mas a chave tem que ser h2h."""
        leg = parse_leg("Vencedor Matteo Berrettini")
        p = prob_da_perna(leg, self._partida())
        self.assertIsNotNone(p)

    def test_handicap_de_games_troca_o_sinal_no_visitante(self):
        """A Pinnacle indexa o bloco pela linha DO MANDANTE."""
        casa = parse_leg("Handicap de games Matteo Berrettini -2.5")
        self.assertEqual(casa.handicap_linha, -2.5)
        self.assertIsNotNone(prob_da_perna(casa, self._partida()))

        # O visitante com +2.5 é o outro lado do MESMO bloco (mandante -2.5).
        fora = parse_leg("Handicap de games Mariano Navone +2.5")
        p = prob_da_perna(fora, self._partida())
        self.assertIsNotNone(p)

    def test_placar_de_sets_2_0_equivale_a_handicap_em_bo3(self):
        leg = parse_leg("Resultado exato (sets) 2 - 0")
        self.assertEqual(leg.market_key, "sets_spread:-1.5")
        self.assertEqual(leg.exige_mercado, "sets_total:2.5")
        self.assertIsNotNone(prob_da_perna(leg, self._partida()))

    def test_placar_de_sets_recusa_sem_a_assinatura_de_bo3(self):
        """Em bo5 "2-0" não é resultado final — a equivalência não vale."""
        bo5 = self._partida()
        del bo5.markets["sets_total:2.5"]
        self.assertIsNone(prob_da_perna(parse_leg("Resultado exato (sets) 2 - 0"), bo5))

    def test_mercados_de_tenis_realmente_ausentes(self):
        for texto, motivo in [
            ("Tie Breaks Mais de 0.5", "tie-break"),
            ("Resultado Após 2 Games (Set 1) 1-1", "resultado após N games"),
            ("Resultado no Set (Set 1) 6 - 3", "placar exato de set"),
            ("Total de Aces 17+", "aces"),
            ("Rafael Jodar Duplas faltas 3+", "duplas faltas"),
        ]:
            leg = parse_leg(texto)
            self.assertFalse(leg.suportado, texto)
            self.assertEqual(leg.motivo, motivo)


class TestUmXDoisDeSubMercado(unittest.TestCase):
    """Regressão do vazamento achado em 2026-08-04.

    "Escanteios 1x2: Fortaleza" (super odd real da Esportiva Bet, 2.17→3.00)
    casava com o `\\b1x2\\b` de `_parse_resultado` e virava `h2h` — a odd de
    quem vence os ESCANTEIOS comparada contra a odd justa de quem vence o JOGO.
    Mercado restrito contra referência ampla, o mesmo erro do combinado do
    Vélez, e sem nada no rótulo denunciando.
    """

    AMPLOS = ("h2h", "h2h_1t", "double_chance", "double_chance_1t")

    def test_nenhum_1x2_de_sub_mercado_vira_o_1x2_do_jogo(self):
        for texto in ("Escanteios 1x2: Fortaleza",
                      "Escanteios 1x2: Empate",
                      "Escanteios 1x2: 1",
                      "1º tempo - escanteios 1x2: Fortaleza",
                      "Escanteios - Resultado Final: Fortaleza",
                      "Vencedor de escanteios: Fortaleza",
                      "Remates 1x2: Palmeiras",
                      "Faltas 1x2: Palmeiras",
                      "Escanteios Chance dupla: Fortaleza ou empate"):
            with self.subTest(texto=texto):
                leg = parse_leg(texto)
                base = (leg.market_key or "").split(":")[0]
                self.assertNotIn(base, self.AMPLOS,
                                 f"{texto!r} virou referência do jogo inteiro")

    def test_escanteios_vao_para_a_chave_propria(self):
        self.assertEqual(parse_leg("Escanteios 1x2: Fortaleza").market_key, "corners_h2h")
        self.assertEqual(parse_leg("1º tempo - escanteios 1x2: Empate").market_key,
                         "corners_h2h_1t")
        self.assertEqual(parse_leg("Escanteios 1x2: Empate").selecao, "draw")
        self.assertEqual(parse_leg("Escanteios 1x2: Fortaleza").time_nome, "Fortaleza")

    def test_sub_mercado_sem_referencia_e_recusado(self):
        for texto, motivo in (("Remates 1x2: Palmeiras", "1x2 de remates (sem referência)"),
                              ("Escanteios Chance dupla: X ou empate",
                               "chance dupla de escanteios")):
            leg = parse_leg(texto)
            self.assertFalse(leg.suportado, texto)
            self.assertEqual(leg.motivo, motivo)

    def test_o_1x2_de_verdade_continua_funcionando(self):
        """A guarda não pode ter comido o mercado normal."""
        for texto, chave in (("Resultado Final: Flamengo", "h2h"),
                             ("1x2: Levski Sófia", "h2h"),
                             ("Vencedor do encontro: 1", "h2h"),
                             ("1º tempo - 1x2: Empate", "h2h_1t"),
                             ("Chance dupla: Celtic ou empate", "double_chance"),
                             ("Total de Escanteios: Mais de 9.5", "corners:9.5")):
            self.assertEqual(parse_leg(texto).market_key, chave, texto)


class TestEscanteios1x2DoHandicap(unittest.TestCase):
    """A Pinnacle não publica moneyline de escanteios em jogo nenhum (conferido
    em 60), mas publica o handicap — e dele o 1X2 sai por aritmética exata."""

    def _jogo(self, ladder: dict[float, tuple[float, float]]) -> Matchup:
        """`ladder` mapeia a linha do MANDANTE -> (odd casa, odd fora), que é
        como a Pinnacle indexa o bloco de handicap."""
        m = Matchup(id=71, league="Brazil - Serie A", home_team="Fortaleza",
                    away_team="Palmeiras", commence_time=HOJE)
        for linha, (casa, fora) in ladder.items():
            chave = f"corners_spread:{linha}"
            m.markets[chave] = Market(key=chave, label=chave,
                                      outcomes={"home": casa, "away": fora})
        return m

    def _jogo_padrao(self) -> Matchup:
        # -0.5: mandante vence os escanteios. +0.5: vence ou empata.
        return self._jogo({-0.5: (2.00, 2.00), 0.5: (1.40, 3.20)})

    def test_as_tres_probabilidades_somam_um(self):
        jogo = self._jogo_padrao()
        soma = 0.0
        for texto in ("Escanteios 1x2: Fortaleza", "Escanteios 1x2: Empate",
                      "Escanteios 1x2: Palmeiras"):
            p = prob_da_perna(parse_leg(texto), jogo)
            self.assertIsNotNone(p, texto)
            soma += p.probabilidade
        self.assertAlmostEqual(soma, 1.0, places=6)

    def test_empate_e_a_faixa_entre_as_duas_linhas(self):
        jogo = self._jogo_padrao()
        a = remover_vig(jogo.markets["corners_spread:-0.5"])["home"].probabilidade
        b = remover_vig(jogo.markets["corners_spread:0.5"])["home"].probabilidade
        empate = prob_da_perna(parse_leg("Escanteios 1x2: Empate"), jogo)
        self.assertAlmostEqual(empate.probabilidade, b - a, places=6)

    def test_sai_marcada_como_estimada(self):
        """Herda o desconto de stake e o selo no alerta — não é preço direto."""
        p = prob_da_perna(parse_leg("Escanteios 1x2: Fortaleza"), self._jogo_padrao())
        self.assertTrue(p.interpolada)
        self.assertIn("escanteios", p.mercado)

    def test_sem_o_ladder_no_zero_recusa(self):
        """Jogo desequilibrado: a Pinnacle publica de -3.0 pra baixo."""
        jogo = self._jogo({-3.0: (1.90, 1.95), -4.0: (2.30, 1.62)})
        self.assertIsNone(prob_da_perna(parse_leg("Escanteios 1x2: Fortaleza"), jogo))

    def test_uma_linha_so_nao_basta(self):
        jogo = self._jogo({-0.5: (2.00, 2.00)})
        self.assertIsNone(prob_da_perna(parse_leg("Escanteios 1x2: Empate"), jogo))

    def test_ladder_incoerente_recusa(self):
        """"vence ou empata" tem que ser mais provável que "vence" — se não for,
        alguma das duas linhas não é o que pensamos."""
        jogo = self._jogo({-0.5: (1.30, 3.60), 0.5: (2.60, 1.50)})
        self.assertIsNone(prob_da_perna(parse_leg("Escanteios 1x2: Fortaleza"), jogo))

    def test_time_que_nao_e_do_jogo_recusa(self):
        self.assertIsNone(
            prob_da_perna(parse_leg("Escanteios 1x2: Grêmio"), self._jogo_padrao()))


class TestHandicapDeTime(unittest.TestCase):
    """Handicap de futebol/basquete por nome de time — "Handicap: Cruzeiro
    (-1.5)". Textos reais achados no banco (Esportiva Bet, EstrelaBet,
    BateuBet, GingaBet); antes caía em "handicap de futebol (texto não
    calibrado)" mesmo a Pinnacle publicando (é o mercado mais líquido dela)."""

    def _futebol(self) -> Matchup:
        m = Matchup(id=80, league="Brazil - Serie A", home_team="Cruzeiro",
                    away_team="Vasco da Gama", commence_time=HOJE)
        m.markets["spread:-1.5"] = Market(key="spread:-1.5", label="Handicap -1.5",
                                          outcomes={"home": 1.95, "away": 1.87})
        m.markets["spread_1t:-0.5"] = Market(key="spread_1t:-0.5", label="Handicap 1T -0.5",
                                             outcomes={"home": 2.05, "away": 1.78}, period=1)
        return m

    def _basquete(self) -> Matchup:
        m = Matchup(id=81, league="WNBA", home_team="Toronto Tempo",
                    away_team="Golden State Valkyries", commence_time=HOJE,
                    sport="basketball")
        m.markets["spread:3.5"] = Market(key="spread:3.5", label="Handicap 3.5",
                                         outcomes={"home": 1.91, "away": 1.91})
        return m

    def test_formato_simples_da_esportiva_bet(self):
        leg = parse_leg("Handicap: Cruzeiro (-1.5)")
        self.assertEqual(leg.market_key, "spread")
        self.assertEqual(leg.time_nome, "Cruzeiro")
        self.assertEqual(leg.handicap_linha, -1.5)
        self.assertIsNotNone(prob_da_perna(leg, self._futebol()))

    def test_formato_asiatico_da_estrelabet(self):
        leg = parse_leg("Handicap Asiático: Mirassol (+0.75)")
        self.assertEqual((leg.market_key, leg.time_nome, leg.handicap_linha),
                         ("spread", "Mirassol", 0.75))

    def test_visitante_troca_o_sinal(self):
        """A Pinnacle indexa o bloco pela linha DO MANDANTE (mesmo mecanismo
        já validado no handicap de games do tênis)."""
        casa = parse_leg("Handicap: Cruzeiro (-1.5)")
        self.assertIsNotNone(prob_da_perna(casa, self._futebol()))

        fora = parse_leg("Handicap: Vasco da Gama (+1.5)")
        self.assertIsNotNone(prob_da_perna(fora, self._futebol()))

    def test_primeiro_tempo_usa_o_mercado_1t(self):
        leg = parse_leg("1º tempo - handicap: Cruzeiro (-0.5)")
        self.assertEqual(leg.market_key, "spread_1t")
        self.assertIsNotNone(prob_da_perna(leg, self._futebol()))

    def test_mesmo_formato_serve_pro_basquete(self):
        """Quem decide o esporte é o evento casado, não o parser — o
        `market_key` "spread" é igual pros dois em `pinnacle.py`."""
        leg = parse_leg("Handicap (incluindo Prorrogação): Toronto Tempo (F) (+3.5)")
        self.assertEqual(leg.market_key, "spread")
        self.assertEqual(leg.time_nome, "Toronto Tempo (F)")
        self.assertIsNotNone(prob_da_perna(leg, self._basquete()))

    def test_handicap_de_games_do_tenis_nao_e_capturado_aqui(self):
        """Guarda de regressão: "handicap de games/sets" (tênis) é resolvido
        em `_parse_tenis`, antes deste parser rodar — não pode cair aqui."""
        leg = parse_leg("Handicap de games Matteo Berrettini -2.5")
        self.assertEqual(leg.market_key, "games_spread")

    def test_handicap_de_escanteios_nao_casa(self):
        """Só não apareceu no banco ainda — mas se aparecer, não pode virar
        `spread` (handicap do JOGO), o mesmo erro de classe do Mirassol
        (mercado restrito casado com referência ampla)."""
        leg = parse_leg("Handicap de Escanteios: Cruzeiro (-1.5)")
        self.assertNotEqual(leg.market_key, "spread")

    def test_texto_nao_calibrado_ainda_cai_no_motivo_explicito(self):
        leg = parse_leg("Handicap qualquer coisa sem o formato esperado")
        self.assertFalse(leg.suportado)
        self.assertEqual(leg.motivo, "handicap de futebol (texto não calibrado)")


class TestBasquete(unittest.TestCase):
    def _jogo(self) -> Matchup:
        m = Matchup(id=60, league="WNBA", home_team="New York Liberty",
                    away_team="Seattle Storm", commence_time=HOJE, sport="basketball")
        m.markets["totals:180.5"] = Market(key="totals:180.5", label="Total de Pontos",
                                           outcomes={"over": 1.90, "under": 1.92})
        m.markets["totals_1t:93.5"] = Market(key="totals_1t:93.5", label="Total 1T",
                                             outcomes={"over": 1.88, "under": 1.94}, period=1)
        m.markets["team_total:home:94.5"] = Market(key="team_total:home:94.5", label="Total NY",
                                                   outcomes={"over": 1.88, "under": 1.93})
        m.markets["player:pontos:sabrina ionescu:19.5"] = Market(
            key="player:pontos:sabrina ionescu:19.5", label="Ionescu pontos",
            outcomes={"over": 1.83, "under": 1.92})
        return m

    def test_prop_de_jogador_converte_limite_inteiro_em_linha(self):
        """A Betano publica "20+"; a Pinnacle, over 19.5."""
        leg = parse_leg("Sabrina Ionescu Total de pontos 20+")
        self.assertEqual(leg.market_key, "player:pontos:sabrina ionescu:19.5")
        self.assertEqual(leg.selecao, "over")
        self.assertIsNotNone(prob_da_perna(leg, self._jogo()))

    def test_estatisticas_de_jogador(self):
        for texto, chave in [
            ("A'ja Wilson Total de Rebotes 11+", "player:rebotes:a ja wilson:10.5"),
            ("Jordin Canada Total de Assistências 9+", "player:assistencias:jordin canada:8.5"),
            ("Rhyne Howard Arremessos de três pontos convertidos 4+",
             "player:triplos:rhyne howard:3.5"),
            ("Allisha Gray Total de Pontos, Rebotes e Assistências 27+",
             "player:pra:allisha gray:26.5"),
        ]:
            self.assertEqual(parse_leg(texto).market_key, chave, texto)

    def test_total_do_jogo_e_por_equipe(self):
        self.assertEqual(parse_leg("Total de pontos Mais de 180.5").market_key, "totals:180.5")
        self.assertEqual(parse_leg("1º Tempo - Total de pontos Mais de 93.5").market_key,
                         "totals_1t:93.5")
        leg = parse_leg("Total de pontos New York Liberty Mais de 94.5")
        self.assertEqual(leg.market_key, "team_total:{lado}:94.5")
        self.assertEqual(leg.time_nome, "New York Liberty")
        self.assertIsNotNone(prob_da_perna(leg, self._jogo()))

    def test_cestinha_do_jogo_nao_existe_na_pinnacle(self):
        leg = parse_leg("Maior número de pontos no jogo Kahleah Copper")
        self.assertFalse(leg.suportado)
        self.assertEqual(leg.motivo, "cestinha do jogo")


class TestPropSemEstatistica(unittest.TestCase):
    """"Nyara Sabally (TOR) mais 4.5" — bet-builder da Altenar.

    O `marketId` da perna não vem no payload, então sobra só o nome da seleção
    e o rótulo não diz se 4.5 é rebote, assistência ou ponto. A Pinnacle publica
    os quatro do mesmo jogador. Quem decide é a linha, e só quando ela é única.
    """

    def _jogo(self, **extras: float) -> Matchup:
        m = Matchup(id=61, league="WNBA", home_team="Golden State Valkyries",
                    away_team="Toronto Tempo", commence_time=HOJE, sport="basketball")
        for chave, linha in extras.items():
            k = f"player:{chave}:nyara sabally:{linha}"
            m.markets[k] = Market(key=k, label=chave,
                                  outcomes={"over": 1.85, "under": 1.90})
        return m

    def test_chave_sai_com_a_estatistica_em_aberto(self):
        leg = parse_leg("Nyara Sabally (TOR) mais 4.5")
        self.assertTrue(leg.suportado)
        self.assertEqual(leg.market_key, "player:{stat}:nyara sabally:4.5")
        self.assertEqual(leg.selecao, "over")

    def test_resolve_quando_so_uma_estatistica_bate_a_linha(self):
        leg = parse_leg("Nyara Sabally (TOR) mais 4.5")
        jogo = self._jogo(rebotes=4.5, pontos=12.5, triplos=1.5)
        self.assertIsNotNone(prob_da_perna(leg, jogo))

    def test_RECUSA_quando_duas_estatisticas_batem_a_linha(self):
        """4.5 serve pra rebote e pra assistência. Escolher compararia a odd de
        um mercado contra a justa de outro — é o edge inventado que a guarda
        de combinados já existe pra impedir."""
        leg = parse_leg("Nyara Sabally (TOR) mais 4.5")
        jogo = self._jogo(rebotes=4.5, assistencias=4.5)
        self.assertIsNone(prob_da_perna(leg, jogo))

    def test_recusa_quando_o_jogador_nao_tem_prop_publicado(self):
        leg = parse_leg("Nyara Sabally (TOR) mais 4.5")
        self.assertIsNone(prob_da_perna(leg, self._jogo(pontos=12.5)))

    def test_menos_vira_under(self):
        self.assertEqual(parse_leg("Allisha Gray (ATL) menos 18.5").selecao, "under")

    def test_nao_engole_rotulo_que_nao_e_prop(self):
        for texto in ("Resultado Final: Flamengo",
                      "Chance dupla: Empate ou Santos",
                      "Total de Gols: Mais de 2.5"):
            leg = parse_leg(texto)
            self.assertNotIn("{stat}", leg.market_key or "", texto)

    def test_a_chave_bate_com_a_que_a_PINNACLE_produz(self):
        """As duas pontas montam a chave separadamente; se divergirem, a
        resolução nunca acha nada e a perda é silenciosa.

        O bloco abaixo está no formato real da API (props vêm por
        `participantId` chamado Over/Under, com o jogador na `description`).
        """
        from betano_superodds.value.pinnacle import PinnacleScraper

        jogo = Matchup(id=62, league="WNBA", home_team="Atlanta Dream",
                       away_team="Phoenix Mercury", commence_time=HOJE,
                       sport="basketball")
        bloco = {"type": "total", "period": 0, "matchupId": 900, "prices": [
            {"participantId": 1, "price": -115, "points": 18.5},
            {"participantId": 2, "price": -105, "points": 18.5},
        ]}
        ctx = {"units": "Points", "descricao": "Allisha Gray Total Points",
               "participantes": {1: "Over", 2: "Under"}}
        market = PinnacleScraper()._mapear_basquete(bloco, ctx)
        self.assertIsNotNone(market)
        jogo.markets[market.key] = market

        leg = parse_leg("Allisha Gray (ATL) menos 18.5")
        p = prob_da_perna(leg, jogo)
        self.assertIsNotNone(p, f"parser gerou {leg.market_key}, pinnacle {market.key}")


class TestInterpolacaoDeLinha(unittest.TestCase):
    """A Betano oferece linhas que a Pinnacle nem sempre publica."""

    def _partida(self, linhas: dict[float, tuple[float, float]]) -> Matchup:
        m = Matchup(id=80, league="Montreal Masters", home_team="A", away_team="B",
                    commence_time=HOJE, sport="tennis")
        for linha, (over, under) in linhas.items():
            m.markets[f"games_s1:{linha}"] = Market(
                key=f"games_s1:{linha}", label=f"Games Set 1 {linha}",
                outcomes={"over": over, "under": under})
        return m

    def test_interpola_entre_as_duas_vizinhas(self):
        # 8.5 e 10.5 publicadas; a Betano quer 9.5.
        mu = self._partida({8.5: (1.40, 2.90), 10.5: (2.90, 1.40)})
        p = prob_da_perna(parse_leg("Total de Games no Set (Set 1) Mais de 9.5"), mu)
        self.assertIsNotNone(p)
        self.assertTrue(p.interpolada)
        # No ponto médio de duas linhas simétricas, P(over) ≈ 0.5.
        self.assertAlmostEqual(p.probabilidade, 0.5, places=2)

    def test_nao_extrapola_fora_do_intervalo(self):
        """Só 8.5 e 9.5 publicadas: 12.5 está fora e não pode ser estimada."""
        mu = self._partida({8.5: (1.40, 2.90), 9.5: (1.90, 1.90)})
        self.assertIsNone(
            prob_da_perna(parse_leg("Total de Games no Set (Set 1) Mais de 12.5"), mu))

    def test_recusa_intervalo_largo_demais(self):
        mu = self._partida({4.5: (1.10, 6.00), 14.5: (6.00, 1.10)})
        self.assertIsNone(
            prob_da_perna(parse_leg("Total de Games no Set (Set 1) Mais de 9.5"), mu))

    def test_linha_publicada_tem_precedencia_sobre_estimativa(self):
        mu = self._partida({8.5: (1.40, 2.90), 9.5: (1.85, 1.95), 10.5: (2.90, 1.40)})
        p = prob_da_perna(parse_leg("Total de Games no Set (Set 1) Mais de 9.5"), mu)
        self.assertFalse(p.interpolada)


class TestTotaisPorEquipe(unittest.TestCase):
    """A Pinnacle publica `team_total` — antes era descartado por presunção."""

    def _jogo(self) -> Matchup:
        m = Matchup(id=70, league="L", home_team="Flamengo", away_team="Palmeiras",
                    commence_time=HOJE)
        m.markets["team_total:home:0.5"] = Market(key="team_total:home:0.5", label="Total casa",
                                                  outcomes={"over": 1.15, "under": 5.20})
        m.markets["corners_team_total:away:4.5"] = Market(
            key="corners_team_total:away:4.5", label="Escanteios fora",
            outcomes={"over": 1.90, "under": 1.92})
        return m

    def test_total_de_gols_por_equipe(self):
        leg = parse_leg("Flamengo - Total de Gols Mais de 0.5")
        self.assertTrue(leg.suportado)
        self.assertEqual(leg.market_key, "team_total:{lado}:0.5")
        self.assertIsNotNone(prob_da_perna(leg, self._jogo()))

    def test_escanteios_por_equipe(self):
        leg = parse_leg("Palmeiras Escanteios Mais de 4.5")
        self.assertTrue(leg.suportado)
        self.assertEqual(leg.market_key, "corners_team_total:{lado}:4.5")
        self.assertIsNotNone(prob_da_perna(leg, self._jogo()))

    def test_escanteios_do_jogo_continuam_na_chave_antiga(self):
        self.assertEqual(parse_leg("Escanteios Mais de 9.5").market_key, "corners:9.5")


class TestTabelaDeConfianca(unittest.TestCase):
    """A tabela de fonte/threshold/confiança por tipo de mercado (skill
    `value-bet-methodology`): edge% sozinho não basta, confiança depende de
    quantas casas sustentam o consenso e de que classe de mercado é.

    Os dois primeiros casos usam os NÚMEROS REAIS de
    `logs/bot-2026-08-05.log` (grep por "🔥 VALUE" e "consenso"): o log tinha
    edges de +82.7%, +86.9%, +106.7% e +24.5% saindo como "🔥 VALUE" sob o
    threshold único antigo — exatamente o "bet com edge de 80%+ que era só
    ruído de consenso fraco" que motivou esta tabela. O caso `alta` reusa o
    fixture real do bug de gols-exatos do Internacional já coberto em
    `TestGolsExatosDeTime` (mesmo log: "23.6%: Corinthians - Internacional").
    """

    def test_consenso_de_2_casas_com_edge_de_80_por_cento_e_insuficiente(self):
        """82.7% é o edge real do log ("FC Salzburgo - Pafos FC"). Só 2 casas
        no consenso — abaixo do mínimo — e o edge alto não compra confiança
        nenhuma: sem base suficiente não é value, é ruído de consenso fraco."""
        r = avaliar_value(3.654, 2.00, "simples", fonte_odd="consenso",
                          n_casas_consenso=2)
        self.assertAlmostEqual(r.edge_pct, 82.7, places=1)
        self.assertEqual(r.confianca, "insuficiente")
        self.assertFalse(r.is_value)

        s = stake.calcular(3.654, 2.00, confianca=r.confianca, flag=r.flag)
        self.assertEqual(s.unidades, 0.0)
        self.assertFalse(s.apostavel)

    def test_consenso_de_2_casas_com_edge_de_24_5_por_cento_tambem_e_insuficiente(self):
        """24.5% é o edge real do log ("Maccabi Tel Aviv FC - CSKA Sofia").
        Mesmo mecanismo com edge modesto: `insuficiente` não é sobre o
        tamanho do edge, é sobre não ter base pra confiar nele."""
        r = avaliar_value(2.49, 2.00, "simples", fonte_odd="consenso",
                          n_casas_consenso=2)
        self.assertAlmostEqual(r.edge_pct, 24.5, places=1)
        self.assertEqual(r.confianca, "insuficiente")
        self.assertFalse(r.is_value)

    def test_simples_com_pinnacle_e_confianca_alta(self):
        """Caso real Corinthians-Internacional (gols exatos do 1ºT, boost
        3.45 — o mesmo fixture do bug corrigido em `TestGolsExatosDeTime`):
        preço direto da Pinnacle, mercado simples — confiança máxima."""
        oferta = {"evento": "Corinthians - Internacional",
                  "mercado": "1º tempo - Internacional gols exatos: 1",
                  "odd_original": 3.20, "odd_boost": 3.45,
                  "valido_ate": HOJE.isoformat(), "url": "http://x"}
        r = avaliar_oferta(oferta, [matchup_gols_exatos_time()])
        self.assertEqual(r["status"], "avaliada")
        self.assertEqual(r["fonte_odd"], "pinnacle")
        self.assertEqual(r["confianca"], "alta")
        self.assertIsNone(r["flag"])

    def test_edge_acima_do_teto_de_sanidade_vira_flag_e_zera_stake(self):
        """Edge > 50% em QUALQUER mercado — mesmo simples, mesmo com
        Pinnacle — é bloqueado antes do Kelly: é sintoma de bug de casamento
        até prova em contrário (mesma classe do falso +141% do Mirassol e do
        falso +23,6% do gols-exatos do Internacional, os dois já corrigidos)."""
        r = avaliar_value(3.20, 2.00, "simples", fonte_odd="pinnacle")
        self.assertGreater(r.edge_pct, config.EDGE_TETO_SANIDADE)
        self.assertEqual(r.flag, "possivel_erro_matching")
        self.assertFalse(r.is_value)

        s = stake.calcular(3.20, 2.00, confianca=r.confianca, flag=r.flag)
        self.assertEqual(s.unidades, 0.0)
        self.assertEqual(s.kelly_cheio, 0.0)
        self.assertFalse(s.apostavel)

    def test_media_e_baixa_usam_um_oitavo_de_kelly_nao_um_quarto(self):
        """`alta`/`média-alta` mantêm o Kelly cheio da config (¼);
        `média`/`baixa` usam metade (⅛) — Kelly é linear no edge, e uma fonte
        fraca infla o edge igual a um matching errado."""
        for confianca in ("alta", "média-alta"):
            with self.subTest(confianca=confianca):
                s = stake.calcular(3.00, 2.00, confianca=confianca)
                self.assertAlmostEqual(s.fracao_kelly, config.KELLY_FRACAO)
        for confianca in ("média", "baixa"):
            with self.subTest(confianca=confianca):
                s = stake.calcular(3.00, 2.00, confianca=confianca)
                self.assertAlmostEqual(s.fracao_kelly, config.KELLY_FRACAO / 2.0)

    def test_insuficiente_e_flag_zeram_o_stake_direto_em_calcular(self):
        """`stake.calcular` respeita `confianca`/`flag` sozinho — não deve
        depender de quem chama já ter zerado nada antes."""
        self.assertEqual(stake.calcular(3.00, 2.00, confianca="insuficiente"),
                         stake.Stake(0.0, 0.0, 0.0, False, ()))
        self.assertEqual(
            stake.calcular(3.00, 2.00, confianca="alta",
                           flag="possivel_erro_matching"),
            stake.Stake(0.0, 0.0, 0.0, False, ()))

    def test_classe_mercado_reconhece_cartao_e_handicap_mas_nao_prop_de_basquete(self):
        """`gol de jogador` da tabela é o artilheiro do FUTEBOL — nunca tem
        preço da Pinnacle (`market_parser.SEM_COBERTURA`). Prop de jogador de
        BASQUETE (`player:pontos:...`) é outra coisa: a Pinnacle publica
        direto (`pinnacle.py`, campo `units`), então segue o caminho normal
        de mercado simples — não é a classe `prop` desta tabela."""
        cartao = parse_leg("Total de cartões 3.5: Mais de 3.5")
        self.assertFalse(cartao.suportado)
        self.assertEqual(classe_mercado_da_perna(cartao), "prop")

        handicap = parse_leg("Handicap: Cruzeiro (-1.5)")
        self.assertTrue(handicap.suportado)
        self.assertEqual(classe_mercado_da_perna(handicap), "prop")

        pontos_de_basquete = parse_leg("Kamilla Cardoso - Points : Over 15.5")
        self.assertTrue(pontos_de_basquete.suportado)
        self.assertEqual(classe_mercado_da_perna(pontos_de_basquete), "geral")

        simples = parse_leg("Resultado Final: Celtic")
        self.assertEqual(classe_mercado_da_perna(simples), "geral")

    def test_classificar_confianca_cobre_a_tabela_inteira(self):
        """As cinco linhas da tabela, direto na função — sem depender de
        `avaliar_value` pra não confundir "qual threshold" com "qual tier"."""
        self.assertEqual(classificar_confianca("simples", "pinnacle"), "alta")
        self.assertEqual(classificar_confianca("combo", "pinnacle"), "média-alta")
        self.assertEqual(
            classificar_confianca("simples", "consenso", n_casas_consenso=5), "média")
        self.assertEqual(
            classificar_confianca("combo", "consenso", n_casas_consenso=5), "baixa")
        self.assertEqual(
            classificar_confianca("simples", "consenso", classe_mercado="prop",
                                  n_casas_consenso=6), "baixa")
        self.assertEqual(
            classificar_confianca("simples", "consenso", n_casas_consenso=2),
            "insuficiente")


if __name__ == "__main__":
    unittest.main(verbosity=2)
