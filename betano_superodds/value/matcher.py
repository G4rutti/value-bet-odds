"""Casamento entre o evento da super odd e o evento na API de odds.

Os nomes divergem entre as fontes ("Flamengo" vs "CR Flamengo", "São Paulo" vs
"Sao Paulo FC", "Athletico-PR" vs "Athletico Paranaense"), então o match é
fuzzy nos dois times + conferência de data. Abaixo do score mínimo devolve
None: comparar odd de jogo errado é pior do que não comparar.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz

from . import config
from .models import Matchup

log = logging.getLogger(__name__)

# Sufixos/prefixos de clube que só adicionam ruído ao comparar nomes.
#
# A segunda linha de prefixos (sk, fk, ks…) entrou depois de medir: "SK Brann"
# tirava 76.9 contra "Brann" e era rejeitado por 3 pontos. São iniciais de
# associação esportiva escandinava/eslava/alemã que a Pinnacle simplesmente
# omite.
RUIDO = re.compile(
    r"\b(fc|cf|sc|ac|ec|cr|se|aa|ca|afc|cfc|club|clube|futebol|football|"
    r"atletico|atlético|deportivo|sporting|society|team|women|feminino|"
    r"sk|fk|ks|bk|if|nk|hk|bsc|vfb|vfl|tsv|fsv|sv|"
    r"u\d{2}|jr|junior)\b",
    re.IGNORECASE,
)

# Sufixo de unidade federativa: "Athletico-PR", "América-MG", "Vitória BA".
# A Pinnacle escreve o nome por extenso ("Athletico Paranaense") ou sem UF
# nenhuma ("Vitoria"), então manter a sigla no nome só afunda o score.
#
# ⚠️ Retirar a UF é justamente o que apaga a diferença entre Botafogo-SP e
# Botafogo-RJ. Por isso ela é EXTRAÍDA antes de sumir, e `uf_conflita` usa o
# que foi extraído como veto. Nunca remova uma sem a outra.
UF = re.compile(
    r"[-\s/]\s*(pr|mg|sp|rj|rs|ba|ce|pe|go|mt|ms|pa|df|al|pb|rn|se|pi|ma|to|"
    r"ac|ro|rr|ap|am|es)\b",
    re.IGNORECASE,
)

# Marcador de time feminino no nome da oferta: "Atlanta Dream (F)".
FEMININO_NOME = re.compile(r"\(\s*f\s*\)|\bfeminin[oa]\b|\bwomen\b|\bfem\b",
                           re.IGNORECASE)
# ...e do lado da Pinnacle, que marca na LIGA, não no nome do time.
FEMININO_LIGA = re.compile(r"\bwomen\b|\bfeminin[oa]\b|\bwsl\b|\bnwsl\b|\bwnba\b",
                           re.IGNORECASE)

# Separadores usados pela Betano entre mandante e visitante.
SEPARADORES = (" - ", " vs ", " x ", " v ", " – ", " — ")


def extrair_uf(nome: str) -> str | None:
    """A sigla de estado do nome, se houver. É o veto contra homônimo."""
    m = UF.search(nome)
    return m.group(1).lower() if m else None


def normalizar(nome: str) -> str:
    """Minúsculas, sem acento, sem pontuação, sem UF e sem sufixo de clube."""
    sem_uf = UF.sub(" ", nome)
    sem_acento = unicodedata.normalize("NFKD", sem_uf)
    ascii_only = sem_acento.encode("ascii", "ignore").decode("ascii").lower()
    limpo = re.sub(r"[^a-z0-9\s]", " ", ascii_only)
    limpo = RUIDO.sub(" ", limpo)
    return re.sub(r"\s+", " ", limpo).strip()


def split_times(evento: str) -> tuple[str, str] | None:
    """Quebra "Flamengo - Palmeiras" nos dois nomes."""
    for sep in SEPARADORES:
        if sep in evento:
            casa, _, fora = evento.partition(sep)
            if casa.strip() and fora.strip():
                return casa.strip(), fora.strip()
    return None


def _score_nome(a: str, b: str) -> float:
    """Score 0-100 de um nome de time contra outro.

    `token_set_ratio` ao lado do `token_sort_ratio` porque as duas fontes
    escrevem o mesmo clube com granularidade diferente, e o `sort` pune token a
    mais como se fosse token errado: "Rangers" contra "Glasgow Rangers" tirava
    63.6, "Estudiantes LP" contra "Estudiantes de La Plata" tirava 75.7 — os
    dois abaixo do corte de 80, os dois o mesmo time. O `set` compara a
    interseção e resolve o caso subconjunto sem afrouxar nome de fato diferente
    ("Palmeiras" contra "Santos" continua no chão).
    """
    x, y = normalizar(a), normalizar(b)
    if not x or not y:
        # A normalização comeu o nome inteiro (ex.: um time chamado só "FC").
        # Sem texto pra comparar, não dá pra afirmar nada.
        return 0.0
    return max(fuzz.token_sort_ratio(x, y), fuzz.token_set_ratio(x, y))


def score_times(casa_a: str, fora_a: str, casa_b: str, fora_b: str) -> float:
    """Score 0-100 do par de times. Usa o pior dos dois lados.

    Média esconderia um lado muito errado — "Flamengo vs Palmeiras" contra
    "Flamengo vs Santos" tiraria ~75 na média e passaria no threshold. Com o
    mínimo, o lado ruim reprova o match inteiro, que é o comportamento seguro.
    """
    return min(_score_nome(casa_a, casa_b), _score_nome(fora_a, fora_b))


def uf_conflita(a: str, b: str) -> bool:
    """Os dois nomes declaram estados DIFERENTES?

    Botafogo-SP e Botafogo-RJ viram o mesmo texto depois de `normalizar`, e
    podem jogar no mesmo fim de semana — a janela de data não separa. Este é o
    veto que separa.

    Um lado sem UF não é conflito: a Pinnacle costuma escrever "Vitoria" seco,
    e exigir a sigla nos dois lados devolveria a perda de cobertura que a
    normalização acabou de resolver.
    """
    ua, ub = extrair_uf(a), extrair_uf(b)
    return ua is not None and ub is not None and ua != ub


def genero_conflita(evento: str, liga: str | None) -> bool:
    """Oferta de time feminino contra jogo masculino (ou o contrário)?

    Depois que `RUIDO` come "women"/"feminino", "Corinthians (F)" e
    "Corinthians" ficam idênticos — e são jogos diferentes, em competições
    diferentes, com odds que não têm nada a ver. As casas marcam no NOME do
    time; a Pinnacle marca no nome da LIGA. Por isso a comparação é assimétrica.
    """
    return bool(FEMININO_NOME.search(evento)) != bool(
        FEMININO_LIGA.search(liga or ""))


@dataclass
class MatchResult:
    matchup: Matchup
    score: float
    invertido: bool  # mandante/visitante trocados entre as fontes

    @property
    def event_id(self) -> str:
        return self.matchup.id


def _parse_data(valor: str | datetime | None) -> datetime | None:
    if valor is None:
        return None
    if isinstance(valor, datetime):
        dt = valor
    else:
        try:
            dt = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
        except ValueError:
            return None
    # Data sem fuso é tratada como local, pra comparar com o UTC da API.
    return dt.astimezone(timezone.utc) if dt.tzinfo else dt.astimezone()


def encontrar_evento(
    evento: str,
    data: str | datetime | None,
    matchups: list[Matchup],
    *,
    min_score: float | None = None,
    max_horas: float | None = None,
) -> MatchResult | None:
    """Acha o jogo correspondente. None se nada passar do score mínimo."""
    min_score = config.MATCH_MIN_SCORE if min_score is None else min_score
    max_horas = config.MATCH_MAX_HORAS if max_horas is None else max_horas

    times = split_times(evento)
    if not times:
        log.debug("não consegui separar os times de %r", evento)
        return None
    casa, fora = times

    quando = _parse_data(data)
    melhor: MatchResult | None = None

    for fx in matchups:
        if not fx.home_team or not fx.away_team:
            continue

        # Confere a data antes do fuzzy: descarta o mesmo confronto de outra rodada.
        if quando is not None and fx.commence_time is not None:
            alvo = fx.commence_time
            alvo = alvo.astimezone(timezone.utc) if alvo.tzinfo else alvo.replace(tzinfo=timezone.utc)
            if abs((alvo - quando).total_seconds()) > max_horas * 3600:
                continue

        # Feminino x masculino do mesmo clube passam por idênticos depois da
        # normalização. Veto antes do fuzzy: não é questão de score.
        if genero_conflita(evento, fx.league):
            continue

        direto = score_times(casa, fora, fx.home_team, fx.away_team)
        # Algumas fontes invertem mandante/visitante — testa os dois sentidos.
        trocado = score_times(casa, fora, fx.away_team, fx.home_team)
        score, invertido = (direto, False) if direto >= trocado else (trocado, True)

        # O veto de UF é sobre o par EFETIVAMENTE usado: comparar cruzado
        # acusaria conflito em "Botafogo-SP vs América-MG", que é um jogo só.
        par = ((casa, fx.away_team), (fora, fx.home_team)) if invertido else (
            (casa, fx.home_team), (fora, fx.away_team))
        if any(uf_conflita(x, y) for x, y in par):
            log.debug("descartado por UF divergente: %r x %r", evento, fx.display_name)
            continue

        if score >= min_score and (melhor is None or score > melhor.score):
            melhor = MatchResult(matchup=fx, score=score, invertido=invertido)

    if melhor is None:
        log.debug("sem match para %r (min_score=%s)", evento, min_score)
    else:
        log.debug("match %r -> %r (score=%.1f%s)", evento, melhor.matchup.display_name,
                  melhor.score, ", invertido" if melhor.invertido else "")
    return melhor
