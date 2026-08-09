"""Modelos normalizados das odds de referência (Pinnacle)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def american_para_decimal(price: float | int | None) -> float | None:
    """A Pinnacle devolve odds no formato americano (+256, -607)."""
    if price is None:
        return None
    p = float(price)
    if p == 0:
        return None
    return round(p / 100.0 + 1.0, 4) if p > 0 else round(100.0 / abs(p) + 1.0, 4)


@dataclass
class Market:
    """Um mercado com todos os seus lados — a unidade que o de-vig consome.

    Precisa de TODOS os lados: o de-vig soma as probabilidades implícitas pra
    achar a margem, então um mercado pela metade produziria odd justa errada.
    """

    key: str                       # "h2h", "totals:2.5", "btts", "corners:10.5"...
    label: str                     # descrição legível
    outcomes: dict[str, float] = field(default_factory=dict)   # lado -> odd decimal
    period: int = 0                # 0 = jogo todo, 1 = 1º tempo

    @property
    def completo(self) -> bool:
        return len(self.outcomes) >= 2 and all(o > 1.0 for o in self.outcomes.values())


@dataclass
class Matchup:
    """Um jogo na Pinnacle, com os mercados que conseguimos mapear.

    `sport` decide como os blocos de preço são traduzidos: a Pinnacle usa os
    mesmos `type` ("total", "spread") para tudo, mas o significado muda —
    `total` no futebol é gol, no tênis é game, no basquete é ponto.
    """

    id: int
    league: str
    home_team: str
    away_team: str
    commence_time: datetime | None
    sport: str = "soccer"          # "soccer" | "tennis" | "basketball"
    markets: dict[str, Market] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return f"{self.home_team} vs {self.away_team}"

    def get(self, key: str) -> Market | None:
        return self.markets.get(key)
