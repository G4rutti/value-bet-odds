"""Cálculo da odd justa: de-vig da Pinnacle + combinação de pernas.

O de-vig, em português claro
────────────────────────────
Toda casa infla os preços pra que as probabilidades implícitas somem MAIS que
100% — a sobra é a margem (vig/overround). Ex., Pinnacle 1X2:

    home 1.77 / draw 3.60 / away 5.19
    implícitas: 1/1.77 + 1/3.60 + 1/5.19 = 0.565 + 0.278 + 0.193 = 1.036
    overround = 3.6%

Normalizando pela soma:

    prob_justa(home) = 0.565 / 1.036 = 0.545  ->  odd justa = 1/0.545 = 1.835

Sem esse passo, a odd crua da Pinnacle ainda carrega a margem dela e o edge
calculado sai sistematicamente MENOR do que é de verdade.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rapidfuzz import fuzz

# ProbJusta, remover_vig, SOMA_ESPERADA, MARGEM_MIN/MAX e _lado_do_time
# migraram pro pacote compartilhado `odds_service` (Projetos-pessoais/
# odds-service) — ver `.matcher`/`.models`, que são shims sobre o pacote.
# Reimportados aqui (não só usados) porque `consenso.py:52` faz
# `from .fair_odds import ProbJusta, remover_vig` — quebrar esse import
# quebraria o consenso.
from odds_service.devig import MARGEM_MAX, MARGEM_MIN, ProbJusta, SOMA_ESPERADA, remover_vig

from . import config
from .market_parser import ALTERNATIVAS, Leg
from .matcher import _lado_do_time, normalizar
from .models import Market, Matchup
from .modelo_gols import MAX_GOLS, ModeloGols, matriz_placares, modelo_do_jogo

if TYPE_CHECKING:
    # Só pro type checker: `consenso` importa `ProbJusta` e `remover_vig` daqui,
    # e um import de verdade fecharia o ciclo.
    from .consenso import ProvedorConsenso

log = logging.getLogger(__name__)


def _trace_ativo() -> bool:
    """`FAIR_ODDS_TRACE=1` (env) liga o modo trace.

    Lido em CADA chamada (não uma vez no import) de propósito: `avaliar.py
    --trace` seta a env var dentro de `main()`, depois que este módulo já foi
    importado pela cadeia de imports do topo do arquivo. Se o valor fosse
    congelado no import, a flag de linha de comando nunca funcionaria.
    """
    return os.getenv("FAIR_ODDS_TRACE", "") not in ("", "0", "false", "False")


def _trace_perna(leg: "Leg", chave: str, market: "Market | None",
                 resultado: "ProbJusta | None") -> None:
    """Uma linha estruturada por perna, em INFO — não `log.debug`.

    Existe porque a classe de bug documentada na skill `value-bet-methodology`
    (mercado restrito casado com referência ampla) só aparece na CHAVE
    resolvida e no LABEL da Pinnacle, não nos números finais — uma justa de
    4.05 e uma de 2.79 são igualmente plausíveis olhando só o resultado. Hoje
    todo diagnóstico equivalente é `log.debug` e some sem `-v`.
    """
    if not _trace_ativo() or resultado is None:
        return
    label = market.label if market is not None else resultado.mercado
    linhas = [f"TRACE perna={leg.texto!r}",
              f"  chave={chave}  label={label!r}"]
    if market is not None:
        overround_bruto = sum(1.0 / odd for odd in market.outcomes.values())
        cruas = {nome: round(odd, 4) for nome, odd in market.outcomes.items()}
        linhas.append(f"  cruas={cruas}  overround={overround_bruto:.4f} "
                      f"(margem {resultado.margem_pct:.2f}%)")
    alvo = leg.selecao or leg.time_nome
    linhas.append(f"  selecao={alvo!r}  prob={resultado.probabilidade:.5f}  "
                  f"justa={resultado.odd_justa}")
    log.info("\n".join(linhas))


@dataclass
class FairOddsResult:
    odd_justa: float | None
    tipo_mercado: str                    # "simples" | "combo"
    fonte_odd: str                       # "pinnacle" | "consenso" | "modelo"
    pernas: list[ProbJusta] = field(default_factory=list)
    motivo_falha: str | None = None
    # Quantas casas formaram a mediana, quando a fonte foi consenso. Vai pro
    # alerta: "consenso de 3 casas" e "de 9" merecem confiança diferente.
    n_casas_consenso: int = 0
    # Quantos preços DISTINTOS havia entre essas casas. Sem isto o alerta diz
    # "3 casas" para o que muitas vezes é uma fonte só replicada — as casas
    # Altenar compartilham o feed em 90% das seleções.
    n_precos_consenso: int = 0

    @property
    def ok(self) -> bool:
        return self.odd_justa is not None

    @property
    def interpolada(self) -> bool:
        """Alguma perna teve a linha estimada em vez de lida direto."""
        return any(p.interpolada for p in self.pernas)

    @property
    def derivada(self) -> bool:
        """Alguma perna veio do modelo. Basta UMA pra oferta inteira contar
        como modelo: a odd justa do conjunto herda o erro da pior perna."""
        return any(p.derivada for p in self.pernas)

    @property
    def por_consenso(self) -> bool:
        """Alguma perna foi precificada pelas casas, não pela Pinnacle."""
        return any(p.consenso for p in self.pernas)


def _resolver_template(selecao: str, matchup: Matchup) -> list[str]:
    """Rótulo de especial vira o texto exato que a Pinnacle usa.

    Os mercados especiais (`Correct Score`, `Half-Time/Full-Time`, `Double
    Chance`, `Both Teams To Score/Winner`) indexam os desfechos pelo NOME dos
    times — "New York City 2, Santos Laguna 1", "Draw Or Santos Laguna". O
    parser não pode montar isso: ele não sabe quem é o mandante nem como a
    Pinnacle escreve o nome. Então ele emite um molde e a substituição acontece
    aqui, depois do match.

    `||` separa alternativas: a chance dupla tem ordem fixa na Pinnacle
    (mandante primeiro) e o parser não sabe a ordem certa, então manda as duas.
    Só uma existe no mercado, então não há ambiguidade.
    """
    saida = []
    for alternativa in selecao.split(ALTERNATIVAS):
        texto = (alternativa.replace("{home}", matchup.home_team)
                            .replace("{away}", matchup.away_team))
        for nome in re.findall(r"\{time:([^}]+)\}", texto):
            lado = _lado_do_time(nome, matchup)
            if lado is None:
                break
            real = matchup.home_team if lado == "home" else matchup.away_team
            texto = texto.replace(f"{{time:{nome}}}", real)
        else:
            saida.append(texto)
    return saida


def _resolver_lado(leg: Leg, matchup: Matchup, probs: dict[str, ProbJusta],
                   selecao: str | None = None) -> ProbJusta | None:
    """Descobre qual lado do mercado corresponde à perna."""
    selecao = leg.selecao if selecao is None else selecao

    # Molde de mercado especial: resolver os nomes antes de procurar.
    if selecao and ("{" in selecao or ALTERNATIVAS in selecao):
        for candidato in _resolver_template(selecao, matchup):
            achado = probs.get(candidato)
            if achado:
                return achado
        log.debug("nenhum desfecho da Pinnacle bate com o molde %r", selecao)
        return None

    # Lado já canônico (home/draw/away/over/under/Yes/No).
    if selecao:
        direto = probs.get(selecao)
        if direto:
            return direto
        alvo = selecao.lower()
        return next((p for n, p in probs.items() if n.lower() == alvo), None)

    # Lado veio por nome de time/jogador: descobre se é mandante ou visitante.
    if leg.time_nome:
        lado = _lado_do_time(leg.time_nome, matchup)
        return probs.get(lado) if lado else None

    return None


def _resolver_chave(leg: Leg, matchup: Matchup) -> tuple[str, str | None] | None:
    """Resolve a chave final do mercado e o lado a ler.

    Duas famílias só ficam decidíveis depois do match do evento, porque
    dependem de saber quem é o mandante:

    - total por equipe (`team_total:{lado}:2.5`) — a chave traz um placeholder;
    - handicap — a Pinnacle indexa o bloco pela linha DO MANDANTE, e a Betano
      cita a linha do time escolhido. Se o escolhido é o visitante, a linha do
      mandante é a mesma com o sinal trocado.
    """
    chave = leg.market_key or ""

    if leg.handicap_linha is not None:
        lado = _lado_do_time(leg.time_nome or "", matchup)
        if lado is None:
            return None
        linha = leg.handicap_linha if lado == "home" else -leg.handicap_linha
        return f"{chave}:{linha if linha != 0 else 0.0}", lado

    if "{lado}" in chave:
        lado = _lado_do_time(leg.time_nome or "", matchup)
        if lado is None:
            return None
        return chave.replace("{lado}", lado), leg.selecao

    if "{stat}" in chave:
        resolvida = _stat_do_prop(chave, matchup)
        if resolvida is None:
            return None
        return resolvida, leg.selecao

    return chave, leg.selecao


def _stat_do_prop(chave: str, matchup: Matchup) -> str | None:
    """Descobre QUAL estatística é o prop, pela linha — ou desiste.

    O rótulo da casa ("Nyara Sabally (TOR) mais 4.5") não diz se 4.5 é rebote,
    assistência ou ponto, e a Pinnacle publica os quatro do mesmo jogador. A
    única resolução honesta é a linha ser única: se dois mercados daquele
    jogador têm a mesma linha, não há como escolher, e escolher errado compara
    a odd de um mercado contra a justa de outro.

    Devolver None aqui vira "sem odd na Pinnacle para: <perna>" — o mesmo
    caminho de qualquer mercado ausente.
    """
    prefixo, _, sufixo = chave.partition("{stat}")
    candidatos = [k for k in matchup.markets
                  if k.startswith(prefixo) and k.endswith(sufixo)]
    if len(candidatos) == 1:
        return candidatos[0]
    if candidatos:
        log.debug("prop ambíguo em %s: %s casam %s",
                  matchup.display_name, len(candidatos), chave)
    return None


def _placar_do_rotulo(rotulo: str, matchup: Matchup) -> tuple[int, int] | None:
    """"Celtic 2, Dundee 1" -> (2, 1). O molde já foi resolvido antes."""
    m = re.match(rf"^{re.escape(matchup.home_team)}\s+(\d+),\s*"
                 rf"{re.escape(matchup.away_team)}\s+(\d+)$", rotulo.strip())
    return (int(m.group(1)), int(m.group(2))) if m else None


def _lado_do_rotulo(rotulo: str, matchup: Matchup) -> str | None:
    if rotulo == "Draw":
        return "draw"
    if rotulo == matchup.home_team:
        return "home"
    if rotulo == matchup.away_team:
        return "away"
    return None


def probabilidade_derivada(chave: str, selecao: str | None,
                           matchup: Matchup) -> ProbJusta | None:
    """Probabilidade tirada do modelo de placar, quando a Pinnacle não publica.

    Cobre só o que a matriz de placares determina sem hipótese extra:
    placar exato, gols exatos e HT/FT. Qualquer outra chave devolve None — é
    melhor não avaliar do que avaliar com um número inventado.
    """
    if not selecao:
        return None

    base = chave.split(":")[0]
    if base not in ("correct_score", "correct_score_1t",
                    "exact_goals", "exact_goals_1t", "ht_ft"):
        return None

    sufixo = "_1t" if base.endswith("_1t") else ""
    modelo = modelo_do_jogo(matchup, sufixo)
    if modelo is None:
        return None
    if modelo.erro > config.MODELO_ERRO_MAX:
        # Não reproduziu nem o mercado que o alimentou.
        log.debug("modelo descartado em %s%s: resíduo %.6f",
                  matchup.display_name, sufixo, modelo.erro)
        return None

    # O molde ("{home} 2, {away} 1") só é resolvido em `_resolver_lado`, e
    # aquele caminho exige que o mercado exista. Aqui ele não existe — é o
    # motivo de estarmos no modelo —, então a resolução acontece agora.
    candidatos = (_resolver_template(selecao, matchup)
                  if ("{" in selecao or ALTERNATIVAS in selecao) else [selecao])
    prob = next((p for p in (_prob_do_modelo(base, c, modelo, matchup)
                             for c in candidatos) if p is not None), None)
    if prob is None or not 0.0 < prob < 1.0:
        return None

    return ProbJusta(
        probabilidade=prob,
        odd_justa=round(1.0 / prob, 4),
        overround=0.0,          # não há vig a remover: não é preço de ninguém
        mercado=f"{base} (modelo λ {modelo.lambda_casa:.2f}/{modelo.lambda_fora:.2f})",
        derivada=True,
    )


def _prob_do_modelo(base: str, selecao: str, modelo: "ModeloGols",
                    matchup: Matchup) -> float | None:
    if base in ("correct_score", "correct_score_1t"):
        placar = _placar_do_rotulo(selecao, matchup)
        return modelo.p_placar(*placar) if placar else None

    if base in ("exact_goals", "exact_goals_1t"):
        try:
            return modelo.p_total_exato(int(selecao))
        except ValueError:
            return None

    if base == "ht_ft":
        # Único ponto do módulo que combina DOIS modelos, e o único que assume
        # independência entre os tempos. Se o 1º tempo não fecha, não há HT/FT.
        primeiro = modelo_do_jogo(matchup, "_1t")
        if primeiro is None or primeiro.erro > config.MODELO_ERRO_MAX:
            return None
        partes = [p.strip() for p in selecao.split(" - ")]
        if len(partes) != 2:
            return None
        ht, ft = (_lado_do_rotulo(p, matchup) for p in partes)
        if ht is None or ft is None:
            return None
        return _p_ht_ft(primeiro, modelo, ht, ft)

    return None


def _p_ht_ft(primeiro: "ModeloGols", jogo: "ModeloGols",
             ht: str, ft: str) -> float | None:
    """P(resultado no intervalo = ht E resultado final = ft).

    O 2º tempo sai por diferença dos λ e é tratado como independente do 1º —
    hipótese padrão, e a mais frágil deste módulo. λ negativo significa que os
    dois ajustes se contradizem; aí não há modelo, e devolver None é o certo.
    """
    lh2 = jogo.lambda_casa - primeiro.lambda_casa
    la2 = jogo.lambda_fora - primeiro.lambda_fora
    if lh2 <= 0 or la2 <= 0:
        return None
    segundo = matriz_placares(lh2, la2, config.MODELO_RHO)

    def resultado(dif: int) -> str:
        return "home" if dif > 0 else ("away" if dif < 0 else "draw")

    total = 0.0
    for h1 in range(MAX_GOLS + 1):
        for a1 in range(MAX_GOLS + 1):
            p1 = primeiro.matriz[h1][a1]
            if p1 <= 0 or resultado(h1 - a1) != ht:
                continue
            for h2 in range(MAX_GOLS + 1):
                for a2 in range(MAX_GOLS + 1):
                    if resultado((h1 + h2) - (a1 + a2)) == ft:
                        total += p1 * segundo[h2][a2]
    return total


def _predicado_placar(leg: Leg, matchup: Matchup):
    """A perna como condição sobre o placar final, ou None se não der.

    Só mercados do jogo inteiro que são função do placar. Escanteios, cartões
    e qualquer coisa por tempo ficam de fora — a matriz não fala sobre eles, e
    fingir que fala seria pior que assumir independência.
    """
    chave, sel = leg.market_key or "", leg.selecao
    partes = chave.split(":")
    base = partes[0]
    if base.endswith("_1t"):
        return None

    if base == "h2h":
        lado = sel or _lado_do_time(leg.time_nome or "", matchup)
        if lado == "home":
            return lambda h, a: h > a
        if lado == "away":
            return lambda h, a: h < a
        if lado == "draw":
            return lambda h, a: h == a
        return None

    if base == "totals" and sel in ("over", "under") and len(partes) > 1:
        linha = float(partes[1])
        return ((lambda h, a: h + a > linha) if sel == "over"
                else (lambda h, a: h + a < linha))

    if base == "team_total" and sel in ("over", "under") and len(partes) > 2:
        equipe, linha = partes[1], float(partes[2])
        if equipe not in ("home", "away"):
            return None
        pega = (lambda h, a: h) if equipe == "home" else (lambda h, a: a)
        return ((lambda h, a: pega(h, a) > linha) if sel == "over"
                else (lambda h, a: pega(h, a) < linha))

    if base == "btts":
        if sel == "Yes":
            return lambda h, a: h > 0 and a > 0
        if sel == "No":
            return lambda h, a: h == 0 or a == 0
        return None

    if base == "exact_goals" and sel and sel.isdigit():
        total = int(sel)
        return lambda h, a: h + a == total

    return None


def _conjunta_correlacionada(legs: list[Leg], probs: list[ProbJusta],
                             matchup: Matchup) -> float | None:
    """Conjunta do combo lida na matriz de placares, sem assumir independência.

    Multiplicar as pernas assume que elas são independentes, e no futebol elas
    não são — pior, o erro é sempre na direção que INFLA o edge. Caso real de
    2026-08-04: "1x2: Empate + Total: Mais de 2.5" num jogo com favorito
    pesado. Independência dava justa 14.98 contra odd 24 (+60% de "value"),
    mas empate com 3+ gols quer dizer 2-2 ou 3-3 — perto de 2%, justa lá pra
    52. O combo era EV negativo se passando por oportunidade.

    A correção vale para o SUBCONJUNTO de pernas que é função do placar; as
    outras (escanteios, cartões) seguem multiplicadas. Meia correção é melhor
    que nenhuma: "over 2.5 + ambas não marcam + escanteios" tem a pior
    correlação justamente entre as duas primeiras, que a matriz cobre.
    """
    indices = [i for i, l in enumerate(legs) if _predicado_placar(l, matchup)]
    if len(indices) < 2:
        return None

    modelo = modelo_do_jogo(matchup, "")
    if modelo is None or modelo.erro > config.MODELO_ERRO_MAX_CORRELACAO:
        return None

    predicados = [_predicado_placar(legs[i], matchup) for i in indices]
    sub = sum(modelo.matriz[h][a]
              for h in range(MAX_GOLS + 1) for a in range(MAX_GOLS + 1)
              if all(p(h, a) for p in predicados))

    total = sub
    for i, p in enumerate(probs):
        if i not in indices:
            total *= p.probabilidade
    return total


def _conjunta_correlacionada_multi(
        legs: list[Leg], probs: list[ProbJusta], matchup: Matchup,
        matchups_por_jogo: dict[str, Matchup] | None) -> float | None:
    """Como `_conjunta_correlacionada`, mas agrupando por jogo primeiro.

    `_conjunta_correlacionada` indexa `legs`/`probs` posicionalmente contra
    UM `matchup` — correto quando a oferta é um jogo só. Num combo
    multi-jogo (Novibet "Festival de Gols", ver `pipeline.py`), pernas de
    jogos diferentes não têm relação nenhuma entre si: tratá-las como
    correlacionadas juntaria dois placares que não têm nada a ver. A
    correção só pode olhar DENTRO do grupo de cada jogo; entre grupos, o
    produto das probabilidades já é o valor certo — não uma aproximação
    otimista — porque jogos diferentes SÃO independentes de verdade.
    """
    if not matchups_por_jogo:
        return _conjunta_correlacionada(legs, probs, matchup)

    grupos: dict[str, list[int]] = {}
    for i, leg in enumerate(legs):
        grupos.setdefault(leg.evento_texto or "__oferta__", []).append(i)

    if len(grupos) == 1:
        return _conjunta_correlacionada(legs, probs, matchup)

    total = 1.0
    algum_corrigido = False
    for chave, indices in grupos.items():
        matchup_grupo = matchup if chave == "__oferta__" else matchups_por_jogo.get(chave, matchup)
        sub_legs = [legs[i] for i in indices]
        sub_probs = [probs[i] for i in indices]
        corrigida = (_conjunta_correlacionada(sub_legs, sub_probs, matchup_grupo)
                    if len(sub_legs) > 1 else None)
        if corrigida is not None:
            total *= corrigida
            algum_corrigido = True
        else:
            for p in sub_probs:
                total *= p.probabilidade
    return total if algum_corrigido else None


# Famílias onde `time_nome` só escolhe QUAL LADO ler (home/draw/away, ou o
# jogador do handicap) de um mercado que já é indexado por lado — não têm uma
# versão "restrita a um time só" que possa ser confundida com a versão ampla,
# então a chave não precisa (nem pode) ganhar sufixo `:home`/`:away`.
#
# Mercados de PARTIÇÃO (totais, gols exatos, escanteios) têm as duas versões —
# jogo inteiro e por time — e foi exatamente aí que o bug do Internacional
# aconteceu (`market_parser._parse_especiais`, "gols exatos" sem âncora à
# esquerda casando com o mercado do jogo inteiro). `startswith` cobre os
# sufixos de período/set (`h2h_1t`, `games_spread_s1`, ...) sem listar cada um.
_PREFIXOS_LADO_SEM_SUFIXO = (
    "h2h", "spread", "corners_h2h", "corners_spread",
    "games_spread", "sets_spread", "sets_h2h", "dnb",
)


def _chave_ja_seleciona_lado(chave: str) -> bool:
    return (chave.split(":")[0] or "").startswith(_PREFIXOS_LADO_SEM_SUFIXO)


def _tem_componente_de_time(chave: str) -> bool:
    partes = chave.split(":")
    return "home" in partes or "away" in partes


def prob_da_perna(leg: Leg, matchup: Matchup) -> ProbJusta | None:
    """Probabilidade de-vigada de uma perna. None se não houver referência."""
    if not leg.suportado or not leg.market_key:
        return None

    # Guarda de equivalência: "resultado exato em sets 2-0" só equivale a um
    # handicap de -1.5 sets em melhor-de-3. `sets_total:2.5` é a assinatura de
    # bo3 — sem ela, a equivalência não vale e é melhor não avaliar.
    if leg.exige_mercado and matchup.get(leg.exige_mercado) is None:
        log.debug("guarda %s ausente em %s — perna descartada",
                  leg.exige_mercado, matchup.display_name)
        return None

    resolvido = _resolver_chave(leg, matchup)
    if resolvido is None:
        return None
    chave, selecao = resolvido

    # Guarda estrutural (mata a CLASSE do bug, não só a instância): se o
    # rótulo nomeia um time, a chave resolvida tem que ter componente de time
    # em mercado de partição — senão o de-vig compararia o mercado do TIME
    # contra a referência do JOGO INTEIRO (mercado restrito x referência
    # ampla, ver skill `value-bet-methodology`). Não recusa em h2h/handicap/
    # corners_h2h: lá `time_nome` É o lado, não falta sufixo nenhum.
    if (leg.time_nome and not _chave_ja_seleciona_lado(chave)
            and not _tem_componente_de_time(chave)):
        log.info("guarda: perna %r nomeia %r mas a chave resolvida %s não tem "
                 "componente de time (:home/:away) — recusada (mercado "
                 "restrito x referência ampla)", leg.texto, leg.time_nome, chave)
        return None

    market = matchup.get(chave)
    if market is None:
        # A linha exata da Betano pode não estar publicada, mas as vizinhas sim.
        if config.INTERPOLAR_LINHAS and selecao in ("over", "under"):
            interp = _interpolar_linha(chave, matchup, selecao)
            if interp is not None:
                _trace_perna(leg, chave, None, interp)
                return interp

        # 1X2 de escanteios: a Pinnacle não publica moneyline de escanteios em
        # jogo nenhum (conferido em 60), mas publica o handicap — e dele o 1X2
        # sai por aritmética, não por modelo.
        if chave.startswith("corners_h2h"):
            lado = selecao or _lado_do_time(leg.time_nome or "", matchup)
            resultado = _h2h_de_escanteios(matchup, chave[len("corners_h2h"):], lado)
            _trace_perna(leg, chave, None, resultado)
            return resultado
        # Último recurso: derivar do modelo de placar. Só entra quando a
        # Pinnacle não publica o mercado — preço observado sempre ganha.
        derivada = probabilidade_derivada(chave, selecao, matchup)
        if derivada is not None:
            _trace_perna(leg, chave, None, derivada)
            return derivada
        log.debug("mercado %s ausente em %s", chave, matchup.display_name)
        return None

    probs = remover_vig(market)
    if not probs:
        return None

    resultado = _resolver_lado(leg, matchup, probs, selecao)
    _trace_perna(leg, chave, market, resultado)
    return resultado


def _h2h_de_escanteios(matchup: Matchup, sufixo: str,
                       lado: str | None) -> ProbJusta | None:
    """P(vencer os escanteios) a partir do handicap de ±0.5.

    Com A = P(mandante cobre −0.5) e B = P(mandante cobre +0.5):

        P(mandante) = A          (vencer por 1+)
        P(empate)   = B − A      (a faixa entre "vence" e "vence ou empata")
        P(visitante)= 1 − B

    Não é modelo: são dois preços observados, de-vigados, e a conta é exata —
    mesma natureza do `_interpolar_linha`, e por isso sai com a mesma marca.

    Só funciona quando o ladder passa pelo zero. Em jogo desequilibrado a
    Pinnacle publica de −3.0 pra baixo (Cruzeiro x Chapecoense) e aí não há
    ±0.5 pra ler — devolve None, que vira "sem odd na Pinnacle" como qualquer
    mercado ausente.
    """
    if lado not in ("home", "draw", "away"):
        return None
    abaixo = matchup.get(f"corners_spread{sufixo}:-0.5")
    acima = matchup.get(f"corners_spread{sufixo}:0.5")
    if abaixo is None or acima is None:
        return None

    p_abaixo, p_acima = remover_vig(abaixo), remover_vig(acima)
    if not p_abaixo or not p_acima:
        return None
    a, b = p_abaixo.get("home"), p_acima.get("home")
    if a is None or b is None:
        return None

    # "Vence ou empata" tem que ser mais provável que "vence". Se o ladder
    # devolve o contrário, alguma das duas linhas não é o que pensamos.
    if not b.probabilidade > a.probabilidade:
        log.debug("ladder de escanteios incoerente em %s: %.4f <= %.4f",
                  matchup.display_name, b.probabilidade, a.probabilidade)
        return None

    prob = {"home": a.probabilidade,
            "draw": b.probabilidade - a.probabilidade,
            "away": 1.0 - b.probabilidade}[lado]
    if not 0.0 < prob < 1.0:
        return None

    return ProbJusta(
        probabilidade=prob,
        odd_justa=round(1.0 / prob, 4),
        overround=0.0,   # as duas pontas já vieram sem vig
        mercado=f"1x2 de escanteios{sufixo} (do handicap ±0.5)",
        interpolada=True,
    )


def _prob_over(market: Market) -> float | None:
    """Probabilidade de-vigada do "over" de um mercado over/under."""
    probs = remover_vig(market)
    if not probs:
        return None
    over = next((p for n, p in probs.items() if n.lower() == "over"), None)
    return over.probabilidade if over else None


def _interpolar_linha(chave: str, matchup: Matchup, selecao: str) -> ProbJusta | None:
    """Estima a probabilidade numa linha que a Pinnacle não publicou.

    Só funciona ENTRE duas linhas publicadas: acha a maior abaixo e a menor
    acima do alvo e interpola linearmente P(over), que é monótona decrescente
    na linha. Nunca extrapola — fora do intervalo o erro é ilimitado, e aí é
    melhor não avaliar do que chutar.

    Ex.: a Betano oferece "Games no Set Mais de 9.5"; a Pinnacle publica 8.5 e
    10.5. P(over 9.5) sai no meio das duas.
    """
    familia, _, texto_linha = chave.rpartition(":")
    if not familia:
        return None
    try:
        alvo = float(texto_linha)
    except ValueError:
        return None

    abaixo: tuple[float, Market] | None = None
    acima: tuple[float, Market] | None = None
    for k, mk in matchup.markets.items():
        fam, _, txt = k.rpartition(":")
        if fam != familia:
            continue
        if {n.lower() for n in mk.outcomes} != {"over", "under"}:
            continue
        try:
            linha = float(txt)
        except ValueError:
            continue
        if linha < alvo and (abaixo is None or linha > abaixo[0]):
            abaixo = (linha, mk)
        elif linha > alvo and (acima is None or linha < acima[0]):
            acima = (linha, mk)

    if abaixo is None or acima is None:
        return None
    if acima[0] - abaixo[0] > config.INTERPOLACAO_GAP_MAX:
        log.debug("intervalo %s-%s largo demais para %s", abaixo[0], acima[0], chave)
        return None

    p_baixo = _prob_over(abaixo[1])
    p_alto = _prob_over(acima[1])
    if p_baixo is None or p_alto is None:
        return None

    peso = (alvo - abaixo[0]) / (acima[0] - abaixo[0])
    p_over = p_baixo + (p_alto - p_baixo) * peso
    prob = p_over if selecao == "over" else 1.0 - p_over
    if not 0.0 < prob < 1.0:
        return None

    log.debug("linha %s interpolada entre %s e %s", alvo, abaixo[0], acima[0])
    return ProbJusta(
        probabilidade=prob,
        odd_justa=round(1.0 / prob, 4),
        overround=0.0,   # as duas pontas já vieram sem vig
        mercado=f"{abaixo[1].label} ↔ {acima[1].label} (interpolada em {alvo})",
        interpolada=True,
    )


def calcular_odd_justa(legs: list[Leg], matchup: Matchup,
                       consenso: "ProvedorConsenso | None" = None,
                       matchups_por_jogo: dict[str, Matchup] | None = None) -> FairOddsResult:
    """Odd justa da oferta inteira.

    `consenso` é o fallback para perna que a Pinnacle não cobre (prop de
    jogador: chutes ao gol, cartões, artilheiro). Quando ausente, o
    comportamento é o de antes — perna sem cobertura mata a oferta. Ver
    `value/consenso.py` para as ressalvas dessa fonte.

    `matchups_por_jogo` é aditivo: mapa `evento_texto -> Matchup` só
    preenchido em combo multi-jogo (`pipeline.py`). Uma perna com
    `leg.evento_texto` é precificada contra O SEU jogo, não contra
    `matchup` (o da oferta) — sem isto, um combo de jogos diferentes
    multiplicaria a probabilidade de UM jogo várias vezes. Ausente ou vazio,
    o comportamento é idêntico ao de antes.

    Mercado simples: de-vig direto do lado correspondente.

    Combo: multiplica as probabilidades de-vigadas das pernas.

    ⚠️ A multiplicação assume que as pernas são INDEPENDENTES, o que não é
    verdade no futebol — um jogo com resultado mais definido tende a ter mais
    gols, mais escanteios e mais chutes. Como as pernas são positivamente
    correlacionadas, a probabilidade real do combo é MAIOR que o produto, ou
    seja, a odd justa calculada aqui sai OTIMISTA (baixa demais) e o edge sai
    inflado. É estimativa, não valor exato — por isso o threshold de combo é
    bem mais alto que o de mercado simples (ver config.EDGE_MIN_COMBO).
    """
    tipo = "simples" if len(legs) == 1 else "combo"

    sem_cobertura = [l for l in legs if not l.suportado]
    if sem_cobertura and consenso is None:
        motivos = ", ".join(sorted({l.motivo or "?" for l in sem_cobertura}))
        return FairOddsResult(None, tipo, "pinnacle", motivo_falha=f"sem cobertura: {motivos}")

    probs: list[ProbJusta] = []
    casas_consenso = 0
    precos_consenso = 0
    for leg in legs:
        matchup_da_perna = (
            matchups_por_jogo.get(leg.evento_texto, matchup)
            if leg.evento_texto and matchups_por_jogo else matchup
        )
        # Pinnacle primeiro, sempre: consenso é o que se usa quando não há
        # fonte sharp, nunca no lugar dela.
        p = prob_da_perna(leg, matchup_da_perna) if leg.suportado else None
        if p is None and consenso is not None:
            achado = consenso.prob_para(leg.texto)
            if achado is not None:
                p = achado.prob
                # A perna PIOR manda: com várias pernas de consenso, o que
                # descreve a oferta é a menos apoiada, não a melhor.
                casas_consenso = (achado.n_casas if casas_consenso == 0
                                  else min(casas_consenso, achado.n_casas))
                precos_consenso = (achado.n_precos if precos_consenso == 0
                                   else min(precos_consenso, achado.n_precos))
        if p is None:
            motivo = (f"sem cobertura: {leg.motivo or '?'}" if not leg.suportado
                      else f"sem odd na Pinnacle para: {leg.texto}")
            return FairOddsResult(None, tipo, "pinnacle", motivo_falha=motivo)
        probs.append(p)

    conjunta = 1.0
    for p in probs:
        conjunta *= p.probabilidade

    if conjunta <= 0:
        return FairOddsResult(None, tipo, "pinnacle", motivo_falha="probabilidade conjunta zero")

    # Correlação: quando as pernas são todas função do placar, a matriz dá a
    # conjunta de verdade. Fica a MENOR das duas de propósito — o produto por
    # independência só erra pra cima, e é aí que nasce value falso. Deixar o
    # modelo aumentar a probabilidade seria trocar preço observado por
    # estimativa em troca de um edge maior, exatamente o negócio errado.
    correlacionada = None
    if len(legs) > 1:
        correlacionada = _conjunta_correlacionada_multi(legs, probs, matchup, matchups_por_jogo)
    if correlacionada is not None and 0 < correlacionada < conjunta:
        log.debug("combo corrigido por correlação: %.5f -> %.5f",
                  conjunta, correlacionada)
        conjunta = correlacionada
        probs = [*probs, ProbJusta(
            probabilidade=correlacionada,
            odd_justa=round(1.0 / correlacionada, 4),
            overround=0.0,
            mercado="conjunta corrigida por correlação (modelo)",
            derivada=True,
        )]

    # A fonte da oferta é a da PIOR perna, não a da maioria: a odd justa do
    # conjunto herda o erro de quem foi estimado com menos base. É o que faz o
    # resto do pipeline aplicar threshold, desconto de stake e marcação
    # próprios — odd derivada ou de consenso não pode passar por preço sharp.
    if any(p.derivada for p in probs):
        fonte = "modelo"
    elif any(p.consenso for p in probs):
        fonte = "consenso"
    else:
        fonte = "pinnacle"

    return FairOddsResult(
        odd_justa=round(1.0 / conjunta, 4),
        tipo_mercado=tipo,
        fonte_odd=fonte,
        pernas=probs,
        n_casas_consenso=casas_consenso,
        n_precos_consenso=precos_consenso,
    )
