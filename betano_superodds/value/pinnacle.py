"""Migrado pro pacote compartilhado `odds_service` — ver Projetos-pessoais/odds-service.

Shim de compatibilidade: `pipeline.py` e `avaliar.py` continuam importando
`.pinnacle` normalmente. `PinnacleScraper` é síncrono, assinatura idêntica à
original — nenhum outro arquivo do monitor precisa mudar.
"""

from odds_service.pinnacle import (
    STATS_JOGADOR,
    PinnacleError,
    PinnacleScraper,
    SOCCER_SPORT_ID,
)

__all__ = ["STATS_JOGADOR", "PinnacleError", "PinnacleScraper", "SOCCER_SPORT_ID"]
