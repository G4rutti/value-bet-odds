"""Ponte fuzzy: `evento_id` de casa não-Altenar -> `evento_id` do pool.

Por que isto existe
--------------------
`mercados_casa` só é escrito pelas casas Altenar (`esportiva.py`), que
compartilham `evento_id`. Betano, Novibet, EsportesDaSorte e CasaDeAposta
(306 ofertas ativas medidas) usam `evento_id` PRÓPRIO, que nunca aparece no
pool — `ProvedorConsenso._linhas()` busca por igualdade exata e sempre volta
vazio pra elas, mesmo quando o mesmo jogo está lá com nome de time.

Esta ponte casa por NOME + DATA (não por `evento_id`), reusando
`matcher.encontrar_evento` — que já tem `split_times`, os dois sentidos de
mandante/visitante, veto de UF e veto de gênero. Nada de fuzzy é
reimplementado aqui.

Guarda inegociável: kickoff dos dois lados
-------------------------------------------
`matcher.encontrar_evento` PULA a checagem de data quando o parâmetro `data`
é `None` (comentário em `matcher.py:195`) — ou seja, SEM data você cai no
modo mais FROUXO, justo onde esta ponte precisa do mais apertado. Por isso:

- linha do pool sem `inicio_evento` nunca vira candidata (filtrada em
  `montar_matchups`, nunca um `commence_time=None` chega no matcher);
- oferta sem `inicio_evento` recusa de cara — sem isso a checagem de data cai
  para "sem checagem" mesmo com o pool inteiro tendo data.

`montar_matchups`/`resolver_evento_id` são separados de
`resolver_evento_id_via_pool` pra permitir cachear a lista de `Matchup`
construída (custo O(M)) fora do laço por-oferta — ver `Storage`, que é quem
compartilha o ciclo inteiro (`ProvedorConsenso` nasce e morre por oferta).
"""

from __future__ import annotations

from . import config
from .matcher import _parse_data, encontrar_evento, split_times
from .models import Matchup


def montar_matchups(pool: list[dict]) -> list[Matchup]:
    """`Matchup` sintéticos a partir de `Storage.eventos_do_pool`.

    Linha sem `evento`, sem `inicio_evento` parseável, ou que não separa em
    dois times (`matcher.split_times`) nunca vira candidata — degradar pra
    "sem checagem de data" é o erro que este módulo existe pra evitar.
    """
    matchups: list[Matchup] = []
    for linha in pool:
        nome = linha.get("evento")
        inicio = linha.get("inicio_evento")
        if not nome or not inicio:
            continue
        commence = _parse_data(inicio)
        if commence is None:
            continue
        times = split_times(nome)
        if times is None:
            continue
        home, away = times
        matchups.append(Matchup(
            id=linha["evento_id"],
            league=linha.get("liga") or "",
            home_team=home,
            away_team=away,
            commence_time=commence,
        ))
    return matchups


def resolver_evento_id(evento: str, inicio_evento: str | None,
                        matchups: list[Matchup]) -> str | None:
    """Mesmo casamento de `resolver_evento_id_via_pool`, mas contra uma lista
    de `Matchup` já montada (o que permite cachear `montar_matchups` fora do
    laço por-oferta)."""
    if not evento or not inicio_evento:
        # Kickoff obrigatório NOS DOIS LADOS. Sem data do lado da oferta, a
        # checagem de data do matcher fica desligada e o casamento vira o
        # modo mais frouxo — o oposto do que esta ponte precisa.
        return None
    if _parse_data(inicio_evento) is None:
        return None
    if not matchups:
        return None

    resultado = encontrar_evento(
        evento, inicio_evento, matchups,
        min_score=config.CONSENSO_MATCH_MIN_SCORE,
        max_horas=config.CONSENSO_MATCH_MAX_HORAS,
    )
    return resultado.event_id if resultado is not None else None


def resolver_evento_id_via_pool(evento: str, inicio_evento: str | None,
                                 pool: list[dict]) -> str | None:
    """`evento_id` do pool que corresponde a `evento`/`inicio_evento`, ou None.

    `pool` é o resultado de `Storage.eventos_do_pool` — uma lista de dicts com
    `evento_id`, `evento`, `inicio_evento`, `liga`. Conveniência sem cache;
    quem chama em laço (`Storage.resolver_evento_consenso`) usa
    `montar_matchups` + `resolver_evento_id` direto pra não reconstruir a
    lista de `Matchup` a cada oferta.
    """
    return resolver_evento_id(evento, inicio_evento, montar_matchups(pool))
