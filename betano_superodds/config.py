"""Configuração do scanner de Super Odds da Betano.

Tudo que é ajustável (endpoints, ritmo de polling, esportes vigiados) mora aqui.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

BASE_URL = "https://www.betano.bet.br"

# A API é pública (sem login, sem cookie de sessão), mas o WAF da Betano faz
# fingerprint do handshake TLS: a stack TLS do Python (urllib/httpx/requests)
# leva 403 mesmo com headers idênticos aos do navegador. Por isso o transporte é
# curl_cffi, que reproduz o handshake do Chrome. Não sobrescreva o User-Agent —
# o impersonate já manda um coerente com o fingerprint, e divergir entrega o bot.
IMPERSONATE = "chrome"

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Referer": f"{BASE_URL}/",
}

# `req` seleciona quais blocos a API devolve. la,s,stnf,c,mb = listagem com eventos.
REQ_LISTING = "la,s,stnf,c,mb"
# m,ms,s,c,stnf = página de evento com todos os mercados (inclui MRES + MR12).
REQ_EVENT = "m,ms,s,c,stnf"

# Mercado "Resultado Final SuperOdds" — a odd turbinada do 1X2.
SUPERODDS_MARKET_TYPE = "MR12"
# Mercado "Resultado Final" normal — de onde sai a odd original.
BASE_MARKET_TYPE = "MRES"
# offerTypeId que a Betano usa pra marcar um mercado como SuperOdds.
SUPERODDS_OFFER_TYPE_ID = 1000


@dataclass(frozen=True)
class Sport:
    """Um esporte vigiado. `slug` é da URL pt-BR, `sport_id` é o numérico da API."""

    code: str
    slug: str
    sport_id: int
    name: str


# Descobertos em /api/home/top-events-v2 (sports.byId) + rotas pt-BR.
SPORTS: tuple[Sport, ...] = (
    Sport("FOOT", "futebol", 1, "Futebol"),
    Sport("BASK", "basquete", 2, "Basquete"),
    Sport("TENN", "tenis", 3, "Tênis"),
)

# Fontes de oferta (dá pra desligar uma se a Betano quebrar o formato dela).
ENABLE_MR12 = True
ENABLE_SMART_PICKS = True

# smart-picks: includeTypes filtra as categorias de pick. 4 e 5 são as turbinadas
# (10% e 20%); 0/1/2 vêm sem boost e são descartadas no scraper.
SMART_PICKS_INCLUDE_TYPES = (0, 1, 2, 4, 5)

# ---------------------------------------------------------------------------
# Casas na plataforma Altenar
# ---------------------------------------------------------------------------


# As duas fontes de oferta da Altenar, e a diferença que importa para o diff:
#
# - LISTAGEM: vem do `GetEvents`, que devolve o catálogo INTEIRO toda visita.
#   Se uma oferta dessas some, sumiu de verdade → pode expirar.
# - DETALHE: vem do `GetEventDetails`, pedido só para uns poucos eventos por
#   visita (o orçamento é caro). A ausência dela NÃO prova nada — pode ser só
#   que o evento não foi sondado desta vez.
SUFIXO_FONTE_LISTAGEM = "_1x2"
SUFIXO_FONTE_DETALHE = "_boost"


@dataclass(frozen=True)
class CasaAltenar:
    """Uma casa que roda o sportsbook da Altenar.

    A API é multi-tenant: o endpoint é o mesmo para todas e quem seleciona a
    marca é o parâmetro `integration`. Por isso uma casa nova aqui é **uma
    linha nesta tabela**, não um parser novo.
    """

    nome: str
    slug: str    # o parâmetro `integration` da API
    site: str    # base do link que vai no alerta

    @property
    def fonte_1x2(self) -> str:
        return f"{self.slug}{SUFIXO_FONTE_LISTAGEM}"

    @property
    def fonte_boost(self) -> str:
        return f"{self.slug}{SUFIXO_FONTE_DETALHE}"


# Levantadas e verificadas ao vivo em 2026-08-03 — ver CASAS-MAPEADAS.md.
# A Esportiva vem primeiro por ser a que já estava em produção: os `fonte`
# dela seguem saindo como `esportiva_1x2`/`esportiva_boost`, então as ofertas
# já gravadas continuam casando depois desta mudança.
CASAS_ALTENAR: tuple[CasaAltenar, ...] = (
    CasaAltenar("Esportiva Bet", "esportiva", "https://esportiva.bet.br"),
    CasaAltenar("EstrelaBet", "estrelabet", "https://www.estrelabet.bet.br"),
    CasaAltenar("4Play", "4play", "https://4play.bet.br"),
    CasaAltenar("BetGorillas", "betgorillas", "https://betgorillas.bet.br"),
    CasaAltenar("vupi", "vupi", "https://www.vupi.bet.br"),
    CasaAltenar("BateuBet", "bateu", "https://bateu.bet.br"),
    CasaAltenar("MultiBet", "multibet", "https://multi.bet.br"),
    CasaAltenar("BetPix365", "betpix365", "https://www.betpix365.bet.br"),
    CasaAltenar("JogoDeOuro", "jogodeouro", "https://jogodeouro.bet.br"),
    CasaAltenar("VaiDeBet", "vaidebet", "https://www.vaidebet.bet.br"),
    # Slug tem o ponto mesmo — é o WSDK (Widget SDK da Altenar) que o app usa
    # como chave no localStorage (`WSDK_gingabet.br_*`). Achado em 2026-08-04
    # via captura de XHR em browser real; ver CASAS-PENDENTES.md seção 0.
    CasaAltenar("GingaBet", "gingabet.br", "https://ginga.bet.br"),
)

# ---------------------------------------------------------------------------
# CasaDeAposta — plataforma própria, fora da Altenar
# ---------------------------------------------------------------------------

# Widget de promoções ("SUPER ODDS") do CMS da CasaDeAposta. Achado em
# 2026-08-04 via captura de XHR em browser real — ver CASAS-PENDENTES.md
# seção 0. É um host separado do site principal (`zizy-cms.casadeapostas.tv`),
# público, sem cookie de sessão.
ENABLE_CASADEAPOSTA = os.getenv("ENABLE_CASADEAPOSTA", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------------------
# Novibet — plataforma própria, fora da Altenar
# ---------------------------------------------------------------------------

# Hub "Odds Turbinadas" da Novibet. Achado em 2026-08-05 via captura de XHR
# em browser real — ver CASAS-PENDENTES.md seção 0. Público, sem cookie de
# sessão, mas exige os headers `x-gw-*` de contexto (país/moeda/idioma) —
# sem eles a API quebra com um erro genérico de referência nula antes de
# sequer olhar para os IDs da rota.
ENABLE_NOVIBET = os.getenv("ENABLE_NOVIBET", "1") not in ("0", "false", "False")

# ---------------------------------------------------------------------------
# SportingTech — EsportesDaSorte, OleyBet
# ---------------------------------------------------------------------------

# Esporte virtual "Super Odds" (stId 712), achado em 2026-08-05 via captura
# de XHR em browser real — ver CASAS-PENDENTES.md seção 0. Sem cookie, mas
# exige os headers `origin`/`sec-fetch-*` — sem eles a rota responde `200`
# com `NO_DATA_FOUND`, um "sucesso vazio" que engana. A lista de casas em si
# (`CASAS_SPORTINGTECH`) mora em `sportingtech.py`, não aqui, porque cada
# entrada carrega lógica (`super_odds_url`) além de dados.
ENABLE_SPORTINGTECH = os.getenv("ENABLE_SPORTINGTECH", "1") not in ("0", "false", "False")

# `ENABLE_ESPORTIVA` é o nome antigo, de quando a Altenar era uma casa só.
ENABLE_ALTENAR = os.getenv(
    "ENABLE_ALTENAR", os.getenv("ENABLE_ESPORTIVA", "1")
) not in ("0", "false", "False")
ENABLE_ESPORTIVA = ENABLE_ALTENAR


def _casas_ativas() -> tuple[CasaAltenar, ...]:
    """`CASAS_ALTENAR=esportiva,estrelabet` restringe; vazio liga todas."""
    bruto = os.getenv("CASAS_ALTENAR", "").strip()
    if not bruto:
        return CASAS_ALTENAR
    querem = {s.strip().lower() for s in bruto.split(",") if s.strip()}
    return tuple(c for c in CASAS_ALTENAR if c.slug.lower() in querem)


CASAS_ALTENAR_ATIVAS = _casas_ativas()

# Quantas casas Altenar raspar por ciclo (rodízio). 0 = todas de uma vez.
#
# Por que rodízio em vez de simplesmente baixar o teto de detalhes: com 10 casas
# no teto antigo seriam ~250 requests de detalhe por ciclo de 180s, contra um
# WAF que escala bloqueio por volume. Baixar o teto para 2-3 por casa caberia no
# orçamento, mas cortaria a cobertura *dentro* de cada casa — as ofertas do fim
# da listagem nunca seriam vistas. O rodízio mantém o teto por casa intacto e
# distribui o custo no tempo; é o mesmo princípio da fila de `avaliacoes`.
CASAS_POR_CICLO = int(os.getenv("CASAS_POR_CICLO", "3"))

# Quantos eventos por casa ganham request de detalhe (onde moram os combos e a
# odd original). Teto **por casa, por ciclo** — o custo do ciclo é este número
# vezes `CASAS_POR_CICLO`.
#
# Era 25 quando a Altenar era uma casa só e o gasto era oportunista: só eventos
# que já mostravam mercado turbinado na listagem. Hoje o orçamento é sempre
# gasto por inteiro, porque em várias casas o boost só aparece no detalhe e a
# listagem não dá pista nenhuma (ver comentário em `esportiva._scrape_esporte`).
# 12 × 3 casas = 36 requests de detalhe por ciclo, na mesma ordem de grandeza
# do que já rodava antes desta mudança.
ALTENAR_MAX_DETALHES = int(
    os.getenv("ALTENAR_MAX_DETALHES", os.getenv("ESPORTIVA_MAX_DETALHES", "12"))
)
ESPORTIVA_MAX_DETALHES = ALTENAR_MAX_DETALHES   # nome antigo

# Até que profundidade da listagem sondar em busca de combos turbinados.
#
# Cada casa ordena a listagem do seu jeito, e não dá pra supor que o boost está
# no topo. Medido em 2026-08-03, nos 40 primeiros jogos de futebol:
#
#   EstrelaBet  boosts nos índices 0-6      → o teto de 12 pega tudo
#   4Play       boosts nos índices 0-11     → idem
#   VaiDeBet    boosts nos índices 20-39    → o teto de 12 NÃO pega nada
#
# A VaiDeBet tinha 77 combos turbinados e devolvia zero oferta só por causa
# disso. Como o teto por visita é caro, a saída é varrer a janela aos poucos:
# cada visita começa de um offset diferente e avança, dando a volta. Com janela
# de 48 e teto de 12, uma casa cobre tudo em 4 visitas (~48 min no ritmo
# padrão), dentro do TTL de reavaliação de 60 min.
ALTENAR_JANELA_SONDA = int(os.getenv("ALTENAR_JANELA_SONDA", "48"))

# Quantos dos `ALTENAR_MAX_DETALHES` ficam reservados pros eventos que estão na
# FILA DE AVALIAÇÃO com perna sem cobertura.
#
# Fatiado de DENTRO do orçamento, nunca somado: o custo por ciclo continua
# `ALTENAR_MAX_DETALHES × CASAS_POR_CICLO`. O que muda é pra onde os 12
# apontam.
#
# Existe porque o pool de consenso era subproduto puro da caça a boost: a sonda
# escolhia por "tem mercado turbinado" mais uma janela rotativa, e nada olhava
# o que a avaliação precisava precificar. Medido no banco vivo, das 509 ofertas
# ativas com perna sem cobertura, 31% não tinham NENHUMA outra casa no pool pro
# evento delas e 58% tinham menos que `CONSENSO_MIN_CASAS_PROP`. A falta se
# concentra em Betano e Novibet, as duas casas fora do feed Altenar — jogos de
# UFC, Championship, Cincinnati WTA e MLS que a rotação simplesmente nunca
# sondou.
#
# 4 de 12 é um terço: deixa a maioria do orçamento na caça a boost (que é o que
# gera oferta) e ainda assim dobra a chance de o evento avaliado ter
# referência. Subir isto troca descoberta de oferta por cobertura de consenso —
# é decisão do dono, com o funil por causa do log na mão (`pipeline.py`).
ALTENAR_DETALHES_FILA = int(os.getenv("ALTENAR_DETALHES_FILA", "4"))

# --- Superbet: fonte de REFERÊNCIA, não de oferta --------------------------
# Ver o cabeçalho de `superbet.py`. Ela existe pra ser a TERCEIRA família de
# feed do pool de consenso (hoje só há `altenar` e `betano`), que é o que torna
# `CONSENSO_MIN_FAMILIAS=2` ligável.
ENABLE_SUPERBET = os.getenv("ENABLE_SUPERBET", "1") not in ("0", "false", "False")

# Detalhes por ciclo. Mais alto que o teto da Altenar (12) porque aqui o
# orçamento não é dividido com caça a boost — a Superbet não é fonte de oferta,
# então cada request vira pool puro. E ela é UMA casa, não um rodízio de 11:
# o que ela não sondar neste ciclo não é compensado por nenhuma irmã.
SUPERBET_MAX_DETALHES = int(os.getenv("SUPERBET_MAX_DETALHES", "8"))

# --- CasaDeAposta (livro de mercados): fonte de REFERÊNCIA, não de oferta --
# Ver o cabeçalho de `casadeaposta_livro.py`. Endpoint diferente do CMS de
# combos que `casadeaposta.py` já usa — flag própria pra poder desligar um
# sem afetar o outro. É a 4ª família de feed do pool de consenso.
ENABLE_CASADEAPOSTA_LIVRO = os.getenv("ENABLE_CASADEAPOSTA_LIVRO", "1") not in ("0", "false", "False")

# Páginas por esporte por ciclo (`pageSize=50` fixo no scraper). Diferente da
# Superbet, a listagem já traz o livro de mercados completo — não há request
# de detalhe por evento, então o teto aqui é só sobre profundidade de
# catálogo, não sobre custo por jogo. 6 páginas cobre o catálogo de futebol
# observado na sondagem (2026-08-19: ~280 jogos em 72h de janela).
CASADEAPOSTA_LIVRO_MAX_PAGINAS = int(os.getenv("CASADEAPOSTA_LIVRO_MAX_PAGINAS", "6"))

# Até quantas horas à frente vale gastar request de detalhe.
#
# A janela de sondagem é cara (12 detalhes por casa, e a casa só volta a cada
# ~12 min) e era gasta na ordem em que a API devolve — que não tem relação
# nenhuma com o horário do jogo. Detalhe pedido num jogo de daqui a três dias
# não vira alerta útil, e detalhe pedido num jogo que já começou não vira
# alerta nenhum: os dois roubam a vez de um jogo desta tarde, que é o que
# chegava tarde demais. Com o corte, a rotação varre a janela ÚTIL.
ALTENAR_HORIZONTE_HORAS = float(os.getenv("ALTENAR_HORIZONTE_HORAS", "24"))

POLL_INTERVAL_SECONDS = int(os.getenv("BETANO_POLL_INTERVAL", "180"))
REQUEST_TIMEOUT_SECONDS = 30.0
# Nº de requests simultâneos. Baixo de propósito: não é pra parecer bot.
MAX_CONCURRENCY = 4
# Pausa aleatória entre requests, em segundos (min, max).
REQUEST_JITTER = (0.4, 1.2)

# Se N ciclos seguidos capturarem 0 ofertas, provavelmente o scraper quebrou
# (a Betano mudou a estrutura) em vez de realmente não haver promoção no ar.
BREAKAGE_ALERT_AFTER_EMPTY_RUNS = 3

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = Path(os.getenv("BETANO_DB_PATH", PROJECT_ROOT / "betano_superodds.db"))

# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

# Credenciais só por variável de ambiente — token em arquivo versionado é como
# se vaza. Sem as duas preenchidas o notificador fica inerte e o loop segue
# normal, logando no console como antes.
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()
TELEGRAM_API = "https://api.telegram.org"

# Hora local (0-23) e minuto (0-59) do resumo diário. O alerta de value é raro
# por natureza — na validação foram 0 em 241 ofertas —, então sem um heartbeat
# não dá pra distinguir "nada de bom apareceu" de "o bot morreu".
RESUMO_DIARIO_HORA = int(os.getenv("RESUMO_DIARIO_HORA", "20"))
RESUMO_DIARIO_MINUTO = int(os.getenv("RESUMO_DIARIO_MINUTO", "0"))

# Quanto tempo DEPOIS do horário alvo o resumo ainda pode sair, inclusive já no
# dia seguinte — e continua contando como o relatório do dia que fechou.
#
# Existe porque o disparo não é um agendador: `talvez_resumo_diario` só roda no
# fim de cada ciclo, e o ciclo leva ~100s + `POLL_INTERVAL_SECONDS` de sono —
# na prática ~4,6 min entre duas checagens. Com alvo às 23:59 sobram 60s até a
# meia-noite, então na maioria dos dias a primeira checagem elegível cai já em
# 00:0x. Sem esta tolerância o relatório simplesmente não sairia nesses dias.
#
# Também é o que impede o efeito colateral oposto: sem um teto, subir o bot ao
# meio-dia dispararia na hora o resumo "de ontem". Tem que ser maior que o
# intervalo entre checagens e bem menor que 24h.
RESUMO_TOLERANCIA_MINUTOS = int(os.getenv("RESUMO_TOLERANCIA_MINUTOS", "30"))

# Avisar no Telegram quando o detector de quebra disparar.
ALERTAR_QUEBRA = os.getenv("ALERTAR_QUEBRA", "1") not in ("0", "false", "False")

# --- Liquidação (a aposta deu green ou red?) -------------------------------
#
# Roda incremental, algumas partidas por ciclo, e persiste. O relatório das
# 23:59 só lê o banco: um lote de rede naquele horário seria ~300s de bloqueio
# no exato momento em que falhar é irrecuperável (o resumo se marca como
# enviado mesmo quando o envio falha, pra não virar loop de reenvio).
#
# Cada partida custa ~8 requests no SofaScore (~5s com o rate limit). Com 3 por
# ciclo são ~15s a mais num ciclo de ~100s, dentro do intervalo de 180s.
LIQUIDACAO_MAX_POR_CICLO = int(os.getenv("LIQUIDACAO_MAX_POR_CICLO", "3"))

# Quanto esperar depois do apito inicial pra tentar liquidar. 90 de jogo +
# intervalo + acréscimos + margem pro SofaScore fechar a estatística.
LIQUIDACAO_ATRASO_MIN = int(os.getenv("LIQUIDACAO_ATRASO_MIN", "150"))

# Depois de N tentativas sem casar, a partida é estacionada de vez.
LIQUIDACAO_MAX_TENTATIVAS = int(os.getenv("LIQUIDACAO_MAX_TENTATIVAS", "6"))

# Combo com uma perna perdida E outra não resolvível: conta como red (1) ou
# como desconhecido (0)?
#
# Padrão 1 porque combo quase sempre casa perna de gol resolvível com perna
# exótica, e `red` continua exigindo uma perna CONFIRMADA perdida — nunca
# inferida. Perna não resolvível sozinha nunca vira derrota nos dois modos.
LIQUIDACAO_RED_COM_PERNA_INDEFINIDA = os.getenv(
    "LIQUIDACAO_RED_COM_PERNA_INDEFINIDA", "1") not in ("0", "false", "False")

# Teto de alertas de value por ciclo. Se algo quebrar no cálculo e o edge sair
# inflado em massa, isso evita despejar centenas de mensagens no chat.
MAX_ALERTAS_POR_CICLO = int(os.getenv("MAX_ALERTAS_POR_CICLO", "10"))

# A partir de que ganho o alerta avisa pra conferir o teto de aposta no site.
#
# O ideal seria mostrar o teto real, mas a API da Altenar não publica nenhum
# (conferido: nenhuma chave de limite no payload — ver esportiva.py). E boost
# desproporcional quase sempre TEM teto: uma oferta de 1.29 -> 12.00 não é
# generosidade, é promoção com aposta máxima baixa, aplicada só na hora de
# apostar. Sem este aviso, "apostar 5un" seria uma recomendação inexecutável.
# Os boosts normais ficam em +10% a +25%, então 100% separa bem os dois mundos.
BOOST_ALTO_AVISO_PCT = float(os.getenv("BOOST_ALTO_AVISO_PCT", "100"))

# Teto de ofertas avaliadas por ciclo. Cada oferta com evento casado custa 2
# requests na Pinnacle; no primeiro ciclo, quando TUDO é novo, seriam ~400 de
# uma vez. O excedente não é perdido — a fila de pendentes drena nos ciclos
# seguintes (ver Storage.pendentes_de_avaliacao).
MAX_AVALIACOES_POR_CICLO = int(os.getenv("MAX_AVALIACOES_POR_CICLO", "60"))

# Quanto tempo uma avaliação vale antes da oferta voltar pra fila.
# Não basta avaliar uma vez: a odd da Betano pode ficar parada enquanto a linha
# da Pinnacle anda, e é justamente aí que um edge nasce sem nada "mudar" do
# lado de cá. 0 desliga a reavaliação (avalia cada conteúdo uma única vez).
REAVALIAR_APOS_MINUTOS = int(os.getenv("REAVALIAR_APOS_MINUTOS", "60"))

# Até quantas horas antes do jogo a avaliação tem chance de dar em alguma
# coisa. Medido no README (seção de cobertura da Pinnacle): 67% dos jogos têm
# mercado publicado entre 3 e 12 h antes, contra 3% acima de 48 h.
#
# Isto não descarta a oferta distante — ela continua na fila, só que ATRÁS de
# quem está na janela. O problema medido era o inverso: 699 das 800 ofertas
# ativas eram de jogos a 12-48 h e entupiam o teto de 60 avaliações por ciclo
# com trabalho que ia falhar (3.563 `sem_odd_justa` contra 950 `avaliada`),
# enquanto a oferta do jogo desta tarde esperava atrás delas.
HORIZONTE_AVALIACAO_HORAS = float(os.getenv("HORIZONTE_AVALIACAO_HORAS", "12"))

# TTL de reavaliação para quem está fora da janela acima. Reavaliar de hora em
# hora um jogo de amanhã é pagar Pinnacle pra ouvir o mesmo "sem mercado":
# a fila tinha 114 pendentes para um teto de 60, e não drenava.
REAVALIAR_LONGE_MINUTOS = int(os.getenv("REAVALIAR_LONGE_MINUTOS", "240"))

# Piso entre dois alertas da MESMA oferta, mesmo que a odd turbinada mude.
# A dedup por `alert_hash` já cobre o alerta idêntico; isto é a segunda guarda,
# contra qualquer campo volátil que ainda não conhecemos. Um reajuste real de
# boost dentro da janela é perdido — é o preço de não repetir mensagem.
INTERVALO_MIN_REALERTA = int(os.getenv("INTERVALO_MIN_REALERTA", "30"))

# Quantos pontos percentuais de EDGE a oferta precisa ganhar sobre o melhor
# alerta já enviado pra justificar um segundo envio da MESMA oferta. Existe
# porque `alert_hash` não inclui mais a odd (2026-08-07): sem isto, qualquer
# variação de preço dentro da janela de `INTERVALO_MIN_REALERTA` reabriria a
# porta pro flood que a mudança de `alert_hash` fechou. Comparação é contra o
# MAX(edge_pct) já registrado pra este `offer_id`, não contra o último envio —
# senão uma odd oscilando pra cima e pra baixo dentro da margem reenviaria a
# cada volta.
REALERTA_MELHORA_MIN_PP = float(os.getenv("REALERTA_MELHORA_MIN_PP", "5.0"))

# Teto de quantas vezes a MESMA oferta pode realertar, mesmo com melhora de
# edge legítima repetida. Sem isto uma oferta de vida longa (o rodízio de
# casas deixa ofertas ativas por horas) que melhora aos poucos vira flood de
# novo, só que mais devagar.
MAX_REALERTAS_POR_OFERTA = int(os.getenv("MAX_REALERTAS_POR_OFERTA", "2"))

# Idade máxima da captura para a oferta ainda valer alerta.
# O rodízio de casas MULTIPLICADO pelo rodízio da janela de detalhes faz uma
# oferta ficar até ~1h sem refresh (medido: BetGorillas, evento só na janela
# @36, visto 11:27 e depois só 12:33). Avaliar dado desse tamanho é gastar
# Pinnacle com preço que a revalidação vai derrubar de qualquer jeito.
IDADE_MAX_PARA_ALERTA = int(os.getenv("IDADE_MAX_PARA_ALERTA", "20"))

# Abaixo de quantos minutos para o kickoff o alerta sai MARCADO "em cima da
# hora". Não é um corte: a oferta ainda é enviada, com aviso no topo e sem
# fingir que dá tempo de conferir o teto de aposta com calma. O corte de
# verdade é o jogo já iniciado, que não sai nunca (ver `alerts`).
# 0 desliga a marcação.
MIN_ANTECEDENCIA_ALERTA = int(os.getenv("MIN_ANTECEDENCIA_ALERTA", "15"))

# Quanto a odd revalidada pode divergir da guardada, em pontos decimais.
# 0.0 = tem que ser exatamente igual. A linha mudando (escanteios 9.5 -> 9) não
# passa por aqui: aquilo é mercado diferente, não odd diferente, e a
# revalidação recusa por não achar a seleção.
TOLERANCIA_REVALIDACAO = float(os.getenv("TOLERANCIA_REVALIDACAO", "0.0"))
