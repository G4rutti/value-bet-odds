"""Migrado pro pacote compartilhado `odds_service` — ver Projetos-pessoais/odds-service.

Shim de compatibilidade: `consenso.py`, `pool_eventos.py`, `liquidacao.py`,
`stats_check.py` e `fair_odds.py` continuam importando `.matcher` normalmente.
"""

from odds_service.matcher import (
    FEMININO_LIGA,
    FEMININO_NOME,
    RUIDO,
    SEPARADORES,
    UF,
    MatchResult,
    _lado_do_time,
    _parse_data,
    _score_nome,
    encontrar_evento,
    extrair_uf,
    genero_conflita,
    normalizar,
    score_times,
    split_times,
    uf_conflita,
)

__all__ = [
    "FEMININO_LIGA", "FEMININO_NOME", "RUIDO", "SEPARADORES", "UF",
    "MatchResult", "encontrar_evento", "extrair_uf", "genero_conflita",
    "normalizar", "score_times", "split_times", "uf_conflita",
]
