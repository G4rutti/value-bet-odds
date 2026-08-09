"""Configuração do avaliador de value bet."""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Pinnacle (fonte da odd justa)
# ---------------------------------------------------------------------------

PINNACLE_BASE = "https://guest.api.arcadia.pinnacle.com/0.1"

# Chave pública que vem no bundle JS do site da Pinnacle — não é credencial de
# usuário, é o que o próprio frontend usa pra ler odds sem login.
PINNACLE_API_KEY = os.getenv("PINNACLE_API_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CIrOi0R")

PINNACLE_HEADERS = {
    "x-api-key": PINNACLE_API_KEY,
    "x-device-uuid": os.getenv("PINNACLE_DEVICE_UUID", "betano-scanner-local"),
    "accept": "application/json",
    "referer": "https://www.pinnacle.com/",
}

# Mesmo motivo da Betano: stack TLS do Python leva bloqueio, curl_cffi não.
IMPERSONATE = "chrome"
REQUEST_TIMEOUT = 30.0

# Esportes carregados da Pinnacle. Só futebol era carregado, e isso sozinho
# descartava 51% das pernas capturadas (287 de tênis + 30 de basquete em 621).
# O `id` é o sportId da API; o nome vira `Matchup.sport` e decide o mapeamento.
SPORTS: dict[int, str] = {
    29: "soccer",
    33: "tennis",
    4: "basketball",
}

# Descrição do special na Pinnacle -> chave interna de mercado.
# Só o que sabemos casar com um texto de mercado da Betano entra aqui; o resto
# é ignorado de propósito, pra não inventar correspondência.
SPECIAL_KEYS: dict[str, str] = {
    "Both Teams To Score?": "btts",
    "Both Teams To Score? 1st Half": "btts_1t",
    "Double Chance": "double_chance",
    "Double Chance 1st Half": "double_chance_1t",
    "Draw No Bet": "dnb",
    "Draw No Bet 1st Half": "dnb_1t",
    "Correct Score": "correct_score",
    "Correct Score 1st Half": "correct_score_1t",
    "Half-Time/Full-Time": "ht_ft",
    "Total Goals Odd/Even": "odd_even",
    "Total Goals Odd/Even 1st Half": "odd_even_1t",
    "Exact Total Goals": "exact_goals",
    "Exact Total Goals 1st Half": "exact_goals_1t",
    # Publicados em ~8-12% dos jogos, medidos na API — antes eram descartados
    # por não estarem nesta tabela, não por ausência na Pinnacle.
    "Total Goals Range": "faixa_gols",
    "Total Goals Range 1st Half": "faixa_gols_1t",
    "Winning Margin": "margem_vitoria",
    "Winning Margin 1st Half": "margem_vitoria_1t",
    "Either Team To Score?": "algum_marca",
    "Either Team To Score? 1st Half": "algum_marca_1t",
    "First Team To Score": "primeiro_a_marcar",
    "First Team To Score 1st Half": "primeiro_a_marcar_1t",
    "Both Teams To Score/Total Goals": "btts_gols",
    "Both Teams To Score/Winner": "btts_vencedor",
    "Winner/Total Goals": "vencedor_gols",
    "Odd/Even / Total Goals": "par_impar_gols",
}

# ---------------------------------------------------------------------------
# Matching de evento
# ---------------------------------------------------------------------------

# Score mínimo (0-100) pra aceitar um casamento de nomes de time.
# Abaixo disso o matcher devolve None em vez de arriscar comparar jogo errado.
MATCH_MIN_SCORE = float(os.getenv("MATCH_MIN_SCORE", "80"))

# Tolerância de data entre a super odd e o jogo na Pinnacle, em horas.
# Cobre fuso horário e pequenas diferenças de horário agendado.
MATCH_MAX_HORAS = float(os.getenv("MATCH_MAX_HORAS", "18"))

# ---------------------------------------------------------------------------
# Interpolação de linha
# ---------------------------------------------------------------------------

# A Betano escolhe linhas que a Pinnacle nem sempre publica (ela oferece "Games
# no Set Mais de 9.5" e a Pinnacle publica 8.5 e 10.5). Com as duas vizinhas em
# mãos, dá pra estimar a do meio — é a maior causa isolada de perna sem odd.
#
# ⚠️ Isto é ESTIMATIVA, não preço observado. Só interpola entre duas linhas
# realmente publicadas (nunca extrapola pra fora do intervalo) e o resultado é
# marcado como `interpolada` na saída. Desligue com INTERPOLAR_LINHAS=0.
INTERPOLAR_LINHAS = os.getenv("INTERPOLAR_LINHAS", "1") not in ("0", "false", "False")

# Distância máxima entre as duas linhas usadas. Quanto mais largo o intervalo,
# pior a aproximação linear — 2.0 cobre o vizinho imediato dos dois lados.
INTERPOLACAO_GAP_MAX = float(os.getenv("INTERPOLACAO_GAP_MAX", "2.0"))

# ---------------------------------------------------------------------------
# Thresholds de value
# ---------------------------------------------------------------------------

# Mercado simples: odd justa vem direto do de-vig, erro baixo.
EDGE_MIN_SIMPLES = float(os.getenv("EDGE_MIN_SIMPLES", "5.0"))

# Combo: a odd justa assume independência entre as pernas, o que a deixa
# otimista. Threshold mais alto pra compensar. Ver a nota em fair_odds.py.
EDGE_MIN_COMBO = float(os.getenv("EDGE_MIN_COMBO", "18.0"))

# Teto da odd JUSTA em mercado simples. Acima disso a oferta é avaliada,
# gravada e logada — mas não vira alerta.
#
# O corte é na justa e não na ofertada de propósito: justa <= 5.0 significa
# desfecho com >=20% de chance real, e é isso que separa "azarão com preço bom"
# de "resultado que não faz sentido". Uma oferta de odd 7.00 com justa 4.80
# continua passando; justa 8.29 com ofertada 11.00 não.
#
# Além da preferência, há um motivo técnico: `remover_vig` (fair_odds.py) é
# de-vig proporcional, que subestima sistematicamente a justa de zebra — daí o
# edge médio de +111% na faixa de odd 8+ do histórico de alertas, contra +11%
# na faixa até 5. O teto neutraliza justamente a faixa onde esse viés vive.
#
# Não vale pra combo: lá a justa é o produto das pernas, naturalmente alta, e
# quem segura é o EDGE_MIN_COMBO. 0 desliga o teto.
ODD_JUSTA_MAX = float(os.getenv("ODD_JUSTA_MAX", "5.0"))

# ---------------------------------------------------------------------------
# Consenso entre casas (`consenso.py`)
# ---------------------------------------------------------------------------
#
# A Pinnacle não publica prop de jogador (chutes ao gol, cartões, artilheiro):
# conferido na API, ela só tem mercado de gol. Isso deixava sem referência ~25%
# dos mercados que o usuário aposta. O consenso de-viga o mesmo mercado em
# várias casas e toma a mediana.

# Mínimo de casas para formar consenso. Duas casas não são consenso, são um
# desempate. Abaixo disto a oferta fica sem referência, que é a resposta
# honesta.
#
# ⚠️ E este número engana. Medido no banco em 20.669 seleções cotadas por 2+
# casas Altenar: **90,3% têm preço IDÊNTICO** (Esportiva×EstrelaBet 89,9%,
# 4Play×EstrelaBet 96,6%). Elas são skins do mesmo feed — três casas Altenar
# são UMA fonte contada três vezes, não três opiniões.
#
# Por isso `ResultadoConsenso` carrega `n_precos` além de `n_casas`, e o alerta
# mostra os dois. O consenso só é cruzamento de verdade quando entra casa de
# fora da Altenar (Betano, CasaDeAposta) — e é justamente o caso interessante,
# já que a Betano é quem cota os props que o usuário aposta.
#
# Subido de 3 para 5 em 2026-08-06, decisão consciente do dono junto com a
# queda de EDGE_MIN_CONSENSO logo abaixo — as duas mudanças andam juntas: o
# threshold caiu porque a tabela de confiança por mercado (skill
# `value-bet-methodology`) agora segura o risco por outra via (tier de
# confiança + fração de Kelly), não só pelo threshold. Subir o mínimo de casas
# aqui é reforço, não substituto: `n_precos` continua NÃO sendo gate (decisão
# do dono, risco na mesa) — 5 casas Altenar continuam podendo ser 1 preço só,
# e é `n_precos_consenso` na saída que avisa disso, não este número.
CONSENSO_MIN_CASAS = int(os.getenv("CONSENSO_MIN_CASAS", "5"))

# Mínimo de casas para os mercados mais finos do consenso: cartão, chute ao
# gol, gol de jogador (artilheiro) e handicap. Estes nunca têm preço direto da
# Pinnacle no caso de cartão/chute/artilheiro (`market_parser.SEM_COBERTURA`
# confirma isso contra a API ao vivo) e handicap só cai aqui quando a linha
# específica não está publicada — nos dois casos a referência é só o
# consenso, sem a Pinnacle por trás corrigindo o preço, e o piso de casas
# sobe em relação ao consenso genérico (`CONSENSO_MIN_CASAS`).
CONSENSO_MIN_CASAS_PROP = int(os.getenv("CONSENSO_MIN_CASAS_PROP", "6"))

# Gate de INDEPENDÊNCIA DE FEED — desligado por padrão (default 1 = qualquer
# consenso formado hoje continua formando, sem mudança de comportamento).
#
# `CONSENSO_MIN_CASAS` conta casas; `n_precos` (ver `ResultadoConsenso`) já
# mede preços distintos e rebaixa confiança (`value_calc.classificar_confianca`)
# quando eles colapsam — mas isso é diagnóstico, não recusa o consenso. Estes
# dois números aqui são o gate mais forte: recusam o consenso INTEIRO quando
# não há preços/famílias de feed suficientes.
#
# `CONSENSO_MIN_PRECOS`: preços distintos (arredondados) entre as casas que
# entraram na mediana. `CONSENSO_MIN_FAMILIAS`: famílias de feed distintas
# (`consenso._familia_da_casa` — as 11 casas Altenar são UMA família, "altenar",
# porque compartilham o mesmo feed; Betano é outra; casa fora das duas é a
# própria).
#
# Por que o default é 1 pros dois (efetivamente DESLIGADO): hoje só existe
# 1 casa Betano por evento na minoria dos eventos — ligar isto agora mataria
# cobertura real sem necessidade, porque a maioria dos consensos ainda é só
# Altenar. O dono liga subindo o env (ex.: `CONSENSO_MIN_FAMILIAS=2`) quando
# decidir que quer o gate rígido, sabendo que isso reduz cobertura — a mesma
# decisão consciente que já rege `n_precos` como diagnóstico (ver o cabeçalho
# de `consenso.py`).
CONSENSO_MIN_PRECOS = int(os.getenv("CONSENSO_MIN_PRECOS", "1"))
CONSENSO_MIN_FAMILIAS = int(os.getenv("CONSENSO_MIN_FAMILIAS", "1"))

# Janela de frescor dos preços das outras casas, em horas. Existe por causa do
# rodízio: `CASAS_POR_CICLO=3` raspa poucas casas por vez, e exigir todas no
# mesmo ciclo faria o consenso nunca formar. Quanto maior, mais casas entram —
# e mais velho é o preço que serve de referência.
CONSENSO_JANELA_HORAS = float(os.getenv("CONSENSO_JANELA_HORAS", "3.0"))

# Threshold próprio, bem acima do de mercado simples (5.0). Consenso de casas
# moles pode estar errado JUNTO — todas copiam a mesma origem —, e nesse caso o
# edge mede desvio do rebanho, não vantagem real. O piso alto é o que impede
# essa dispersão de virar "value". Mesma lógica de EDGE_MIN_COMBO/MODELO.
#
# Calibração de 2026-08-05, contra a Pinnacle em mercados que AS DUAS cobrem
# (totais, 11 pares, 3 casas por consenso):
#
#     viés médio        +4.9%   (consenso pede odd MAIOR que a Pinnacle)
#     erro abs mediano   4.5%
#     p90                9.1%
#     pior              10.0%
#
# O viés é positivo e isso é a direção segura: justa alta demais ENCOLHE o edge
# calculado, então o erro empurra pra não apostar, não pra apostar errado.
# Amostra pequena e só de totais de basquete; reconferir quando houver mais
# casas com o mesmo mercado. O script da contraprova está no README.
#
# ⚠️ Baixado de 20.0 para 8.0 em 2026-08-06 — decisão do dono, risco na mesa,
# NÃO derivada desta calibração (que sozinha recomendaria ficar acima do p90
# de 9,1%). O motivo de aceitar 8% mesmo assim: a tabela de confiança por tipo
# de mercado (skill `value-bet-methodology`) deixou de tratar "simples via
# consenso" como um bloco só — ela agora exige CONSENSO_MIN_CASAS=5 (subido de
# 3), classifica o resultado como confiança `média` (nunca `alta`), usa
# Kelly ⅛ em vez de ¼, e mantém STAKE_DESCONTO_CONSENSO=0.45 por cima. O
# threshold caiu porque o resto da pilha de segurança ficou mais forte — não
# porque o consenso passou a ser mais confiável. Mercados mais finos (cartão,
# chute ao gol, artilheiro, handicap) NÃO usam este número: são `EDGE_MIN_PROP`.
EDGE_MIN_CONSENSO = float(os.getenv("EDGE_MIN_CONSENSO", "8.0"))

# Combo em que ao menos uma perna caiu pro consenso (sem Pinnacle). Mistura o
# otimismo do produto de pernas independentes (ver EDGE_MIN_COMBO) com o erro
# de casa mole (ver EDGE_MIN_CONSENSO) — os dois riscos empilhados pedem o
# piso mais alto da tabela de mercado simples/combo. Mesmo valor de
# `EDGE_MIN_PROP` por desenho (é o mesmo nível de confiança, `baixa`), mas
# como knob separado — os dois podem divergir se a calibração de um andar
# sozinha no futuro.
EDGE_MIN_COMBO_CONSENSO = float(os.getenv("EDGE_MIN_COMBO_CONSENSO", "30.0"))

# Cartão, chute ao gol, gol de jogador (artilheiro) e handicap sem Pinnacle:
# a classe mais fraca da tabela de confiança. Cartão/chute/artilheiro nunca
# têm preço da Pinnacle (checado ao vivo, ver `market_parser.SEM_COBERTURA`);
# handicap só cai aqui quando falta a linha específica. Sem a Pinnacle
# corrigindo o preço em nenhum dos casos, o piso fica no mesmo nível do combo
# sem Pinnacle — é a mesma confiança `baixa`.
EDGE_MIN_PROP = float(os.getenv("EDGE_MIN_PROP", "30.0"))

# Quanto tempo guardar mercado de casa antes de podar. Só a janela interessa;
# o resto é histórico que ninguém lê e a tabela cresce rápido.
CONSENSO_RETENCAO_HORAS = float(os.getenv("CONSENSO_RETENCAO_HORAS", "12.0"))

# Guarda de sanidade entre as duas constantes acima: janela maior que a
# retenção é no-op silencioso, porque os dados já foram apagados do banco
# antes da janela terminar de "abrir" — `mercados_para_consenso` nunca
# encontraria a diferença. `consenso.ResultadoConsenso.idade_max_min`
# (Etapa 4) existe pra medir o período real do rodízio via `scrape_runs`
# antes de decidir alargar `CONSENSO_JANELA_HORAS` — decisão do dono, não
# tomada aqui.
assert CONSENSO_JANELA_HORAS <= CONSENSO_RETENCAO_HORAS, (
    "janela de consenso maior que a retenção é no-op silencioso: "
    "os dados já foram apagados antes da janela terminar"
)

# ---------------------------------------------------------------------------
# Ponte fuzzy evento_id -> pool (Etapa 3): casa não-Altenar (Betano, Novibet,
# EsportesDaSorte, CasaDeAposta) nunca tem seu `evento_id` no pool, que só as
# Altenar escrevem. `resolver_evento_id_via_pool` (`pool_eventos.py`) reusa
# `matcher.encontrar_evento` pra achar, por nome+data, qual `evento_id` do
# pool corresponde à oferta.
#
# Constantes PRÓPRIAS, deliberadamente mais apertadas que as do caminho
# Pinnacle (`MATCH_MIN_SCORE`/`MATCH_MAX_HORAS` acima) — errar aqui não é
# "sem referência", é "referência do jogo ERRADO", silenciosamente, porque
# vira consenso normal depois.
CONSENSO_MATCH_MIN_SCORE = float(os.getenv("CONSENSO_MATCH_MIN_SCORE", "85"))

# A janela de consenso é 3h (`CONSENSO_JANELA_HORAS`) e a retenção 12h; 18h
# (o padrão do caminho Pinnacle) deixaria a ponte casar a RODADA ERRADA do
# mesmo confronto (ida/volta, turno/returno).
CONSENSO_MATCH_MAX_HORAS = float(os.getenv("CONSENSO_MATCH_MAX_HORAS", "3.0"))

# ---------------------------------------------------------------------------
# Modelo de placar (`modelo_gols`)
# ---------------------------------------------------------------------------
#
# A Pinnacle publica placar exato / HT/FT / gols exatos em só ~1 de cada 12
# jogos. Nos outros o modelo deriva essas probabilidades do 1X2 + over/under.
# Tudo aqui existe pra manter esse número SEPARADO do preço observado.

# Enquanto falso, o modelo calcula, loga e grava — mas não alerta. Ligar só
# depois de rodar a calibração (`avaliar.py --calibrar-modelo`) e olhar o
# desvio contra os especiais reais. Odd derivada tratada como observada é
# como nascem os edges falsos que este projeto já pagou pra aprender.
MODELO_ALERTA_ATIVO = os.getenv("MODELO_ALERTA_ATIVO", "0") in ("1", "true", "True")

# Threshold próprio, e muito acima do de mercado simples (5.0). O número saiu
# da calibração de 2026-08-04 contra os especiais reais da Pinnacle (12 jogos,
# 198 desfechos comparados) — erro relativo da odd derivada:
#
#     correct_score   mediana 11.6%   p90 32.2%   máx 49.2%
#     exact_goals     mediana  5.3%   p90 12.1%   máx 26.7%
#     ht_ft           mediana  9.2%   p90 29.5%   máx 48.5%
#
# Threshold abaixo do próprio ruído do modelo transformaria erro de ajuste em
# "value". 35% fica acima de todos os p90 — e o fato de esse piso ser tão alto
# é o recado principal da calibração: o modelo serve pra cobertura e estudo,
# não (ainda) pra apostar. Reconferir com `python avaliar.py --calibrar-modelo`.
EDGE_MIN_MODELO = float(os.getenv("EDGE_MIN_MODELO", "35.0"))

# Correção de Dixon-Coles para os placares baixos. NÃO é ajustada por jogo: com
# só 3 alvos independentes (1X2 + over/under) ela fica mal identificada e
# roubaria graus de liberdade dos λ, que é o que de fato importa. O valor é o
# usual da literatura, negativo porque a Poisson pura subestima 0-0 e 1-1.
MODELO_RHO = float(os.getenv("MODELO_RHO", "-0.05"))

# Resíduo máximo do ajuste para CRIAR uma odd justa (placar exato, HT/FT,
# gols exatos). Acima disso o modelo não reproduziu nem o mercado que o
# alimentou — se erra o 1X2 que já conhece, não há motivo pra confiar no
# placar exato que ele inventa. Na calibração de 2026-08-04, 9 de 12 jogos
# passaram nesse corte.
MODELO_ERRO_MAX = float(os.getenv("MODELO_ERRO_MAX", "0.0004"))

# Resíduo máximo para CORRIGIR a conjunta de um combo — bem mais folgado, e o
# motivo é assimetria de risco. Criar preço do nada exige rigor. Já a correção
# de correlação só age numa direção: ela apenas REDUZ uma probabilidade que a
# multiplicação por independência já superestimou. Um modelo mediano corrigindo
# pra baixo continua sendo melhor que independência, que erra pra cima sempre.
# Exigir o corte estrito aqui deixava passar combos como "over 2.5 + ambas não
# marcam + escanteios" a +90% de edge só porque o ajuste ficou em 0.00074.
MODELO_ERRO_MAX_CORRELACAO = float(
    os.getenv("MODELO_ERRO_MAX_CORRELACAO", "0.002"))

# ---------------------------------------------------------------------------
# Stake (quanto apostar), em unidades
# ---------------------------------------------------------------------------
#
# 1 unidade = UNIDADE_PCT_BANCA % da banca. Quanto vale em dinheiro é decisão
# sua — o sistema só diz "0.25un", "2un". É de propósito: assim a recomendação
# não muda quando a banca cresce.

# Fração de Kelly. Kelly cheio (1.0) maximiza o crescimento no longo prazo MAS
# só se a probabilidade estimada estiver certa — e a nossa é estimada, com erro
# de matching, de de-vig e de linha. Um p superestimado com Kelly cheio quebra a
# banca. 0.25 é o padrão de mercado justamente por isso.
KELLY_FRACAO = float(os.getenv("KELLY_FRACAO", "0.25"))

# Quanto da banca vale 1 unidade.
UNIDADE_PCT_BANCA = float(os.getenv("UNIDADE_PCT_BANCA", "1.0"))

# Teto por aposta. Kelly em odd baixa com edge alto pede fração absurda da banca
# (1.21 contra 1.12 justa dá 38% no Kelly cheio); o teto é a rede de segurança
# contra uma odd justa errada.
STAKE_MAX_UNIDADES = float(os.getenv("STAKE_MAX_UNIDADES", "5.0"))

# Abaixo disto não vale a pena apostar — o alerta sai marcado como stake mínima.
STAKE_MIN_UNIDADES = float(os.getenv("STAKE_MIN_UNIDADES", "0.25"))

# Granularidade do arredondamento (sempre PRA BAIXO).
STAKE_PASSO = float(os.getenv("STAKE_PASSO", "0.25"))

# Desconto extra quando a odd justa é sabidamente otimista.
# Combo: as pernas são multiplicadas assumindo independência, o que infla o
# edge — e Kelly é linear no edge, então o erro passa direto pro stake.
STAKE_DESCONTO_COMBO = float(os.getenv("STAKE_DESCONTO_COMBO", "0.5"))
# Linha interpolada: a probabilidade foi estimada entre duas publicadas.
STAKE_DESCONTO_INTERPOLADA = float(os.getenv("STAKE_DESCONTO_INTERPOLADA", "0.75"))
# Modelo: a probabilidade não foi observada em lugar nenhum, foi derivada.
# O desconto mais pesado da lista, pelo mesmo motivo do threshold mais alto.
STAKE_DESCONTO_MODELO = float(os.getenv("STAKE_DESCONTO_MODELO", "0.4"))
# Consenso: preço observado, mas em casa mole e não na Pinnacle. Fica entre o
# combo (0.5) e o modelo (0.4) — é observação de verdade, só que de fonte que
# pode estar errada junto.
STAKE_DESCONTO_CONSENSO = float(os.getenv("STAKE_DESCONTO_CONSENSO", "0.45"))

# ---------------------------------------------------------------------------
# Marcação de número suspeito
# ---------------------------------------------------------------------------
#
# Acima deste edge o alerta sai com selo de suspeito em vez de ser suprimido.
# Edge dessa ordem quase sempre é erro de casamento de mercado, não achado —
# mas quem decide é quem lê, então o alerta vai marcado, não sumido.
EDGE_SUSPEITO_PCT = float(os.getenv("EDGE_SUSPEITO_PCT", "100.0"))

# Teto de sanidade da tabela de confiança (skill `value-bet-methodology`).
# Mais rígido que EDGE_SUSPEITO_PCT (100%) e com efeito diferente: não é selo
# no alerta, é bloqueio automático. Edge acima disto em QUALQUER mercado sai
# com `flag="possivel_erro_matching"`, `is_value=False` e stake zero — nunca
# passa pelo Kelly automático, seja qual for a confiança. O caso que motivou
# o número: o falso +141% do Mirassol e o falso +23,6% do gols-exatos do
# Internacional eram os dois sintoma de mercado casado errado, não value real
# — "edge alto é sintoma de bug antes de ser oportunidade" (ver a skill).
# 50% deixa passar a faixa alta mas plausível (combo bem construído, zebra
# genuína) e barra a faixa que historicamente foi sempre bug.
EDGE_TETO_SANIDADE = float(os.getenv("EDGE_TETO_SANIDADE", "50.0"))
