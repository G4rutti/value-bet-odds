"""Parser do texto de mercado da Betano → pernas estruturadas.

Calibrado contra as 621 pernas reais capturadas pelo scraper. As pernas vêm
separadas por " + " (é assim que o scraper junta os labels da Betano).

Cobertura: futebol (1X2, total de gols, escanteios, BTTS, dupla chance, placar
exato, handicap por time, totais por equipe e as versões de 1º tempo), tênis
(games da partida e por set, handicap de games/sets em duas grafias — sem e com
":"/parênteses —, vencedor do set, placar de sets em melhor-de-3) e basquete
(total do jogo, total por equipe, handicap por time e props de jogador).

O que continua sem referência — verificado na API, não presumido: cartões,
chutes no gol, faltas, impedimentos e artilheiro não existem em NENHUM special
da Pinnacle (a única `category` publicada para futebol é "Team Props"); aces e
duplas faltas não existem no tênis dela; e tie-break, "resultado após N games"
e placar exato de set não têm mercado equivalente. Essas pernas ficam sem odd
justa, em vez de virar número inventado.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .matcher import normalizar

SEPARADOR_PERNAS = " + "

# --- mercados sem referência na Pinnacle -------------------------------------
# Checados ANTES dos suportados: "Ambas as equipes receberão um cartão" contém
# "Ambas as equipes", mas é cartão, não BTTS.
#
# ⚠️ Cada linha aqui é uma afirmação de que a Pinnacle NÃO publica o mercado.
# Antes de acrescentar uma, confira na API — a versão anterior deste arquivo
# descartava tênis, basquete, handicap e totais por equipe por presunção, e os
# quatro existem.
SEM_COBERTURA: tuple[tuple[str, str], ...] = (
    (r"cart(õ|o)es|cart(ã|a)o", "cartões"),
    (r"chutes?\s+(no|a|ao)\s+gol", "chutes no gol"),
    (r"marcar\s+(em\s+qualquer\s+momento|a\s+qualquer)", "artilheiro (prop de jogador)"),
    # "Gol ou Assistência: <Jogador> (<Time>)" — Novibet. Mesma família do
    # artilheiro: a Pinnacle não publica prop de jogador nenhum no futebol.
    (r"gol\s+ou\s+assist(ê|è|e)ncia", "artilheiro (prop de jogador)"),
    # "Marcador: <Jogador> 1+" (CasaDeAposta) — mesmo artilheiro, outro rótulo.
    (r"^\s*marcador\s*:", "artilheiro (prop de jogador)"),
    # "Primeiro a marcar: <Time>" (CasaDeAposta) — qual TIME faz o primeiro
    # gol. Diferente de artilheiro (que é sobre jogador); a Pinnacle publica
    # "<Time> To Score?" (marca em algum momento) mas não "primeiro a
    # marcar" — checado ao vivo em 2026-08-05, não existe esse special.
    (r"primeiro\s+a\s+marcar", "primeiro a marcar (sem equivalente na Pinnacle)"),
    # A ordem das palavras varia por casa: "duplas faltas" (Betano) e "faltas
    # duplas"/"total faltas duplas" (Novibet) são o mesmo mercado.
    (r"duplas?\s+faltas?|faltas?\s+duplas?", "duplas faltas"),
    (r"\baces?\b", "aces"),
    # "<Time> Marcar em Ambos os Tempos" / "<Time> Para Ganhar Um Dos Tempos"
    # — probabilidade CONJUNTA (marcar/vencer nos dois tempos, ou em pelo
    # menos um) que a Pinnacle não publica diretamente: ela só tem "<Time> To
    # Score?"/"Goals" pro jogo inteiro e pro 1º tempo separadamente, não uma
    # combinação pronta dos dois. Calcular isso exigiria modelo de correlação
    # (mesma categoria de risco do `modelo_gols.py`), não é parsing de texto.
    (r"marcar\s+em\s+ambos\s+os\s+tempos|ganhar\s+um\s+dos\s+tempos",
     "probabilidade conjunta entre tempos (sem equivalente na Pinnacle)"),
    # Tênis: só o que realmente não existe na Pinnacle.
    (r"tie\s*breaks?", "tie-break"),
    (r"resultado\s+ap(ó|o)s\s+\d+\s+games?", "resultado após N games"),
    (r"resultado\s+no\s+set", "placar exato de set"),
    # Basquete: cestinha do jogo não é publicado.
    (r"maior\s+n(ú|u)mero\s+de\s+pontos", "cestinha do jogo"),
    (r"pr(ó|o)ximo\s+gol|(ú|u)ltima\s+equipe", "sequência de gols"),

    # --- bet-builder da Altenar: a perna perdeu o nome do mercado ------------
    # No `GetEventDetails` o `marketId` das pernas de bet-builder não vem no
    # array `markets`, então o parser da casa só consegue gravar o nome da
    # SELEÇÃO. Sobra um rótulo que não diz de que mercado é.
    #
    # ⚠️ Parecem parseáveis e não são. "Mais de 0.5 + Mais de 0.5" é uma oferta
    # real da Esportiva: duas pernas de mercados DIFERENTES com o mesmo rótulo.
    # Ler as duas como "total de gols do jogo acima de 0.5" daria duas
    # probabilidades de ~0.95, odd justa ~1.1 contra uma paga de 5.0 — edge
    # inventado, do mesmo tipo do falso +141% do Mirassol. Recusar é a única
    # saída correta enquanto o nome do mercado não vier no payload.
    #
    # ⚠️ Checado ao vivo em 2026-08-05 se dava pra recuperar o mercado por
    # outra via: a entrada de `odds` (seleção) NÃO carrega `marketId` próprio.
    # O que ela tem é `typeId` (ex. 12) e `sv` (ex. "0.5") — a linha, não o
    # mercado. Duas seleções de mercados diferentes (escanteios "mais de 0.5"
    # e gols do time "mais de 0.5") têm o mesmo `typeId`/`sv`, então não dá
    # pra distinguir por aí. Não há recuperação possível com o payload atual;
    # recusar continua sendo a única saída correta.
    (r"^\s*(mais|menos)\s+de\s+\d+([.,]\d+)?\s*$", "seleção sem mercado (bet-builder)"),
    # Props de jogador do bet-builder — o `sv` da seleção traz `ls:player:NNN`.
    # A Pinnacle não publica artilheiro, então não há referência possível.
    (r"^\s*qualq\.?\s+(altura|momento)\s*$", "artilheiro (prop de jogador)"),
    (r"^\s*(primeiro|(ú|u)ltimo)\s*$", "artilheiro (prop de jogador)"),
)

# --- chave canônica pra casamento no consenso (Etapa 1) ---------------------
# `market_key`/`suportado`/`motivo` continuam intocados: são o espaço da
# Pinnacle. `ChaveConsenso` é um espaço PARALELO, só pra `consenso.py` casar o
# rótulo da oferta ("Total de Cartões Mais de 4.5") contra a grafia do pool
# ("Total cartões: Mais de 4.5") sem fuzzy de string — ver a nota da skill
# `value-bet-methodology` sobre por que fuzzy de nome de mercado é proibido.
#
# Duas chaves só casam se os QUATRO campos forem iguais. Nunca dê `market_key`
# a uma perna que ganhou `ChaveConsenso`: roteá-la pra `prob_da_perna`
# repetiria a classe de bug "mercado restrito casado com referência ampla"
# (o falso +23,6% do Internacional).
@dataclass(frozen=True)
class ChaveConsenso:
    familia: str          # "cartoes" | "chutes_gol" | "artilheiro" | "tie_break" | ...
    escopo: str            # "jogo" | "1t" | "time:<norm>" | "jogador:<norm>"
    lado: str | None       # "over" | "under" | "sim" | "nao"
    linha: float | None


# Traduz o `motivo` (já verificado contra a API — ver o cabeçalho do arquivo)
# pra uma família de mercado. Motivo sem entrada aqui devolve `None` em
# `chave_consenso` — nunca chuta a família de um motivo desconhecido.
_FAMILIA_POR_MOTIVO: dict[str, str] = {
    "cartões": "cartoes",
    "chutes no gol": "chutes_gol",
    "artilheiro (prop de jogador)": "artilheiro",
    "duplas faltas": "duplas_faltas",
    "aces": "aces",
    "tie-break": "tie_break",
}


# --- mercados COMBINADOS num rótulo só ------------------------------------
# A Esportiva Bet vende condições compostas como uma seleção única:
#   "1x2 e ambas equipes marcam: Vélez Sarsfield e não"
# Isso NÃO é o combo separado por " + ": é um mercado próprio, com odd própria.
#
# ⚠️ Estes precisam ser recusados ANTES dos parsers normais. Sem esta guarda o
# `_parse_resultado` casava o pedaço "1x2 ... Vélez Sarsfield", ignorava o
# "e ambas equipes marcam: não" e comparava a odd do combinado (4.20) contra a
# odd justa do 1X2 puro (1.83) — edge falso de +130%, com alerta e stake de 5un.
# Recusar é a única saída segura enquanto não houver cálculo de probabilidade
# conjunta pra estes mercados.
COMBINADOS = re.compile(
    r"\be\s+ambas\s+(as\s+)?equipes\s+marcam"
    r"|\be\s+total\s+de\s+gols"
    r"|\be\s+(mais|menos)\s+de\s+\d"
    r"|\be\s+handicap"
    r"|\be\s+placar"
    r"|\be\s+escanteios"
    r"|chance\s+dupla\s+e\b"
    r"|resultado\s+e\b"
    r"|\bmulti\s?gols?\b",
    re.IGNORECASE,
)

PRIMEIRO_TEMPO = re.compile(r"1[.°ºo]?\s*tempo|primeiro\s+tempo|intervalo", re.IGNORECASE)

# A Pinnacle não publica 2º tempo (só período 0 = jogo inteiro e 1 = 1º
# tempo/intervalo) — ver a nota em `parse_leg`. Detectado cedo pra rejeitar
# antes de qualquer parser de mercado ter chance de tratar como jogo inteiro.
SEGUNDO_TEMPO = re.compile(r"2[.°ºo]?\s*tempo|segundo\s+tempo", re.IGNORECASE)

# Qualificador de período no começo do rótulo: "1º tempo - Grêmio total de
# gols". Precisa sair antes de procurar o nome da equipe, senão o time vira
# "1º tempo - Grêmio" e não casa com ninguém.
_PREFIXO_PERIODO = re.compile(
    r"^\s*(\d[.°ºo]?\s*tempo|primeiro\s+tempo|segundo\s+tempo|intervalo)\s*[-–:]\s*",
    re.IGNORECASE,
)

# O que aparece ANTES de "total de gols" mas não é nome de equipe. Sem isto,
# "1º tempo - total de gols" viraria um total de equipe com time_nome
# "1º tempo", que nunca casa com ninguém — perda silenciosa de cobertura.
_NAO_E_TIME = re.compile(
    r"^\s*(\d[.°ºo]?\s*tempo"
    r"|primeiro\s+tempo|segundo\s+tempo|intervalo"
    r"|tempo\s+regulamentar|prorroga(ç|c)(ã|a)o"
    r"|jogo|partida|mais/menos|total|asi(á|a)tico"
    r"|\d+(\.\d+)?)\s*$",
    re.IGNORECASE,
)

# Props de basquete: frase da estatística -> sufixo da chave. A ordem importa:
# "Pontos, Rebotes e Assistências" e as duas grafias de triplos (que contêm a
# palavra "pontos") têm que ser testadas ANTES de qualquer padrão bare de
# "pontos"/"points" sozinho, senão "total de cestas de três pontos marcadas"
# combinaria com "pontos" primeiro e viraria a estatística errada.
#
# Achado real na Novibet (2026-08-05): ela escreve "<Jogador> - Rebotes :" /
# "<Jogador> - Assistências :" (sem "total de") e "<Jogador> - Points :" (em
# inglês, sem "total de") — daí os pares bare/`-?\s*` abaixo, cobrindo com e
# sem o traço que a Novibet usa entre o nome e a estatística.
STATS_BASQUETE: tuple[tuple[str, str], ...] = (
    (r"total\s+de\s+pontos,\s*rebotes\s+e\s+assist(ê|e)ncias", "pra"),
    (r"arremessos\s+de\s+tr(ê|e)s\s+pontos\s+convertidos", "triplos"),
    (r"total\s+de\s+cestas\s+de\s+tr(ê|e)s\s+pontos\s+marcadas", "triplos"),
    (r"total\s+de\s+rebotes", "rebotes"),
    (r"-?\s*\brebotes\b", "rebotes"),
    (r"total\s+de\s+assist(ê|e)ncias", "assistencias"),
    (r"-?\s*\bassist(ê|e)ncias\b", "assistencias"),
    (r"total\s+de\s+pontos", "pontos"),
    (r"-?\s*\bpoints\b", "pontos"),
    (r"-?\s*\bpontos\b", "pontos"),
)


@dataclass
class Leg:
    """Uma perna do mercado, mapeada (ou não) pra um mercado da Pinnacle."""

    texto: str
    market_key: str | None = None   # "h2h", "totals:2.5", "games_s1:9.5"...
    selecao: str | None = None      # "home"/"draw"/"away"/"over"/"under"/"Yes"/"No"
    time_nome: str | None = None    # quando o lado vem por nome de time/jogador
    handicap_linha: float | None = None  # handicap: a linha do lado citado
    exige_mercado: str | None = None     # guarda: só vale se este mercado existir
    suportado: bool = False
    motivo: str | None = None
    # Jogo dono desta perna, só preenchido quando a oferta é um combo de jogos
    # DIFERENTES (ex.: promo Novibet "Festival de Gols"). `novibet.py` prefixa
    # o texto cru com "[Jogo] " quando detecta isso; `parse_mercado` extrai o
    # prefixo pra cá antes de parsear o resto. `None` = perna do jogo único da
    # oferta (o caso comum, sem mudança de comportamento).
    evento_texto: str | None = None
    # Espaço PARALELO ao de `market_key`: só preenchido quando `suportado` é
    # `False`, e só consumido por `consenso.py`. `fair_odds.py` (o caminho
    # Pinnacle) nunca lê este campo — ver a nota acima de `ChaveConsenso`.
    consenso_chave: "ChaveConsenso | None" = None

    def __str__(self) -> str:
        if not self.suportado:
            return f"{self.texto}  [sem cobertura: {self.motivo}]"
        alvo = self.selecao or f"time:{self.time_nome}"
        return f"{self.texto}  [{self.market_key} -> {alvo}]"


def _sem_cobertura(texto: str) -> str | None:
    for padrao, motivo in SEM_COBERTURA:
        if re.search(padrao, texto, re.IGNORECASE):
            return motivo
    return None


def _sufixo(texto: str) -> str:
    return "_1t" if PRIMEIRO_TEMPO.search(texto) else ""


def _linha(texto: str) -> tuple[str, float] | None:
    """Extrai "Mais de 2.5" -> ("over", 2.5); "Over 15.5" -> ("over", 15.5).

    O "de" é obrigatório em português ("mais DE", "menos DE") mas não existe
    em inglês ("Over 15.5", achado real na Novibet) — por isso é opcional em
    vez de trocado por sinônimo: manter obrigatório pra "mais"/"menos" faria
    "Mais 2.5" (sem "de") também passar, o que nunca foi visto e não vale o
    risco de casar algo que não devia.
    """
    m = re.search(r"(mais|menos)\s+de\s+(\d+[.,]?\d*)|(over|under)\s+(\d+[.,]?\d*)",
                  texto, re.I)
    if not m:
        return None
    palavra = (m.group(1) or m.group(3)).lower()
    valor = m.group(2) or m.group(4)
    lado = "over" if palavra in ("mais", "over") else "under"
    return lado, float(valor.replace(",", "."))


def _limite(texto: str) -> float | None:
    """"18+" -> 17.5. A Betano publica limite inteiro; a Pinnacle, linha .5."""
    m = re.search(r"(\d+)\s*\+\s*$", texto.strip())
    return float(m.group(1)) - 0.5 if m else None


# "Cartões do Grêmio Mais de 1.5" -> entidade "Grêmio" depois da palavra do
# mercado. A ordem das alternativas do lookahead importa: "mais de"/"menos de"
# tem que vir antes de "over"/"under" só por organização, não é load-bearing.
_ENTIDADE_APOS_MERCADO = re.compile(
    r"^(?:do|da|de)\s+(.+?)\s*(?=mais\s+de\b|menos\s+de\b|over\b|under\b"
    r"|sim\s*$|n(?:ã|a)o\s*$|\d+\s*\+\s*$|$)",
    re.IGNORECASE)

# "Chutes a Gol - Jesse Shaun Derry (ALA)" / "Chutes - Kauã Almeida da Costa
# (PON)" — grafia Altenar de prop de JOGADOR no pool de consenso: o nome vem
# DEPOIS da palavra do mercado, separado por hífen e sem o "do/da/de" que
# `_ENTIDADE_APOS_MERCADO` exige. Sem isto o rótulo do pool caía no escopo
# "jogo" e nunca casava com a grafia da oferta ("Lucas Barbosa Chutes no gol
# 1+", que produz `jogador:...`) — 30 pernas de chute ao gol casando 0 casas.
_JOGADOR_APOS_MERCADO = re.compile(
    r"^[-–]\s*(.+?)\s*(?=mais\s+de\b|menos\s+de\b|over\b|under\b"
    r"|sim\s*$|n(?:ã|a)o\s*$|\d+\s*\+\s*$|$)",
    re.IGNORECASE)

# "Chutes a Gol do Jogador (Carles Alena (ALA))" — o nome real está no
# parêntese, e o texto em volta ("do Jogador", "- Inclui substitutos") é
# qualificação, não entidade. Pega o parêntese MAIS INTERNO com nome de
# pessoa; a sigla de time em caixa alta ("(ALA)", "(ETT)") é descartada por
# `_SIGLA_DE_TIME`.
_PARENTESES = re.compile(r"\(([^()]*)\)")
_SIGLA_DE_TIME = re.compile(r"^[A-ZÀ-Ý0-9\s.'-]{1,5}$")

# Sufixo de qualificação que a Altenar cola no fim do rótulo e que não faz
# parte do nome da entidade.
_QUALIFICADOR = re.compile(
    r"\s*[-–]\s*(inclui\s+substitutos?|incl\.?\s+substitutos?)\s*$", re.IGNORECASE)

# "Al Ettifaq FC total cartões" -> a entidade é "Al Ettifaq FC", e o "total"
# grudado no fim é a palavra do mercado, não parte do nome. É também o que
# distingue TIME de JOGADOR nesta grafia: quem vem antes de "total <mercado>"
# é equipe ("<Time> total cartões"), quem vem antes do mercado puro é jogador
# ("Lucas Barbosa Chutes no gol 1+").
_TOTAL_NO_FIM = re.compile(r"\b(totais|total)\s*$", re.IGNORECASE)

# Mercado COMBINADO no pool: "Para marcar em qualquer momento & 1x2 (Łukasz
# Zjawiński)", "Jogador a marcar o primeiro gol & Placar exato", "Marcador a
# Qualquer Momento & Placar Correto (...)". As seleções desses mercados são os
# desfechos do OUTRO mercado (1x2, placar), condicionados ao gol do jogador —
# conferido no pool: 34.272 linhas só da primeira grafia.
#
# ⚠️ Precificar "Marcar em qualquer momento: Griezmann" contra isto é a
# armadilha "mercado restrito casado com referência ampla" da skill
# `value-bet-methodology`, com o agravante de que o combinado é MENOS provável
# que a perna sozinha — o edge sairia inflado, não deflacionado. `COMBINADOS`
# não pega estes porque a Altenar usa "&" onde a Esportiva usa " e ".
_COMBINADO_POOL = re.compile(r"&|\bplacar\s+(exato|correto)\b", re.IGNORECASE)

# "Total de chutes a Gol Deportivo Alavés" / "Total (2.5) Chutes Ponte Preta"
# — entidade DEPOIS do mercado e SEM o "do/da/de" de `_ENTIDADE_APOS_MERCADO`.
# Só é consultado quando o rótulo disse "Total ..." antes do mercado: é esse
# "Total" que autoriza ler o resto como equipe. Sem essa amarra, qualquer
# sobra de texto depois do mercado viraria nome de time.
# `.*?` (não `.+?`): em "Totais chutes a Gol Mais de 8.5" não há equipe
# nenhuma depois do mercado, e o lookahead casa já na posição 0. Com `.+?` o
# motor era obrigado a consumir um caractere, ia até o fim e devolvia a
# própria linha ("mais de 8 5") como nome de time — o total do JOGO virava
# total de equipe, exatamente a troca de escopo que esta função existe pra
# evitar. Grupo vazio significa "sem equipe": o escopo continua o do jogo.
_ENTIDADE_APOS_TOTAL = re.compile(
    r"^(.*?)\s*(?=mais\s+de\b|menos\s+de\b|over\b|under\b"
    r"|sim\s*$|n(?:ã|a)o\s*$|\d+\s*\+\s*$|$)",
    re.IGNORECASE)


def _jogador_parentizado(texto: str) -> str | None:
    """Nome de jogador entre parênteses, ou None.

    Trata o aninhamento real do pool ("(Carles Alena (ALA))") tirando os
    grupos mais internos e reescaneando. Descarta sigla de time ("(ALA)",
    "(ETT)") e linha numérica ("(2.5)"), que ocupam o mesmo parêntese em
    outras grafias.
    """
    candidatos: list[str] = []
    atual = texto
    for _ in range(3):   # aninhamento observado é 1; 3 é folga barata
        grupos = _PARENTESES.findall(atual)
        if not grupos:
            break
        candidatos += grupos
        atual = _PARENTESES.sub("", atual)
    for bruto in candidatos:
        nome = bruto.strip()
        if not nome or _SIGLA_DE_TIME.match(nome):
            continue
        # Nome de gente tem minúscula; sobra em caixa alta é sigla comprida.
        if not re.search(r"[a-zà-ÿ]", nome):
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]{2,}", nome):
            continue
        return nome
    return None


def chave_consenso(texto: str) -> ChaveConsenso | None:
    """Chave estruturada pra casar uma perna sem cobertura Pinnacle no consenso.

    Só produz chave pra motivos com entrada em `_FAMILIA_POR_MOTIVO` — os
    mesmos mercados que `SEM_COBERTURA` já verificou contra a API. Motivo sem
    família, ou 2º tempo (sem mercado equivalente hoje pra estas famílias,
    mesma armadilha da seção "Períodos" da skill `value-bet-methodology`),
    devolvem `None`. `None` nunca casa com nada, nem consigo mesmo por `==`
    dataclass — é a recusa explícita.
    """
    texto = texto.strip()
    if SEGUNDO_TEMPO.search(texto):
        return None
    if _COMBINADO_POOL.search(texto) or COMBINADOS.search(texto):
        return None
    motivo = _sem_cobertura(texto)
    if motivo is None:
        return None
    familia = _FAMILIA_POR_MOTIVO.get(motivo)
    if familia is None:
        return None

    periodo = "1t" if PRIMEIRO_TEMPO.search(texto) else "jogo"
    corpo = _QUALIFICADOR.sub("", _PREFIXO_PERIODO.sub("", texto).strip()).strip()

    m_familia = None
    for padrao, mot in SEM_COBERTURA:
        if mot != motivo:
            continue
        m_familia = re.search(padrao, corpo, re.IGNORECASE)
        if m_familia:
            break

    escopo = periodo
    if m_familia:
        # `consenso._normalizar` (não `matcher.normalizar`) pra nome de
        # JOGADOR: este é normalizador genérico de rótulo, não afinado pra
        # time — o `RUIDO` de `matcher.normalizar` come "jr"/"junior"/"u\d{2}",
        # que corromperia nome de jogador. Import tardio: `consenso.py`
        # importa este módulo no nível de topo (pro casamento canônico),
        # então importar `consenso` aqui no topo do arquivo formaria ciclo —
        # em tempo de chamada os dois módulos já terminaram de carregar.
        from .consenso import _normalizar as _normalizar_rotulo

        antes = corpo[: m_familia.start()].strip()
        antes = re.sub(r"\b(de|do|da)\s*$", "", antes, flags=re.IGNORECASE).strip()
        depois = corpo[m_familia.end():].strip()

        # "Al Ettifaq FC total cartões" / "Total (2.5) Chutes Ponte Preta": o
        # "total" grudado no fim do nome é a palavra do mercado (e a linha
        # entre parênteses, ruído). Descolar antes de decidir time vs.
        # jogador — é o que fazia a primeira grafia sair como
        # `jogador:al ettifaq fc total` (5.480 linhas no pool) em vez de
        # `time:al ettifaq fc`.
        antes_limpo = _PARENTESES.sub(" ", antes).strip()
        antes_sem_total = _TOTAL_NO_FIM.sub("", antes_limpo).strip()
        virou_total = antes_sem_total != antes_limpo

        # Ordem: parêntese primeiro, porque quando ele existe é ele que tem o
        # nome real ("Chutes a Gol do Jogador (Carles Alena (ALA))"); o texto
        # em volta é qualificação.
        jogador = _jogador_parentizado(corpo)
        if jogador is None:
            m_jog = _JOGADOR_APOS_MERCADO.match(depois)
            if m_jog:
                # "Chutes a Gol - Jesse Shaun Derry (ALA)" — sigla de time no
                # fim não faz parte do nome.
                jogador = _PARENTESES.sub("", m_jog.group(1)).strip() or None

        if jogador:
            norm = _normalizar_rotulo(jogador)
            if norm:
                escopo = f"jogador:{norm}"
        elif virou_total and antes_sem_total and not _NAO_E_TIME.match(antes_sem_total):
            # "<Time> total cartões" / "<Time> total de chutes a Gol".
            norm = normalizar(antes_sem_total)
            if norm:
                escopo = f"time:{norm}"
        elif virou_total and not antes_sem_total:
            # "Total de chutes a Gol Deportivo Alavés": só a palavra do
            # mercado antes, e a equipe depois. Sem nada depois ("Totais
            # chutes a Gol") o escopo continua sendo o do JOGO, que é o certo.
            m_time = _ENTIDADE_APOS_TOTAL.match(depois)
            if m_time and m_time.group(1).strip():
                norm = normalizar(_PARENTESES.sub(" ", m_time.group(1)).strip())
                if norm:
                    escopo = f"time:{norm}"
        elif antes and not virou_total and not _NAO_E_TIME.match(antes):
            # Entidade ANTES da palavra do mercado, sem "total" no meio
            # ("Lucas Barbosa Chutes no gol 1+") é nome de JOGADOR: casas
            # escrevem prop de jogador assim.
            norm = _normalizar_rotulo(antes)
            if norm:
                escopo = f"jogador:{norm}"
        else:
            # Entidade DEPOIS, como "do <Time>"/"da <Time>" ("Cartões do
            # Grêmio Mais de 1.5") é nome de TIME — grafia possessiva comum
            # nesta família de rótulo.
            m_time = _ENTIDADE_APOS_MERCADO.match(depois)
            if m_time and m_time.group(1).strip():
                norm = normalizar(m_time.group(1).strip())
                if norm:
                    escopo = f"time:{norm}"

    lado: str | None
    valor: float | None
    linha_info = _linha(corpo)
    if linha_info:
        lado, valor = linha_info
    else:
        limite = _limite(corpo)
        if limite is not None:
            lado, valor = "over", limite
        else:
            m_sim = re.search(r"\b(sim|n(?:ã|a)o)\s*$", corpo, re.IGNORECASE)
            if m_sim:
                lado = "sim" if m_sim.group(1).lower() == "sim" else "nao"
                valor = None
            else:
                lado, valor = None, None

    # Sem `lado` a chave não identifica um DESFECHO, só um mercado — e
    # `consenso._mercados_por_casa_canonico` escolhe a seleção pelo `lado`.
    # Com `lado=None` ele pegava a primeira seleção do bloco, em ordem de
    # dicionário. Medido no banco: a perna "Cartões 1x2: 2" (fora, La Serena,
    # 2.08) casava com "universidade de concepcion" (casa, 2.45) em 7 casas —
    # precificando o lado errado. Pior: "Cartões 1x2" e "Cartões exatos" são
    # mercados diferentes e produziam a MESMA chave (cartoes/jogo/None/None).
    #
    # Recusar custa zero cobertura hoje (os únicos casamentos canônicos vivos
    # são `lado="over"`) e fecha a porta pra um edge que mede a seleção errada.
    if lado is None:
        return None

    return ChaveConsenso(familia=familia, escopo=escopo, lado=lado, linha=valor)


# Set escrito por extenso, do jeito da Altenar ("Primeiro set - total jogos").
# A Pinnacle indexa o set pelo número (`games_s1`), então a tradução mora aqui.
ORDINAIS_SET: dict[str, int] = {
    "primeiro": 1, "segundo": 2, "terceiro": 3, "quarto": 4, "quinto": 5,
}


# "Handicap de sets: Brandon Nakashime (-1.5)" / "Primeiro set - handicap de
# jogos: Flavio Cobolli (-1.5)" — grafia da vupi, com ":" e a linha entre
# parênteses. A Betano escreve sem ":" e sem parênteses ("Handicap de games
# Matteo Berrettini -2.5", casado mais abaixo) — mesma classe de perda das
# outras variantes de grafia da Altenar: mercado que a Pinnacle já publica,
# só não estava calibrado pra esta grafia.
_HANDICAP_TENIS_COLON = re.compile(
    r"(?:(?P<ordinal>primeiro|segundo|terceiro|quarto|quinto)\s+set\s*[-–]\s*)?"
    r"handicap\s+de\s+(?P<unidade>sets?|jogos)\s*:\s*"
    r"(?P<jogador>.+?)\s*\(\s*(?P<linha>[+-]?\d+(?:[.,]\d+)?)\s*\)\s*$",
    re.IGNORECASE)


# --- tênis -------------------------------------------------------------------

def _parse_tenis(texto: str) -> Leg | None:
    m = _HANDICAP_TENIS_COLON.search(texto)
    if m:
        linha = float(m.group("linha").replace(",", "."))
        jogador = m.group("jogador").strip()
        if m.group("unidade").lower().startswith("jogo"):
            ordinal = m.group("ordinal")
            sufixo = f"_s{ORDINAIS_SET[ordinal.lower()]}" if ordinal else ""
            chave = f"games_spread{sufixo}"
        else:
            chave = "sets_spread"
        return Leg(texto=texto, market_key=chave, time_nome=jogador,
                   handicap_linha=linha, suportado=True)

    # "Total de Games no Set (Set 1) Mais de 9.5" -> período = o set
    m = re.search(r"total\s+de\s+games\s+no\s+set\s*\(\s*set\s*(\d)\s*\)", texto, re.I)
    if m:
        linha = _linha(texto)
        if not linha:
            return Leg(texto=texto, motivo="games do set sem linha reconhecível")
        lado, valor = linha
        return Leg(texto=texto, market_key=f"games_s{m.group(1)}:{valor}",
                   selecao=lado, suportado=True)

    # "Primeiro set - total jogos: Mais de 10.5" — a mesma coisa que o de cima,
    # escrita do jeito da Altenar: set por extenso e "jogos" no lugar de
    # "games". A Pinnacle publica isso (`units=Games`), então a perna morria de
    # graça em "mercado não reconhecido".
    m = re.search(rf"({'|'.join(ORDINAIS_SET)})\s+set\s*[-–:]\s*total\s+(?:de\s+)?jogos",
                  texto, re.I)
    if m:
        linha = _linha(texto)
        if not linha:
            return Leg(texto=texto, motivo="games do set sem linha reconhecível")
        lado, valor = linha
        return Leg(texto=texto, market_key=f"games_s{ORDINAIS_SET[m.group(1).lower()]}:{valor}",
                   selecao=lado, suportado=True)

    # "Games Mais de 22.5" / "Total de Games Mais de 23.5" (Betano) / "Total
    # de Games : Mais de 20,5" (Novibet, com ":" entre "Games" e "Mais de") ->
    # partida inteira.
    if re.match(r"^\s*(total\s+de\s+)?games\s*:?\s*(mais|menos)\s+de", texto, re.I):
        linha = _linha(texto)
        if not linha:
            return Leg(texto=texto, motivo="games sem linha reconhecível")
        lado, valor = linha
        return Leg(texto=texto, market_key=f"games:{valor}", selecao=lado, suportado=True)

    # "<Jogador> total jogos: Mais de 12.5" (grafia da vupi) — total de GAMES
    # do PRÓPRIO JOGADOR, não da partida. Ancorado ANTES da variante sem
    # jogador porque senão caía direto em "mercado não reconhecido": o regex
    # de baixo é `^\s*total...`, que não cobre nome na frente. A Pinnacle
    # (`_mapear_tenis`) só publica total de games da PARTIDA — não tem
    # equivalente pra total por jogador —, então isto fica sem cobertura de
    # propósito, só que com o motivo certo em vez de "não reconheço isso".
    m = re.match(r"^\s*(.+?)\s+total\s+(?:de\s+)?jogos\s*:?\s*(mais|menos)\s+de", texto, re.I)
    if m and m.group(1).strip():
        return Leg(texto=texto, motivo="total de games do jogador não coberto pela Pinnacle")

    # "Total jogos: Menos de 25.5" — variante da anterior, partida inteira.
    if re.match(r"^\s*total\s+(?:de\s+)?jogos\s*:?\s*(mais|menos)\s+de", texto, re.I):
        linha = _linha(texto)
        if not linha:
            return Leg(texto=texto, motivo="games sem linha reconhecível")
        lado, valor = linha
        return Leg(texto=texto, market_key=f"games:{valor}", selecao=lado, suportado=True)

    # "Handicap de games Matteo Berrettini -2.5" — a linha é do jogador citado,
    # e só dá pra saber se ele é o mandante depois de casar o evento.
    m = re.search(r"handicap\s+de\s+games\s+(.+?)\s*([+-]\s*\d+[.,]?\d*)\s*$", texto, re.I)
    if m:
        linha = float(m.group(2).replace(" ", "").replace(",", "."))
        return Leg(texto=texto, market_key="games_spread", time_nome=m.group(1).strip(),
                   handicap_linha=linha, suportado=True)

    # "Vencedor do Set (Set 1) Rafael Jodar"
    m = re.search(r"vencedor\s+do\s+set\s*\(\s*set\s*(\d)\s*\)\s*(.+)$", texto, re.I)
    if m:
        return Leg(texto=texto, market_key=f"sets_h2h_s{m.group(1)}",
                   time_nome=m.group(2).strip(), suportado=True)

    # "Resultado exato (sets) 2 - 0" (Betano) / "Resultado Correto : 2 - 0"
    # (Novibet, sem "exato"/"(sets)") — mesmo placar de sets. Continua sendo
    # placar de SETS e não de gols porque usa "-" entre os números; o placar
    # exato de futebol usa ":" ("Resultado Correto: 1:0", tratado em
    # `_parse_especiais`) — os dois nunca colidem.
    m = re.search(r"resultado\s+(?:exato|correto)\s*(?:\(\s*sets\s*\))?\s*:?\s*(\d)\s*-\s*(\d)",
                  texto, re.I)
    if m:
        casa, fora = int(m.group(1)), int(m.group(2))
        # Em melhor-de-3, "2-0" é exatamente "vencer com handicap de -1.5 sets".
        # A guarda `sets_total:2.5` confirma que o jogo é bo3: em bo5 esse placar
        # não é final e a equivalência seria falsa.
        if (casa, fora) == (2, 0):
            return Leg(texto=texto, market_key="sets_spread:-1.5", selecao="home",
                       exige_mercado="sets_total:2.5", suportado=True)
        if (casa, fora) == (0, 2):
            return Leg(texto=texto, market_key="sets_spread:1.5", selecao="away",
                       exige_mercado="sets_total:2.5", suportado=True)
        return Leg(texto=texto, motivo="placar de sets sem equivalente de mercado único")

    return None


# --- basquete ----------------------------------------------------------------

# "Nyara Sabally (TOR) mais 4.5" — prop de jogador do bet-builder da Altenar,
# onde o `marketId` da perna não vem no payload e sobra só o nome da seleção.
# O rótulo não diz QUAL estatística é, e a Pinnacle publica quatro do mesmo
# jogador (pontos, rebotes, assistências, triplos).
#
# A sigla do time entre parênteses é o que garante que isto é prop de jogador e
# não outra coisa; ela sai do nome antes de normalizar, porque a Pinnacle
# publica só "Nyara Sabally".
_PROP_SEM_STAT = re.compile(
    r"^\s*(?P<jogador>[^()]{4,40}?)\s*\(\s*[A-Z]{2,4}\s*\)\s*"
    r"(?P<lado>mais|menos|over|under)\s+(?:de\s+)?(?P<linha>\d+[.,]?\d*)\s*$")


def _parse_prop_sem_stat(texto: str) -> Leg | None:
    """Prop de jogador cuja estatística o rótulo não informa.

    A chave sai com `{stat}` em aberto e quem fecha é `fair_odds`, que tem o
    jogo em mãos: se exatamente UM `player:*:{jogador}:{linha}` existir naquele
    jogo, é esse; se dois existirem (4.5 serve pra rebote e pra assistência), a
    perna é recusada. Chutar a estatística aqui compararia a odd de rebotes
    contra a justa de pontos — o mesmo tipo de edge inventado que a guarda
    `COMBINADOS` já existe pra impedir.
    """
    m = _PROP_SEM_STAT.match(texto)
    if not m:
        return None
    lado = "over" if m.group("lado").lower() in ("mais", "over") else "under"
    linha = float(m.group("linha").replace(",", "."))
    jogador = normalizar(m.group("jogador"))
    if not jogador:
        return None
    return Leg(texto=texto, market_key=f"player:{{stat}}:{jogador}:{linha}",
               selecao=lado, suportado=True)


def _parse_basquete(texto: str) -> Leg | None:
    # "points" (Novibet, inglês) e "cestas" (Novibet, triplos: "total de
    # cestas de três pontos marcadas") somam-se às palavras-chave originais.
    if not re.search(r"pontos|points|rebotes|assist(ê|e)ncias|arremessos|cestas",
                     texto, re.I):
        return _parse_prop_sem_stat(texto)

    sufixo = _sufixo(texto)
    # Tira o "1º Tempo - " da frente pra não sujar o nome do jogador/equipe.
    corpo = re.sub(r"^\s*1[.°ºo]?\s*tempo\s*-\s*", "", texto, flags=re.I).strip()

    for padrao, stat in STATS_BASQUETE:
        m = re.search(padrao, corpo, re.I)
        if not m:
            continue
        antes = corpo[: m.start()].strip()
        depois = corpo[m.end():].strip()

        # "<Jogador> Total de pontos 18+"
        limite = _limite(depois)
        if limite is not None and antes:
            return Leg(texto=texto, market_key=f"player:{stat}:{normalizar(antes)}:{limite}",
                       selecao="over", suportado=True)

        linha = _linha(depois)
        if linha:
            lado, valor = linha
            # "<Jogador> Total de pontos Mais de 17.5"
            if antes:
                return Leg(texto=texto,
                           market_key=f"player:{stat}:{normalizar(antes)}:{valor}",
                           selecao=lado, suportado=True)
            # O que sobra entre a estatística e a linha é o nome da equipe:
            # "Total de pontos Chicago Sky (F) Mais de 89.5".
            resto = re.sub(r"(mais|menos|over|under)\s+de\s+[\d.,]+", "", depois,
                           flags=re.I).strip()
            if resto:
                if stat != "pontos":
                    return Leg(texto=texto, motivo=f"{stat} por equipe")
                return Leg(texto=texto, market_key=f"team_total{sufixo}:{{lado}}:{valor}",
                           selecao=lado, time_nome=resto, suportado=True)
            if stat != "pontos":
                return Leg(texto=texto, motivo=f"total de {stat} do jogo")
            return Leg(texto=texto, market_key=f"totals{sufixo}:{valor}",
                       selecao=lado, suportado=True)

        return Leg(texto=texto, motivo=f"{stat} sem linha reconhecível")

    return None


# --- futebol -----------------------------------------------------------------

def _parse_escanteios(texto: str) -> Leg | None:
    if not re.search(r"escanteios?|corner", texto, re.I):
        return None

    # "<Time> Para Ter o Maior Número de Escanteios" (SportingTech) — quem
    # vence os escanteios, fraseado como afirmação sobre um time só, sem ":"
    # e sem linha "mais de X" nenhuma. Tem que sair ANTES do `_linha(texto)`
    # abaixo, senão cai em "escanteios sem linha reconhecível" por engano —
    # aqui não tem linha porque o mercado é 1x2 de escanteios, não over/under.
    m = re.match(r"^(.+?)\s+para\s+ter\s+o\s+maior\s+n(ú|u)mero\s+de\s+escanteios\s*$",
                 texto, re.I)
    if m:
        return Leg(texto=texto, market_key=f"corners_h2h{_sufixo(texto)}",
                   time_nome=m.group(1).strip(), suportado=True)

    linha = _linha(texto)
    if not linha:
        return Leg(texto=texto, motivo="escanteios sem linha reconhecível")
    lado, valor = linha
    # "Velez Sarsfield Escanteios Mais de 4.5" é o total de uma equipe só — a
    # Pinnacle publica isso como team_total dentro do bloco de escanteios.
    m = re.match(r"^([A-ZÁÉÍÓÚÂÊÔÃÕÇ][\w\s.'-]{2,}?)\s+escanteios", texto.strip(), re.I)
    if m and not re.match(r"^(escanteios|mais|menos|total)", texto.strip(), re.I):
        return Leg(texto=texto, market_key=f"corners_team_total{_sufixo(texto)}:{{lado}}:{valor}",
                   selecao=lado, time_nome=m.group(1).strip(), suportado=True)
    return Leg(texto=texto, market_key=f"corners{_sufixo(texto)}:{valor}",
               selecao=lado, suportado=True)


def _parse_total_gols(texto: str) -> Leg | None:
    # "Gols Vitória (1º tempo): Mais de 0.5" — grafia da CasaDeAposta, com
    # "Gol(s)" NA FRENTE do nome do time (o oposto das duas grafias de total
    # por equipe tratadas mais abaixo, que têm o time antes de "total de
    # gols"). "2º tempo" já foi recusado mais cedo em `parse_leg`
    # (`SEGUNDO_TEMPO`) — só "" (jogo inteiro) e "_1t" chegam até aqui.
    m = re.match(r"^\s*gols?\s+(.+?)\s*(?:\([^)]*\))?\s*:\s*(mais|menos)\s+de\s+([\d.,]+)\s*$",
                 texto, re.I)
    if m and not _NAO_E_TIME.match(m.group(1).strip()):
        lado = "over" if m.group(2).lower() == "mais" else "under"
        valor = float(m.group(3).replace(",", "."))
        return Leg(texto=texto, market_key=f"team_total{_sufixo(texto)}:{{lado}}:{valor}",
                   selecao=lado, time_nome=m.group(1).strip(), suportado=True)

    # `\btotal\s*:` cobre a grafia da Altenar, que omite "de gols" quando o
    # mercado é o total da partida: "Total: Mais de 2.5", "1 total: Mais de
    # 1.5". Escanteios e cartões nunca chegam aqui — escanteios são tratados
    # pelo parser anterior e cartões saem antes, em SEM_COBERTURA.
    if not re.search(r"total\s+de\s+gols|total\s+gols|mais/menos.*gols"
                     r"|total\s+de\s+gols\s*-|\btotal\s*:", texto, re.I):
        return None
    linha = _linha(texto)
    if not linha:
        return Leg(texto=texto, motivo="total de gols sem linha reconhecível")
    lado, valor = linha

    # Grafia da Altenar para total de equipe: "1 total" = mandante, "2 total"
    # = visitante. Aqui o lado já é conhecido sem passar pelo nome do time,
    # então a chave sai resolvida em vez de com o placeholder `{lado}`.
    corpo_eq = _PREFIXO_PERIODO.sub("", texto.strip())
    m = re.match(r"^([12])\s+total\s*:", corpo_eq, re.I)
    if m:
        equipe = "home" if m.group(1) == "1" else "away"
        return Leg(texto=texto, market_key=f"team_total{_sufixo(texto)}:{equipe}:{valor}",
                   selecao=lado, suportado=True)

    # Total de UMA equipe, não do jogo. Duas grafias, porque cada plataforma
    # escreve de um jeito:
    #     "Flamengo (F) - Total de Gols Mais de 0.5"   Betano, com hífen
    #     "Mirassol total de gols: Mais de 1.5"        Altenar, SEM hífen
    #
    # ⚠️ Só a primeira era reconhecida. A segunda caía como total do JOGO e
    # comparava a odd do over 1.5 do Mirassol (3.70) com a justa do over 1.5 da
    # partida inteira (1.53) — edge falso de +141%, com stake no teto de 5un. É
    # o mesmo erro de classe do "1x2 e ambas equipes marcam": casar um mercado
    # mais restrito com a referência de um mercado mais amplo infla o edge.
    corpo = _PREFIXO_PERIODO.sub("", texto.strip())
    m = re.match(r"^(.+?)\s*[-–]?\s*total\s+(?:de\s+)?gols", corpo, re.I)
    if m and not _NAO_E_TIME.match(m.group(1).strip()):
        return Leg(texto=texto, market_key=f"team_total{_sufixo(texto)}:{{lado}}:{valor}",
                   selecao=lado, time_nome=m.group(1).strip(), suportado=True)
    return Leg(texto=texto, market_key=f"totals{_sufixo(texto)}:{valor}",
               selecao=lado, suportado=True)


ALTERNATIVAS = "||"

# Qualquer marcação de período no rótulo. Vários especiais da Pinnacle só
# existem para o jogo inteiro (ou para o 1º tempo, com chave própria); casar um
# rótulo de 2º tempo com o mercado do jogo todo repetiria o erro de comparar
# mercado restrito com referência ampla.
_TEM_PERIODO = re.compile(
    r"\d[.°ºo]?\s*tempo|primeiro\s+tempo|segundo\s+tempo|intervalo", re.IGNORECASE)


def _token_1x2(alvo: str) -> str:
    """Um lado do 1X2 vira um pedaço de rótulo da Pinnacle.

    `{home}`/`{away}`/`{time:Nome}` são resolvidos em `fair_odds`, depois do
    match — é lá que se sabe quem é o mandante e qual o nome que a Pinnacle usa.
    """
    alvo = alvo.strip()
    # A Novibet às vezes escreve o "X" do 1X2 como Χ grego (U+03A7) em vez de
    # Latin X — achado real: "Chance Dupla: Χ2".
    if re.fullmatch(r"empate|draw|x|Χ", alvo, re.I):
        return "Draw"
    if alvo == "1":
        return "{home}"
    if alvo == "2":
        return "{away}"
    return f"{{time:{alvo}}}"


def _sufixo_especial(texto: str) -> str | None:
    """"" (jogo inteiro), "_1t", ou None quando o período não tem mercado.

    Diferente de `_sufixo`, que devolve "" para qualquer período desconhecido:
    aqui "2º tempo" precisa virar recusa, não virar jogo inteiro. `intervalo`
    fica de fora do ramo de 1º tempo de propósito — quem tem "intervalo" no
    rótulo é o HT/FT, tratado antes de chegar aqui.
    """
    if not _TEM_PERIODO.search(texto):
        return ""
    if re.search(r"1[.°ºo]?\s*tempo|primeiro\s+tempo", texto, re.I):
        return "_1t"
    return None


def _parse_especiais(texto: str) -> Leg | None:
    """Mercados que a Pinnacle publica INTEIROS, com preço próprio.

    Vale a pena separar do resto porque estes são exatamente os rótulos que a
    guarda `COMBINADOS` recusa por precaução — e a precaução existe pra impedir
    casar um PEDAÇO do mercado com a referência de um mercado mais amplo. Aqui
    não há pedaço: `Correct Score`, `Half-Time/Full-Time`, `Double Chance` e
    `Both Teams To Score/Winner` são mercados próprios na Pinnacle, com preço
    próprio, então o casamento é exato. Por isso este parser roda ANTES da
    guarda.

    Só aparecem em ~1 de cada 12 jogos (medido em 2026-08-04), quase sempre
    liga grande. Quando não existem, a perna morre em "sem odd na Pinnacle" —
    que é o comportamento certo enquanto o modelo de placar não entra.
    """
    # HT/FT primeiro: o rótulo contém "intervalo", que qualquer leitura de
    # período interpretaria como 1º tempo. Aqui o mercado abrange os dois.
    # "Intervalo/final do jogo: 1/1", "...: Empate/empate" (Altenar);
    # "Intervalo/Final : Ajax / Ajax" (Novibet, sem "do jogo", espaço antes
    # do ":"); "Intervalo/Final - Cruzeiro/Cruzeiro" (SportingTech, "-" em
    # vez de ":"). "do jogo" e o separador ":"/"-" ficam opcionais/flexíveis
    # pra cobrir as três grafias reais. Não pode casar "...resultado exato:
    # 0:0 2:0" (HT+FT combinado, sem equivalente na Pinnacle) — como não tem
    # "/" logo após "do jogo"/":"/"-", a exigência do primeiro "/" barra isso.
    m = re.search(r"intervalo\s*/\s*final(?:\s+do\s+jogo)?\s*[:\-]\s*(.+?)\s*/\s*(.+?)\s*$",
                  texto, re.I)
    if m:
        return Leg(texto=texto, market_key="ht_ft",
                   selecao=f"{_token_1x2(m.group(1))} - {_token_1x2(m.group(2))}",
                   suportado=True)

    sufixo = _sufixo_especial(texto)
    if sufixo is None:
        # Período sem mercado correspondente (tipicamente 2º tempo). Deixa
        # seguir pra guarda de combinados / "não reconhecido".
        return None

    # "Resultado Correto: 1:0" e "1º tempo - resultado exato: 0:1".
    # O `:` entre os números separa do tênis, que escreve "Resultado Correto
    # 1 - 2" (placar de sets) e já foi tratado antes. O `\s*$` impede casar o
    # segundo placar de "Intervalo/final do jogo resultado exato: 0:0 2:0",
    # que é HT E FT juntos — mercado que a Pinnacle não publica.
    m = re.search(r"resultado\s+(?:correto|exato)\s*:\s*(\d+)\s*:\s*(\d+)\s*$",
                  texto, re.I)
    if m:
        return Leg(texto=texto, market_key=f"correct_score{sufixo}",
                   selecao=f"{{home}} {m.group(1)}, {{away}} {m.group(2)}",
                   suportado=True)

    # "Chance dupla: Empate ou Santos". A Pinnacle fixa a ordem (mandante
    # primeiro, empate no meio) e aqui ainda não se sabe quem é o mandante —
    # então as duas ordens vão como alternativas. Só uma delas existe no
    # mercado, então não há ambiguidade a resolver.
    # "chance dupla" e "dupla chance" são a mesma coisa — a CasaDeAposta usa a
    # ordem invertida e a perna caía em "mercado não reconhecido". A
    # CasaDeAposta também batiza esse mesmo mercado de "Resultado: <Time> ou
    # empate" (achado real: "Resultado: Juventude ou empate") — mesmo texto
    # com "ou", só que sem a palavra "chance". Tratado aqui, ANTES de
    # `_parse_resultado`, pra não virar um h2h de "Juventude ou empate" como
    # se fosse o nome de um time só (fuzzy match parcial em "Juventude").
    m = re.search(r"(?:chance\s+dupla|dupla\s+chance|resultado)\s*:\s*(.+?)\s+ou\s+(.+?)\s*$",
                  texto, re.I)
    if m:
        a, b = _token_1x2(m.group(1)), _token_1x2(m.group(2))
        return Leg(texto=texto, market_key=f"double_chance{sufixo}",
                   selecao=f"{a} Or {b}{ALTERNATIVAS}{b} Or {a}", suportado=True)

    # "Chance Dupla X2" — a Betano escreve em código, sem os nomes. Mesmo
    # mercado, mesma resolução: cada caractere é um lado do 1X2.
    # A Novibet escreve o "X" às vezes como Χ grego (U+03A7) em vez de Latin X
    # — achado real: "Chance Dupla: Χ2", "Chance Dupla: 1X" (as duas grafias
    # convivem na mesma casa). `[12xΧχ]` aceita as duas.
    m = re.search(r"(?:chance\s+dupla|dupla\s+chance)\s*:?\s*([12xΧχ])\s*([12xΧχ])\s*$",
                  texto, re.I)
    if m and m.group(1).lower() != m.group(2).lower():
        a, b = _token_1x2(m.group(1)), _token_1x2(m.group(2))
        return Leg(texto=texto, market_key=f"double_chance{sufixo}",
                   selecao=f"{a} Or {b}{ALTERNATIVAS}{b} Or {a}", suportado=True)

    # "1x2 e ambas equipes marcam: Levski Sófia e sim".
    # A Pinnacle publica `Both Teams To Score/Winner` só para o jogo inteiro —
    # não há versão por tempo. "2º tempo - 1x2 e ambas equipes marcam" casado
    # com o mercado do jogo todo seria de novo restrito-contra-amplo.
    m = re.search(r"1x2\s+e\s+ambas\s+(?:as\s+)?equipes\s+marcam\s*:\s*"
                  r"(.+?)\s+e\s+(sim|n(?:ã|a)o)\s*$", texto, re.I)
    if m and sufixo == "":
        btts = "Yes" if m.group(2).lower() == "sim" else "No"
        return Leg(texto=texto, market_key="btts_vencedor",
                   selecao=f"{btts} & {_token_1x2(m.group(1))}", suportado=True)

    # "1º tempo - gols exatos: 1" / "1º tempo - Internacional gols exatos: 1".
    #
    # ⚠️ Achado real (2026-08, log de produção): o `re.search` antigo não era
    # ancorado à esquerda, então "Internacional gols exatos: 1" casava igual a
    # "gols exatos: 1" e virava `exact_goals_1t` — o TOTAL de gols dos DOIS
    # times no 1ºT, não só do Internacional. O evento amplo é mais provável
    # que o restrito, então a justa saiu ABAIXO da crua (2.79 contra 3.82) —
    # edge fantasma de +23.6%. Exatamente a classe "mercado restrito casado
    # com referência ampla" documentada na skill `value-bet-methodology`.
    #
    # A Pinnacle publica o mercado restrito como "{Time} Goals" / "{Time}
    # Goals 1st Half" (sondado ao vivo) — mapeado em
    # `pinnacle._mapear_special`/`_resolver_team_goals` pra
    # `team_exact_goals{sufixo}:{lado}`. `_PREFIXO_PERIODO` sai da frente
    # antes de procurar o nome, senão "1º tempo -" viraria o "time".
    corpo_gols = _PREFIXO_PERIODO.sub("", texto.strip())
    m = re.search(r"^(.*?)\s*\bgols\s+exatos\s*:\s*(\d+)\s*$", corpo_gols, re.I)
    if m:
        prefixo, digito = m.group(1).strip(), m.group(2)
        if prefixo and not _NAO_E_TIME.match(prefixo):
            return Leg(texto=texto, market_key=f"team_exact_goals{sufixo}:{{lado}}",
                       selecao=digito, time_nome=prefixo, suportado=True)
        return Leg(texto=texto, market_key=f"exact_goals{sufixo}",
                   selecao=digito, suportado=True)

    return None


def _parse_btts(texto: str) -> Leg | None:
    # "ambos os times marcarem" é a grafia da EsportesDaSorte pro mesmo
    # mercado que a Altenar/Betano chamam de "ambas equipes marcam" — sem
    # ambiguidade de escopo (BTTS não existe "de um time só"), então
    # ampliar aqui é seguro, ao contrário do total de gols/escanteios.
    if not re.search(
        r"amb[ao]s\s+(?:[ao]s\s+)?(?:equipes?|times?)\s+marca(?:m|rem)"
        r"|ambas\s+marcam|\bbtts\b",
        texto, re.I,
    ):
        return None
    negativo = re.search(r"\b(n(ã|a)o|no)\s*$", texto.strip(), re.I)
    return Leg(texto=texto, market_key=f"btts{_sufixo(texto)}",
               selecao="No" if negativo else "Yes", suportado=True)


QUALIFICADOR_RESTRITO = re.compile(
    r"\b(escanteios?|corners?|remates?|chutes?|faltas?|desarmes?|impedimentos?)\b",
    re.IGNORECASE)

_1X2_QUALQUER = re.compile(
    r"\b1x2\b|vencedor\b|resultado\s+final|chance\s+dupla|dupla\s+chance", re.IGNORECASE)


def _parse_1x2_restrito(texto: str) -> Leg | None:
    """"Escanteios 1x2: Fortaleza" — quem vence os ESCANTEIOS, não o jogo.

    Roda antes de todo mundo porque `_parse_resultado` casa o `\\b1x2\\b` e
    devolvia `h2h`: a odd de um sub-mercado comparada contra a odd justa do
    resultado da partida. É o mesmo erro do combinado do Vélez documentado em
    `COMBINADOS` — mercado restrito contra referência ampla —, e aqui ele era
    pior, porque nada no rótulo denunciava.

    Escanteios seguem para `corners_h2h`, que `fair_odds` deriva do handicap.
    Remates, chutes e faltas não têm referência nenhuma na Pinnacle.
    """
    q = QUALIFICADOR_RESTRITO.search(texto)
    if not q or not _1X2_QUALQUER.search(texto):
        return None

    escopo = q.group(1).lower()
    if not escopo.startswith(("escanteio", "corner")):
        return Leg(texto=texto, motivo=f"1x2 de {escopo} (sem referência)")

    # A chance dupla de escanteios sairia da mesma aritmética, mas cada
    # derivação nova é mais uma superfície onde edge falso pode nascer — e
    # esta não apareceu nenhuma vez no banco. Recusa explícita.
    if re.search(r"chance\s+dupla|dupla\s+chance", texto, re.I):
        return Leg(texto=texto, motivo="chance dupla de escanteios")

    sufixo = _sufixo(texto)
    _, _, alvo = texto.rpartition(":")
    alvo = alvo.strip()
    if not alvo:
        return Leg(texto=texto, motivo="1x2 de escanteios sem lado reconhecível")
    if re.fullmatch(r"empate|draw|x", alvo, re.I):
        return Leg(texto=texto, market_key=f"corners_h2h{sufixo}",
                   selecao="draw", suportado=True)
    if alvo in ("1", "2"):
        return Leg(texto=texto, market_key=f"corners_h2h{sufixo}",
                   selecao="home" if alvo == "1" else "away", suportado=True)
    return Leg(texto=texto, market_key=f"corners_h2h{sufixo}",
               time_nome=alvo, suportado=True)


# "Handicap: Cruzeiro (-1.5)" / "Handicap Asiático: Mirassol (+0.75)" /
# "1º tempo - handicap: Toronto Tempo (F) (+3.5)" / "Handicap (incluindo
# Prorrogação): Toronto Tempo (F) (+13.5)" — achados reais no banco (Esportiva
# Bet, EstrelaBet, BateuBet, GingaBet). O mesmo formato "time + linha entre
# parênteses" cobre futebol E basquete: quem decide o esporte é o evento
# casado, não o parser, e o `market_key` "spread" é igual pros dois em
# `pinnacle.py`. Não casa com "handicap de games/sets/jogos" (tênis, já
# resolvido em `_parse_tenis`) porque exige `:` logo após "handicap".
_HANDICAP_TIME = re.compile(
    r"handicap(?:\s+asi[áa]tico)?(?:\s*\(incluindo\s+prorroga[çc][ãa]o\))?\s*:\s*"
    r"(?P<time>.+?)\s*\(\s*(?P<linha>[+-]?\d+(?:[.,]\d+)?)\s*\)\s*$",
    re.IGNORECASE)


def _parse_handicap_futebol(texto: str) -> Leg | None:
    """Handicap asiático de time (futebol ou basquete).

    A Pinnacle publica (é o mercado mais líquido dela em futebol, e também
    existe em basquete). `handicap_linha` guarda o sinal como a casa escreveu
    pro time citado; `fair_odds._lado_do_time` resolve mandante/visitante e
    inverte o sinal quando o time citado é o visitante — mesmo mecanismo já
    usado pro handicap de games do tênis.
    """
    if re.search(r"escanteio|corner", texto, re.I):
        # Handicap de escanteios seria outro mercado (`corners_spread`); não
        # apareceu nenhuma vez no banco, mas casar aqui por engano injetaria
        # o handicap do JOGO como referência — o mesmo erro de classe do
        # Mirassol (mercado restrito x referência ampla). Melhor recusar.
        return None
    m = _HANDICAP_TIME.search(texto)
    if m:
        linha = float(m.group("linha").replace(",", "."))
        return Leg(texto=texto, market_key=f"spread{_sufixo(texto)}",
                   time_nome=m.group("time").strip(), handicap_linha=linha,
                   suportado=True)
    if re.search(r"handicap", texto, re.I):
        return Leg(texto=texto, motivo="handicap de futebol (texto não calibrado)")
    return None


def _lado_1x2(alvo: str, texto: str, sufixo: str) -> Leg:
    """"1"/"2"/"X"/"Empate"/"<Time>" -> perna de h2h."""
    alvo = alvo.strip()
    if re.fullmatch(r"empate|draw|x", alvo, re.I):
        return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
    if alvo in ("1", "2"):
        return Leg(texto=texto, market_key=f"h2h{sufixo}",
                   selecao="home" if alvo == "1" else "away", suportado=True)
    return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)


def _parse_resultado(texto: str) -> Leg | None:
    # "resultado\s*:" cobre a grafia bare da CasaDeAposta ("Resultado: Santos",
    # sem "final"/"do 1º tempo"/"intervalo" no meio) e "resultado\s+ao\s+intervalo"
    # cobre a variante da Novibet ("Resultado ao Intervalo: Sheriff Tiraspol").
    # "para\s+ganhar" cobre a grafia da SportingTech ("Grêmio Para Ganhar").
    if not re.search(r"resultado\s*:|resultado\s+(final|do\s+1|ao\s+intervalo|intervalo)"
                     r"|vencedor\b|\b1x2\b|para\s+ganhar\b", texto, re.I):
        return None
    sufixo = _sufixo(texto)

    # "<Time> Para Ganhar" (SportingTech) — vitória simples do time. NÃO pode
    # casar "<Time> Para Ganhar Um Dos Tempos" — vencer UM dos tempos é uma
    # probabilidade conjunta que a Pinnacle não publica (mesma família do
    # "para marcar em ambos os tempos", sem mercado equivalente); o
    # ancoramento em `$` logo depois de "ganhar" garante que sobra texto ali
    # não casa.
    m = re.match(r"^(.+?)\s+para\s+ganhar\s*$", texto, re.I)
    if m:
        return Leg(texto=texto, market_key=f"h2h{sufixo}",
                   time_nome=m.group(1).strip(), suportado=True)

    # Grafia da Altenar: "1x2: Levski Sófia", "Vencedor do encontro: 1",
    # "1º tempo - 1x2: Empate".
    m = re.search(r"(?:\b1x2\b|vencedor(?:\s+do\s+(?:encontro|jogo|jogo\s+todo))?)"
                  r"\s*:\s*(.+)$", texto, re.I)
    if m:
        return _lado_1x2(m.group(1), texto, sufixo)

    # "Vencedor (1º tempo): Grêmio" (CasaDeAposta) / "Vencedor da Partida:
    # Alex Michelsen" (Novibet, tênis) / "Vencedor do Jogo: CHI Sky" (Novibet,
    # basquete) — qualificador entre "Vencedor" e o ":" que a regex acima não
    # prevê. Sem isto, o fallback genérico mais frouxo capturava TUDO depois
    # de "Vencedor " — inclusive o qualificador — e o `time_nome` virava algo
    # como "(1º tempo): Grêmio" ou "da Partida: Alex Michelsen", que nunca
    # casa com time/jogador nenhum. Mesmo erro silencioso já documentado
    # acima pra "Vencedor do encontro", só que noutra grafia. `rpartition`
    # pega o que vem depois do ÚLTIMO ":", igual ao `_parse_1x2_restrito`.
    if re.search(r"vencedor\b.*:", texto, re.I):
        _, _, alvo = texto.rpartition(":")
        alvo = alvo.strip()
        if alvo:
            if re.fullmatch(r"empate|draw|x", alvo, re.I):
                return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
            return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    # "Resultado ao Intervalo: Sheriff Tiraspol" (Novibet) — vencedor do 1º
    # tempo, grafia diferente da Altenar ("Resultado do 1º Tempo <Time>",
    # tratado mais abaixo). `_sufixo` já dá "_1t" pra qualquer texto com
    # "intervalo".
    m = re.search(r"resultado\s+ao\s+intervalo\s*:\s*(.+)$", texto, re.I)
    if m:
        alvo = m.group(1).strip()
        if re.fullmatch(r"empate|draw|x", alvo, re.I):
            return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
        return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    # Formato do scraper para o mercado MR12: "Resultado Final: <lado>"
    m = re.search(r"resultado\s+final\s*:\s*(.+)$", texto, re.I)
    if m:
        alvo = m.group(1).strip()
        if re.fullmatch(r"empate|draw|x", alvo, re.I):
            return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
        return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    # Formato do smart-picks: "Resultado Final 1" / "2" / "X"
    m = re.search(r"resultado\s+(?:final|do\s+1[.°ºo]?\s*tempo)\s+([12x])\b", texto, re.I)
    if m:
        lado = {"1": "home", "2": "away", "x": "draw"}[m.group(1).lower()]
        return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao=lado, suportado=True)

    if re.search(r"resultado\s+(final|do\s+1[.°ºo]?\s*tempo)\s+(empate|draw)", texto, re.I):
        return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)

    # "Resultado do 1° Tempo <Time>"
    m = re.search(r"resultado\s+do\s+1[.°ºo]?\s*tempo\s+(.+)$", texto, re.I)
    if m:
        alvo = m.group(1).strip()
        if re.fullmatch(r"empate|draw|x", alvo, re.I):
            return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
        return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    # Bare "Resultado: <Time>" (CasaDeAposta) — "ou empate" já foi
    # interceptado em `_parse_especiais` (dupla chance) antes de chegar aqui.
    m = re.search(r"resultado\s*:\s*(.+)$", texto, re.I)
    if m:
        alvo = m.group(1).strip()
        if re.fullmatch(r"empate|draw|x", alvo, re.I):
            return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
        return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    # "Vencedor <Nome>", sem ":" nenhum — grafia mais simples da Betano.
    # Último recurso: as variantes com qualificador entre "Vencedor" e ":"
    # já foram tratadas acima (com prioridade, porque têm ":" pra ancorar o
    # `rpartition`); esta é só pra quando não existe ":" nenhum no texto.
    m = re.search(r"vencedor\s+(.+)$", texto, re.I)
    if m:
        alvo = m.group(1).strip()
        if re.fullmatch(r"empate|draw|x", alvo, re.I):
            return Leg(texto=texto, market_key=f"h2h{sufixo}", selecao="draw", suportado=True)
        return Leg(texto=texto, market_key=f"h2h{sufixo}", time_nome=alvo, suportado=True)

    return None


def parse_leg(texto: str) -> Leg:
    """Mapeia uma perna isolada pra um mercado da Pinnacle.

    Fino wrapper em cima de `_parse_leg_pinnacle`: quando a perna sai sem
    cobertura, anexa `consenso_chave` — sempre DEPOIS do veredito
    `suportado=False`, nunca antes, e nunca contaminando `market_key`.
    """
    texto = texto.strip()
    leg = _parse_leg_pinnacle(texto)
    if not leg.suportado:
        leg.consenso_chave = chave_consenso(texto)
    return leg


def _parse_leg_pinnacle(texto: str) -> Leg:
    """Mapeia uma perna isolada pra um mercado da Pinnacle.

    A ordem dos parsers importa: os de tênis e basquete rodam antes do de
    futebol porque "Vencedor do Set (Set 1) X" casaria com o `vencedor\\b` do
    parser de resultado e viraria um 1X2 de futebol.
    """

    motivo = _sem_cobertura(texto)
    if motivo:
        return Leg(texto=texto, motivo=motivo)

    # 2º tempo ANTES de tudo — a Pinnacle só publica período 0 (jogo/partida
    # inteira) e 1 (1º tempo/intervalo) em `pinnacle._mapear_futebol` e
    # `_mapear_basquete` (`periodo not in (0, 1): return None`); não existe
    # bloco de 2º tempo pra referenciar. `_sufixo()` só reconhece o
    # qualificador de 1º tempo (`PRIMEIRO_TEMPO`) — sem esta guarda, um texto
    # de 2º tempo cai com sufixo "" e casa contra o mercado do JOGO INTEIRO
    # por engano. Achado ao vivo em 2026-08-05 (revalidação de adapters):
    # "2º tempo - ambas equipes marcam: Sim" (EstrelaBet) → `btts`, "2º tempo
    # - total: Mais de 1.5" (BetPix365) → `totals:1.5`, "Vencedor (2º tempo):
    # Mirassol" (CasaDeAposta) → `h2h` — todos comparando o mercado do 2º
    # tempo contra a referência do jogo inteiro, a mesma classe do falso
    # +141,8% do Mirassol (mercado restrito x referência ampla).
    if SEGUNDO_TEMPO.search(texto):
        return Leg(texto=texto,
                   motivo="2º tempo (sem mercado equivalente na Pinnacle)")

    # 1X2 de sub-mercado ANTES de tudo: "Escanteios 1x2: Fortaleza" casa com o
    # `\b1x2\b` de `_parse_resultado` e virava o 1X2 do JOGO — restrito contra
    # referência ampla, o erro que `COMBINADOS` documenta.
    leg = _parse_1x2_restrito(texto)
    if leg is not None:
        return leg

    # Tênis e basquete primeiro, pelo mesmo motivo de sempre: "Vencedor do Set
    # (Set 1) X" casaria com o `vencedor\b` do futebol.
    for parser in (_parse_tenis, _parse_basquete):
        leg = parser(texto)
        if leg is not None:
            return leg

    # Especiais ANTES da guarda de combinados. A guarda existe pra impedir
    # casar um PEDAÇO do rótulo com um mercado mais amplo; nestes não há
    # pedaço, porque a Pinnacle publica o mercado inteiro com preço próprio.
    leg = _parse_especiais(texto)
    if leg is not None:
        return leg

    # O que sobrou de combinado não tem referência única — casar só um pedaço
    # produz edge inventado (ver a nota em COMBINADOS).
    if COMBINADOS.search(texto):
        return Leg(texto=texto, motivo="mercado combinado num rótulo só")

    for parser in (_parse_handicap_futebol,
                   _parse_resultado, _parse_escanteios, _parse_total_gols, _parse_btts):
        leg = parser(texto)
        if leg is not None:
            return leg

    return Leg(texto=texto, motivo="mercado não reconhecido")


# "[Time A - Time B] Total de Gols: Mais de 2,5" -> jogo dono da perna.
# Só a Novibet emite isso hoje (`novibet.py`, combo multi-jogo), e só quando
# o ticket mistura jogos diferentes — perna de jogo único nunca tem prefixo.
_PREFIXO_JOGO = re.compile(r"^\[([^\]]+)\]\s*")


def parse_mercado(mercado: str) -> list[Leg]:
    """Quebra o texto completo em pernas e mapeia cada uma."""
    legs = []
    for pedaco in mercado.split(SEPARADOR_PERNAS):
        pedaco = pedaco.strip()
        if not pedaco:
            continue
        m = _PREFIXO_JOGO.match(pedaco)
        evento_texto = None
        if m:
            evento_texto = m.group(1).strip()
            pedaco = pedaco[m.end():].strip()
        leg = parse_leg(pedaco)
        if evento_texto:
            leg.evento_texto = evento_texto
        legs.append(leg)
    return legs


def tipo_mercado(legs: list[Leg]) -> str:
    return "simples" if len(legs) == 1 else "combo"
