"""Quanto apostar, em unidades — critério de Kelly fracionado.

Kelly, em português claro
─────────────────────────
Dada uma odd `b+1` e a probabilidade real `p` de ganhar, a fração da banca que
maximiza o crescimento no longo prazo é:

    f* = (b·p − q) / b        com q = 1 − p

`p` sai da odd justa já sem vig: `p = 1 / odd_justa`. Repare que `f*` é positivo
exatamente quando há edge, e cresce com ele.

Por que NÃO usar Kelly cheio
────────────────────────────
Kelly cheio só é ótimo se `p` estiver **certo**. O nosso é estimado, e cada
etapa injeta erro: o matching de evento é fuzzy, o de-vig é proporcional (uma
aproximação), a linha às vezes é interpolada, e no combo as pernas são
multiplicadas como se fossem independentes — o que sabidamente infla o edge.

Kelly é linear no edge, então um edge inflado vira stake inflada na mesma
proporção. Superestimar `p` com Kelly cheio é a forma clássica de quebrar a
banca *tendo* vantagem real. Daí `KELLY_FRACAO=0.25`, o teto por aposta, e os
descontos extras pra combo e linha interpolada.

Tudo sai em **unidades** (1un = `UNIDADE_PCT_BANCA`% da banca). Quanto vale uma
unidade em dinheiro é decisão do apostador, não do sistema.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import config


@dataclass
class Stake:
    """Recomendação de aposta para uma oferta."""

    unidades: float             # já arredondado, pronto pra exibir
    kelly_cheio: float          # fração da banca no Kelly cheio (diagnóstico)
    fracao_usada: float         # fração da banca efetivamente recomendada
    limitada_pelo_teto: bool    # o teto cortou a recomendação
    descontos: tuple[str, ...]  # o que puxou o stake pra baixo
    fracao_kelly: float = 0.0   # ¼, ⅛... qual fração de Kelly foi usada (rótulo)

    @property
    def apostavel(self) -> bool:
        return self.unidades >= config.STAKE_MIN_UNIDADES

    def descrever(self) -> str:
        """"1.25un (¼ Kelly, combo)" — o que vai na mensagem."""
        if not self.apostavel:
            return f"abaixo de {_fmt(config.STAKE_MIN_UNIDADES)}un — não vale a pena"
        notas = [_rotulo_kelly(self.fracao_kelly)]
        notas.extend(self.descontos)
        if self.limitada_pelo_teto:
            notas.append(f"teto {_fmt(config.STAKE_MAX_UNIDADES)}un")
        return f"{_fmt(self.unidades)}un ({', '.join(notas)})"


def _fmt(valor: float) -> str:
    """2.0 -> "2"; 1.25 -> "1.25". Ninguém escreve "2.0un"."""
    return f"{valor:.2f}".rstrip("0").rstrip(".")


def _rotulo_kelly(fracao_kelly: float | None = None) -> str:
    """`fracao_kelly=None` lê `config.KELLY_FRACAO` no momento da chamada —
    não congela um valor no import, senão mockar a config em teste não teria
    efeito nenhum aqui."""
    if fracao_kelly is None:
        fracao_kelly = config.KELLY_FRACAO
    bonitos = {1.0: "Kelly cheio", 0.5: "½ Kelly", 0.25: "¼ Kelly", 0.125: "⅛ Kelly"}
    return bonitos.get(fracao_kelly, f"{fracao_kelly:g}× Kelly")


def _fracao_kelly_para(confianca: str) -> float:
    """Fração de Kelly por tier de confiança (skill `value-bet-methodology`).

    `alta`/`média-alta` são as duas classes com preço sharp por trás (Pinnacle
    direto, simples ou combo) — mantêm o `KELLY_FRACAO` de sempre (¼).
    `média`/`baixa` são consenso de casa mole (e, dentro dele,
    cartão/chute/artilheiro/handicap e combo sem Pinnacle) — metade do Kelly:
    o mesmo raciocínio de "Kelly é linear no edge, e edge de fonte fraca infla
    igual" que já justifica os descontos multiplicativos abaixo, só que
    aplicado ANTES deles. Lida a cada chamada (não numa tabela fixa no
    import) pra respeitar `config.KELLY_FRACAO` mudando em runtime/teste.
    """
    if confianca in ("média", "baixa"):
        return config.KELLY_FRACAO / 2.0
    return config.KELLY_FRACAO


def kelly_cheio(odd_boost: float, odd_justa: float) -> float:
    """Fração da banca pelo Kelly cheio. 0 quando não há edge."""
    if odd_boost <= 1.0 or odd_justa <= 1.0:
        return 0.0
    p = 1.0 / odd_justa
    b = odd_boost - 1.0
    f = (b * p - (1.0 - p)) / b
    return max(0.0, f)


def _arredondar_para_baixo(valor: float, passo: float) -> float:
    """Sempre pra baixo: errar pra menos custa retorno, pra mais custa banca."""
    if passo <= 0:
        return valor
    # O epsilon evita que 2.9999999 (erro de float) vire 2.75 em vez de 3.
    return math.floor(valor / passo + 1e-9) * passo


def calcular(odd_boost: float, odd_justa: float, *, tipo_mercado: str = "simples",
             interpolada: bool = False, derivada: bool = False,
             consenso: bool = False, confianca: str = "alta",
             flag: str | None = None) -> Stake:
    """Stake recomendada, em unidades, já com fração de Kelly e descontos.

    `confianca` (skill `value-bet-methodology`) decide QUAL fração de Kelly
    entra antes de qualquer desconto multiplicativo: `alta`/`média-alta` usam
    `KELLY_FRACAO` (¼) de sempre; `média`/`baixa` usam metade disso (⅛) — o
    mesmo raciocínio de "Kelly é linear no edge" que já justifica os
    descontos de combo/interpolada/consenso/modelo abaixo, só que aplicado
    ANTES deles, porque eles cobrem erro de origem diferente (não são
    redundantes: um combo via consenso com confiança `baixa` leva o corte de
    ⅛ Kelly E o desconto de combo E o de consenso, todos multiplicados).

    `confianca == "insuficiente"` ou `flag` setada (edge acima do teto de
    sanidade, `EDGE_TETO_SANIDADE`) zeram a recomendação: nos dois casos não
    há base suficiente pra sugerir stake automático, só log pra decisão
    manual.
    """
    if confianca == "insuficiente" or flag is not None:
        return Stake(0.0, 0.0, 0.0, False, ())

    cheio = kelly_cheio(odd_boost, odd_justa)
    if cheio <= 0:
        return Stake(0.0, 0.0, 0.0, False, ())

    fracao_kelly = _fracao_kelly_para(confianca)
    fracao = cheio * fracao_kelly
    descontos: list[str] = []

    # A odd justa de combo é otimista por construção (ver fair_odds.py), então
    # o edge — e com ele o Kelly — vem inflado. Corta antes do teto.
    if tipo_mercado == "combo":
        fracao *= config.STAKE_DESCONTO_COMBO
        descontos.append("combo")
    if interpolada:
        fracao *= config.STAKE_DESCONTO_INTERPOLADA
        descontos.append("linha estimada")
    # Preço observado, mas em casa mole: as casas podem estar erradas juntas, e
    # o Kelly é linear no edge — um consenso enviesado vira stake enviesada.
    if consenso:
        fracao *= config.STAKE_DESCONTO_CONSENSO
        descontos.append("consenso de casas")
    # O mais pesado da lista: aqui a probabilidade não foi observada em preço
    # nenhum, saiu de um modelo ajustado a outros mercados.
    if derivada:
        fracao *= config.STAKE_DESCONTO_MODELO
        descontos.append("modelo")

    unidades = fracao * 100.0 / config.UNIDADE_PCT_BANCA
    limitada = unidades > config.STAKE_MAX_UNIDADES
    if limitada:
        unidades = config.STAKE_MAX_UNIDADES

    unidades = _arredondar_para_baixo(unidades, config.STAKE_PASSO)
    return Stake(
        unidades=unidades,
        kelly_cheio=round(cheio, 6),
        fracao_usada=round(unidades * config.UNIDADE_PCT_BANCA / 100.0, 6),
        limitada_pelo_teto=limitada,
        descontos=tuple(descontos),
        fracao_kelly=fracao_kelly,
    )
