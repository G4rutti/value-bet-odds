"""Cálculo do edge e decisão de value — e a confiança que acompanha o número.

A tabela de confiança (skill `value-bet-methodology`) cruza três eixos pra
decidir quanto edge exigir e quanto confiar no resultado:

    tipo_mercado   "simples" | "combo"
    fonte_odd      "pinnacle" | "consenso" | "modelo"
    classe_mercado "geral" | "prop"   (cartão, chute ao gol, artilheiro, handicap)

`fonte_odd` manda: se qualquer perna da oferta caiu pra consenso ou modelo, é
o erro dela que domina o threshold inteiro, combo ou não (ver `threshold_para`
e a nota em `fair_odds.calcular_odd_justa`). `classe_mercado` só entra em jogo
DENTRO do consenso — ela não sobrepõe a Pinnacle: um handicap com preço direto
da Pinnacle continua "simples com Pinnacle" (`alta`), mesmo sendo handicap.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import config

# Classes de mercado que só existem por consenso e nunca (ou quase nunca) têm
# preço direto da Pinnacle — a linha mais fraca da tabela.
#
# Cartão, chute ao gol e artilheiro (gol de jogador) são absolutos: checado ao
# vivo (`market_parser.SEM_COBERTURA`), a Pinnacle não publica NENHUM dos três
# em futebol. Uma perna com esse `motivo` só chega até aqui via
# `ProvedorConsenso` — nunca por preço sharp.
#
# Handicap é diferente: normalmente É a Pinnacle (é o mercado mais líquido
# dela, ver `market_parser._parse_handicap_futebol`) — só cai pra consenso
# quando a linha específica não está publicada naquele jogo. Por isso ele
# entra na lista de classes "prop" (o piso de casas/threshold mais alto), mas
# só é EXERCIDO quando `fonte_odd == "consenso"`; com Pinnacle ele segue o
# caminho normal de mercado simples/combo, igual a qualquer outro.
#
# ⚠️ Prop de jogador de BASQUETE (`player:pontos:...`, `player:rebotes:...`)
# NÃO entra aqui. Diferente do artilheiro de futebol, a Pinnacle publica esses
# props diretamente (`pinnacle.py`, campo `units`) — eles seguem o caminho
# normal de "simples com Pinnacle" como qualquer h2h ou total. Ver o relatório
# do agente pra mais detalhe: a tabela do dono fala em "gol de jogador"
# (artilheiro de futebol), não em prop estatístico de basquete.
_MOTIVOS_PROP = frozenset({
    "cartões",
    "chutes no gol",
    "artilheiro (prop de jogador)",
})


def classe_mercado_da_perna(leg) -> str:
    """"prop" pra cartão/chute ao gol/artilheiro/handicap; "geral" pro resto.

    Handicap é reconhecido pelo `market_key` (`spread`, `games_spread`,
    `sets_spread` — todos contêm a palavra "spread", como o parser já nomeia).
    Cartão/chute/artilheiro são reconhecidos pelo `motivo` que o parser já
    atribuiu à perna sem cobertura — não há regex nova aqui, só leitura do que
    `market_parser` já decidiu.
    """
    if leg.market_key and "spread" in leg.market_key:
        return "prop"
    if not leg.suportado and leg.motivo in _MOTIVOS_PROP:
        return "prop"
    return "geral"


def classe_mercado_da_oferta(legs) -> str:
    """"prop" se QUALQUER perna for prop — a perna pior manda, igual à fonte."""
    return "prop" if any(classe_mercado_da_perna(l) == "prop" for l in legs) else "geral"


@dataclass
class ValueResult:
    odd_boost: float
    odd_justa: float
    edge_pct: float
    is_value: bool
    tipo_mercado: str
    threshold_usado: float
    confianca: str = "alta"
    flag: str | None = None

    def __str__(self) -> str:
        marca = "🔥 VALUE" if self.is_value else "sem edge suficiente"
        if self.flag:
            marca = f"⚠️ {self.flag}"
        return (f"odd boost: {self.odd_boost:.2f} | odd justa: {self.odd_justa:.2f} "
                f"| edge: {self.edge_pct:+.1f}% | confiança: {self.confianca} | {marca}")


def threshold_para(tipo_mercado: str,
                   edge_min_simples: float | None = None,
                   edge_min_combo: float | None = None,
                   fonte_odd: str = "pinnacle",
                   classe_mercado: str = "geral") -> float:
    """Quanto edge é preciso, conforme a qualidade da odd justa.

    Do mais confiável pro menos:

    - mercado simples com preço observado — só erro de de-vig e de matching;
    - combo com Pinnacle em todas as pernas — as pernas são multiplicadas
      assumindo independência, o que deixa a justa otimista (ver
      fair_odds.calcular_odd_justa);
    - consenso — preço observado, mas em casa mole. A Pinnacle é referência
      porque tem dinheiro sharp corrigindo o preço; um consenso de casas moles
      pode estar errado junto, e aí o edge mede desvio do rebanho;
      - dentro do consenso, cartão/chute ao gol/artilheiro/handicap
        (`classe_mercado == "prop"`) pedem o piso mais alto: são os mercados
        mais finos, com o consenso mais raso;
      - combo com alguma perna em consenso soma os dois riscos (produto
        otimista + casa mole) e usa o mesmo piso alto do "prop";
    - modelo — a probabilidade não foi observada em lugar nenhum, foi derivada
      do 1X2 + over/under. Erro de modelo em cima dos outros dois.

    Fonte vence tipo de mercado E classe de mercado: se qualquer perna foi
    derivada, é `EDGE_MIN_MODELO` que manda, ponto. Só dentro de
    `fonte_odd == "consenso"` é que `classe_mercado` entra na conta — um
    handicap com preço direto da Pinnacle nunca passa por aqui como "prop".
    """
    if fonte_odd == "modelo":
        return config.EDGE_MIN_MODELO
    if fonte_odd == "consenso":
        if classe_mercado == "prop":
            return config.EDGE_MIN_PROP
        if tipo_mercado == "combo":
            return config.EDGE_MIN_COMBO_CONSENSO
        return config.EDGE_MIN_CONSENSO
    if tipo_mercado == "combo":
        return config.EDGE_MIN_COMBO if edge_min_combo is None else edge_min_combo
    return config.EDGE_MIN_SIMPLES if edge_min_simples is None else edge_min_simples


def classificar_confianca(tipo_mercado: str, fonte_odd: str = "pinnacle",
                          classe_mercado: str = "geral",
                          n_casas_consenso: int = 0,
                          n_precos_consenso: int = 0) -> str:
    """"alta" / "média-alta" / "média" / "baixa" / "insuficiente".

    `n_casas_consenso` é uma segunda checagem, não a principal — o gate de
    mínimo de casas já vive em `consenso.calcular` (que devolve `None` e mata
    a oferta antes de chegar aqui quando está abaixo do mínimo). Esta checagem
    cobre o caso em que ela ainda escapa: num COMBO, a fonte/classe da oferta
    é a da PIOR perna, mas `n_casas_consenso` agregado é o MÍNIMO de casas
    entre as pernas — uma perna "geral" com exatamente 5 casas (mínimo dela)
    ao lado de uma perna "prop" (mínimo 6) resulta em `n_casas_consenso=5` e
    `classe_mercado="prop"`, que é insuficiente para prop mesmo sem nenhum
    gate individual ter falhado. Ver o relatório do agente pra mais contexto.

    `n_precos_consenso` é uma TERCEIRA checagem, ortogonal às duas acima e
    sobre outra dimensão: qualidade, não quantidade. Casas Altenar são um
    feed só — ~90% das seleções saem com preço idêntico entre elas, então
    `n_casas_consenso` alto pode esconder um único preço rebanhado (ver
    `consenso.calcular`, que já calcula `n_precos` por isso). O gate
    PRINCIPAL de quantidade de casas continua lá; isto aqui rebaixa a
    confiança quando o consenso, apesar de aprovado em quantidade, não tem
    diversidade de preço nenhuma.
    """
    if fonte_odd == "consenso":
        minimo = (config.CONSENSO_MIN_CASAS_PROP if classe_mercado == "prop"
                  else config.CONSENSO_MIN_CASAS)
        if n_casas_consenso and n_casas_consenso < minimo:
            return "insuficiente"
        if classe_mercado == "prop":
            if 0 < n_precos_consenso <= 1:
                return "insuficiente"
            return "baixa"
        resultado = "baixa" if tipo_mercado == "combo" else "média"
        if resultado == "média" and n_precos_consenso > 0 and n_precos_consenso <= 1:
            return "baixa"
        return resultado
    if fonte_odd == "modelo":
        # A tabela do dono não lista "modelo" como fonte — é uma extensão
        # deste agente. Modelo nunca foi preço observado em lugar nenhum (é
        # Poisson/Dixon-Coles derivado do 1X2 + over/under), o que o coloca
        # abaixo até do consenso de casa mole. `baixa` é a classificação mais
        # conservadora disponível na tabela, e a trava `MODELO_ALERTA_ATIVO`
        # em `pipeline.py` já impede qualquer alerta automático de qualquer
        # forma — este rótulo só importa pro log/diagnóstico.
        return "baixa"
    # pinnacle
    return "média-alta" if tipo_mercado == "combo" else "alta"


def calcular_edge(odd_boost: float, odd_justa: float) -> float:
    """Quanto a odd turbinada supera a justa, em %."""
    if odd_justa <= 0:
        raise ValueError("odd_justa precisa ser positiva")
    return (odd_boost / odd_justa - 1.0) * 100.0


def avaliar_value(odd_boost: float, odd_justa: float, tipo_mercado: str,
                  edge_min_simples: float | None = None,
                  edge_min_combo: float | None = None,
                  fonte_odd: str = "pinnacle",
                  classe_mercado: str = "geral",
                  n_casas_consenso: int = 0,
                  n_precos_consenso: int = 0) -> ValueResult:
    edge = calcular_edge(odd_boost, odd_justa)
    limite = threshold_para(tipo_mercado, edge_min_simples, edge_min_combo,
                            fonte_odd, classe_mercado)
    confianca = classificar_confianca(tipo_mercado, fonte_odd, classe_mercado,
                                      n_casas_consenso, n_precos_consenso)

    # Teto de sanidade: edge alto demais é sintoma de bug de casamento antes
    # de ser oportunidade (ver EDGE_TETO_SANIDADE em config.py — o falso
    # +141% do Mirassol e o falso +23,6% do gols-exatos do Internacional são
    # os dois exemplos reais que motivaram este número). Nunca entra no Kelly
    # automático, não importa a confiança do resto da oferta.
    flag = "possivel_erro_matching" if edge > config.EDGE_TETO_SANIDADE else None

    # "insuficiente" não é zero, é "não sei": não é a mesma coisa que edge
    # abaixo do threshold (que sabemos que não é value), é ausência de base
    # pra afirmar qualquer coisa. Por isso força `is_value=False` aqui em vez
    # de deixar a comparação normal `edge >= limite` decidir — um edge de 80%
    # passaria de qualquer threshold desta tabela.
    is_value = confianca != "insuficiente" and flag is None and edge >= limite

    return ValueResult(
        odd_boost=odd_boost,
        odd_justa=odd_justa,
        edge_pct=round(edge, 2),
        is_value=is_value,
        tipo_mercado=tipo_mercado,
        threshold_usado=limite,
        confianca=confianca,
        flag=flag,
    )
