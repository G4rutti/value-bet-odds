"""Migrado pro pacote compartilhado `odds_service` — ver Projetos-pessoais/odds-service.

Este módulo é um shim de compatibilidade: todo o resto do value bet monitor
continua importando `.models` normalmente, sem saber que o código mora em
outro pacote agora.
"""

from odds_service.models import Market, Matchup, american_para_decimal

__all__ = ["Market", "Matchup", "american_para_decimal"]
