"""Matriz de placares derivada dos mercados líquidos da Pinnacle.

Por que existe
──────────────
A Pinnacle publica placar exato, HT/FT e gols exatos em **~1 de cada 12 jogos**
de futebol (medido em 2026-08-04, amostra de 12 jogos com mercados carregados).
Nos outros ~90% ela publica 1X2 e over/under em praticamente todo jogo — e
desses dois dá pra reconstruir a distribuição de placares.

O que ele NÃO é
───────────────
Não é preço observado. Toda odd que sai daqui é estimativa de modelo, e por
isso vive atrás de trava própria: `fonte_odd` vira `"modelo"`, o threshold de
edge é outro (`EDGE_MIN_MODELO`), o stake leva desconto e — enquanto
`MODELO_ALERTA_ATIVO` for falso — nada disso chega a virar alerta. A trava não
é excesso de zelo: inventar cobertura onde não há preço é exatamente como
nascem os edges falsos de +130% e +141% que este projeto já pagou pra aprender.

Como ajusta
───────────
Poisson bivariado com a correção de Dixon-Coles para os placares baixos (0-0,
1-0, 0-1, 1-1), onde a Poisson pura erra sistematicamente porque os gols de um
jogo não são independentes.

Os dois λ (mandante e visitante) são procurados por busca em grade, minimizando
o erro contra o que o mercado já diz:

    1X2 de-vigado      -> P(casa), P(empate), P(fora)
    over/under de-vig  -> P(acima da linha)

São 3 equações independentes para 2 incógnitas, então é mínimos quadrados, não
solução exata — o resíduo é guardado em `erro` e serve de sinal de desconfiança.

`rho` **não** é ajustado: com só 3 alvos ele fica mal identificado e roubaria
graus de liberdade dos λ, que é o que realmente importa. Fica no valor
padrão da literatura, configurável em `MODELO_RHO`.

Primeiro tempo
──────────────
Ajustado separadamente com `h2h_1t` e `totals_1t`, que a Pinnacle publica com
folga (60 de 60 mercados na amostra). O segundo tempo sai por diferença dos λ,
e HT/FT trata os dois tempos como independentes — que é a hipótese padrão e
também a mais frágil deste módulo.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field

from . import config
from .models import Market, Matchup

log = logging.getLogger(__name__)

# Teto da grade de placares. 8 gols por equipe cobre o resto do espaço com
# folga; o que sobra é somado na última casa pra matriz fechar em 1.
MAX_GOLS = 8

# Faixa plausível de gols esperados por equipe num jogo de futebol.
_LAMBDA_MIN, _LAMBDA_MAX = 0.05, 4.5

_pmf_cache: dict[tuple[float, int], list[float]] = {}


def _pmf(lam: float, n: int = MAX_GOLS) -> list[float]:
    """Poisson truncada em `n`, com a cauda dobrada na última casa."""
    chave = (round(lam, 4), n)
    memo = _pmf_cache.get(chave)
    if memo is not None:
        return memo
    valores = [math.exp(-lam) * lam ** k / math.factorial(k) for k in range(n + 1)]
    valores[n] += max(0.0, 1.0 - sum(valores))
    _pmf_cache[chave] = valores
    return valores


def _tau(h: int, a: int, lh: float, la: float, rho: float) -> float:
    """Correção de Dixon-Coles nos quatro placares baixos.

    A Poisson pura subestima 0-0 e 1-1 e superestima 1-0 e 0-1. Fora dessas
    quatro casas a correção é 1 e a matriz é Poisson independente.
    """
    if h == 0 and a == 0:
        return 1.0 - lh * la * rho
    if h == 0 and a == 1:
        return 1.0 + lh * rho
    if h == 1 and a == 0:
        return 1.0 + la * rho
    if h == 1 and a == 1:
        return 1.0 - rho
    return 1.0


def matriz_placares(lh: float, la: float, rho: float) -> list[list[float]]:
    """P[casa][fora], normalizada pra somar 1."""
    ph, pa = _pmf(lh), _pmf(la)
    m = [[ph[h] * pa[a] * _tau(h, a, lh, la, rho)
          for a in range(MAX_GOLS + 1)] for h in range(MAX_GOLS + 1)]
    total = sum(sum(linha) for linha in m)
    if total <= 0:
        return m
    return [[v / total for v in linha] for linha in m]


@dataclass
class ModeloGols:
    """Distribuição de placares de um jogo, e o que dá pra ler dela."""

    lambda_casa: float
    lambda_fora: float
    erro: float                       # resíduo do ajuste, quanto menor melhor
    matriz: list[list[float]] = field(repr=False, default_factory=list)

    def p_placar(self, casa: int, fora: int) -> float | None:
        if not (0 <= casa <= MAX_GOLS and 0 <= fora <= MAX_GOLS):
            return None
        return self.matriz[casa][fora]

    def p_resultado(self) -> dict[str, float]:
        casa = sum(self.matriz[h][a] for h in range(MAX_GOLS + 1)
                   for a in range(MAX_GOLS + 1) if h > a)
        empate = sum(self.matriz[i][i] for i in range(MAX_GOLS + 1))
        return {"home": casa, "draw": empate, "away": 1.0 - casa - empate}

    def p_total_exato(self, gols: int) -> float:
        return sum(self.matriz[h][gols - h] for h in range(gols + 1)
                   if 0 <= gols - h <= MAX_GOLS and h <= MAX_GOLS)


def _devig(market: Market | None) -> dict[str, float] | None:
    """De-vig proporcional, só o que este módulo precisa."""
    if market is None or not market.completo:
        return None
    implicitas = {nome: 1.0 / odd for nome, odd in market.outcomes.items()}
    total = sum(implicitas.values())
    if not (0.99 <= total <= 1.35):
        return None
    return {nome: v / total for nome, v in implicitas.items()}


def _melhor_total(matchup: Matchup, sufixo: str) -> tuple[float, float] | None:
    """(linha, P(over)) da linha mais informativa.

    A escolhida é a de over mais perto de 50%: é a mais líquida e a que mais
    restringe o ajuste. Linha de over 0.95 quase não diz nada sobre λ.

    ⚠️ Só linhas terminadas em `.5`. A Pinnacle também publica linhas de quarto
    (3.25, 3.75), que são meia aposta em cada linha inteira vizinha — a
    probabilidade delas é uma mistura, não `P(total > linha)`. Tratar 3.25
    como "4 ou mais" fazia dois jogos com totais diferentes caírem no mesmo λ.
    """
    melhor = None
    for chave, market in matchup.markets.items():
        if not chave.startswith(f"totals{sufixo}:"):
            continue
        try:
            linha = float(chave.split(":")[1])
        except (IndexError, ValueError):
            continue
        if abs(linha % 1.0 - 0.5) > 1e-9:
            continue
        probs = _devig(market)
        if not probs or "over" not in probs:
            continue
        dist = abs(probs["over"] - 0.5)
        if melhor is None or dist < melhor[0]:
            melhor = (dist, linha, probs["over"])
    return (melhor[1], melhor[2]) if melhor else None


def _p_over(matriz: list[list[float]], linha: float) -> float:
    return sum(matriz[h][a] for h in range(MAX_GOLS + 1)
               for a in range(MAX_GOLS + 1) if h + a > linha)


def _erro(matriz: list[list[float]], alvo_1x2: dict[str, float],
          alvo_total: tuple[float, float] | None) -> float:
    casa = sum(matriz[h][a] for h in range(MAX_GOLS + 1)
               for a in range(MAX_GOLS + 1) if h > a)
    empate = sum(matriz[i][i] for i in range(MAX_GOLS + 1))
    fora = 1.0 - casa - empate

    err = ((casa - alvo_1x2["home"]) ** 2
           + (empate - alvo_1x2["draw"]) ** 2
           + (fora - alvo_1x2["away"]) ** 2)
    if alvo_total is not None:
        linha, p_over = alvo_total
        # Peso 2: o over/under é a única informação sobre o VOLUME de gols. O
        # 1X2 sozinho fixa a diferença entre os times, não o total — sem este
        # peso o ajuste acerta o 1X2 e erra a escala inteira da matriz.
        err += 2.0 * (_p_over(matriz, linha) - p_over) ** 2
    return err


def _ajustar(alvo_1x2: dict[str, float],
             alvo_total: tuple[float, float] | None,
             rho: float) -> tuple[float, float, float]:
    """Busca em grade, do grosso pro fino. Determinística de propósito:
    ajuste que muda de resultado entre execuções tornaria qualquer
    calibração impossível de reproduzir."""
    melhor = (float("inf"), 1.3, 1.1)

    passo = 0.1
    lh = _LAMBDA_MIN
    while lh <= _LAMBDA_MAX:
        la = _LAMBDA_MIN
        while la <= _LAMBDA_MAX:
            e = _erro(matriz_placares(lh, la, rho), alvo_1x2, alvo_total)
            if e < melhor[0]:
                melhor = (e, lh, la)
            la += passo
        lh += passo

    _, lh0, la0 = melhor
    passo = 0.01
    lh = max(_LAMBDA_MIN, lh0 - 0.1)
    while lh <= min(_LAMBDA_MAX, lh0 + 0.1):
        la = max(_LAMBDA_MIN, la0 - 0.1)
        while la <= min(_LAMBDA_MAX, la0 + 0.1):
            e = _erro(matriz_placares(lh, la, rho), alvo_1x2, alvo_total)
            if e < melhor[0]:
                melhor = (e, lh, la)
            la += passo
        lh += passo

    erro, lh, la = melhor
    return lh, la, erro


def ajustar(matchup: Matchup, sufixo: str = "") -> ModeloGols | None:
    """Modelo do jogo (`sufixo=""`) ou do 1º tempo (`sufixo="_1t"`).

    Devolve None quando falta o 1X2 — sem ele não há nada a ajustar. O
    over/under é opcional mas quase sempre está lá, e sem ele o ajuste fica
    bem pior: o 1X2 fixa a diferença entre os times, não o total de gols.
    """
    alvo_1x2 = _devig(matchup.markets.get(f"h2h{sufixo}"))
    if not alvo_1x2 or not {"home", "draw", "away"} <= alvo_1x2.keys():
        return None

    alvo_total = _melhor_total(matchup, sufixo)
    if alvo_total is None:
        log.debug("sem over/under em %s%s — ajuste só pelo 1X2",
                  matchup.display_name, sufixo)

    rho = config.MODELO_RHO
    lh, la, erro = _ajustar(alvo_1x2, alvo_total, rho)
    return ModeloGols(lambda_casa=lh, lambda_fora=la, erro=erro,
                      matriz=matriz_placares(lh, la, rho))


def modelo_do_jogo(matchup: Matchup, sufixo: str = "") -> ModeloGols | None:
    """`ajustar` com cache no próprio matchup — o ajuste é caro e o jogo é o
    mesmo para todas as ofertas do ciclo."""
    cache = getattr(matchup, "_modelos", None)
    if cache is None:
        cache = {}
        setattr(matchup, "_modelos", cache)
    if sufixo not in cache:
        cache[sufixo] = ajustar(matchup, sufixo)
    return cache[sufixo]
