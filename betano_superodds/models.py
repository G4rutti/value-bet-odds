"""Modelo normalizado de oferta, comum a todas as fontes de scraping."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def millis_to_iso(millis: int | None) -> str | None:
    """Converte epoch em ms (formato que a Betano usa) pra ISO local."""
    if not millis:
        return None
    try:
        return datetime.fromtimestamp(millis / 1000, tz=timezone.utc).astimezone().isoformat(
            timespec="seconds"
        )
    except (ValueError, OSError, OverflowError):
        return None


def to_utc_iso(valor: Any) -> str | None:
    """Normaliza qualquer formato de data das casas para ISO **UTC** canônico.

    Existe porque cada fonte manda de um jeito: a Betano manda epoch em ms, a
    Altenar manda ISO com `Z` em alguns campos e epoch em outros, a
    CasaDeAposta monta a string com `-03:00` na mão. Guardar isso cru numa
    coluna torna impossível comparar ou ordenar por horário em SQL — e é
    exatamente disso que a fila de avaliação precisa.

    A saída é sempre `...+00:00`, então a comparação lexicográfica no SQLite
    equivale à comparação cronológica. Data sem timezone é lida como LOCAL,
    mesma convenção de `diff._ja_comecou` e `value.matcher`.
    """
    if valor is None or valor == "":
        return None
    if isinstance(valor, datetime):
        quando = valor if valor.tzinfo else valor.astimezone()
        return quando.astimezone(timezone.utc).isoformat(timespec="seconds")
    if isinstance(valor, (int, float)):
        return to_utc_iso(millis_to_iso(int(valor)))
    try:
        quando = datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None
    if quando.tzinfo is None:
        quando = quando.astimezone()
    return quando.astimezone(timezone.utc).isoformat(timespec="seconds")


def minutos_ate_inicio(dado: Any, agora: datetime | None = None) -> float | None:
    """Quantos minutos faltam para a bola rolar. Negativo = já começou.

    Ponto único da resposta "quanto falta": o diff, a fila de avaliação, a
    guarda do alerta e o texto do Telegram têm que concordar, e concordavam
    por acidente antes disto.

    `valido_ate` é o fallback, não a fonte: ele mistura fim-de-promoção com
    início de jogo (na Altenar é `boostInfo.endDate or startDate`), então erra
    nos dois sentidos. Só vale para as ofertas gravadas antes de
    `inicio_evento` existir.

    `None` quando não há data legível — quem chama decide o que fazer com isso,
    e todo mundo decide o mesmo: não bloquear. Chutar seria pior.
    """
    if isinstance(dado, dict):
        bruto = dado.get("inicio_evento") or dado.get("valido_ate")
    else:
        bruto = getattr(dado, "inicio_evento", None) or getattr(dado, "valido_ate", None)

    iso = to_utc_iso(bruto)
    if iso is None:
        return None
    quando = datetime.fromisoformat(iso)
    agora = agora or datetime.now(timezone.utc)
    if agora.tzinfo is None:
        agora = agora.astimezone()
    return (quando - agora.astimezone(timezone.utc)).total_seconds() / 60


def calcular_alert_hash(offer_id: str) -> str:
    """Chave de dedup de alerta. Existe como função porque o alerta trabalha
    com a linha do banco (dict), não com o `Offer` — e as duas pontas precisam
    chegar exatamente no mesmo hash.

    Deliberadamente NÃO inclui `odd_boost`: a odd sozinha oscilando (ou
    caindo devagar) gerava um `alert_hash` novo a cada movimento e virava
    flood da mesma oferta várias vezes por hora. A identidade do *alerta* é
    a identidade da *oferta* — quem decide se um novo envio é justificado é
    `Storage.ja_alertou`, comparando o edge contra o melhor já alertado."""
    return hashlib.sha1(f"{offer_id}".encode("utf-8")).hexdigest()[:16]


@dataclass
class MercadoCasa:
    """Um mercado COMPLETO de uma casa, com todas as seleções e preços.

    Existe por causa dos props (chutes ao gol, cartões, artilheiro): a Pinnacle
    não publica nenhum deles, então a única referência possível é o que as
    outras casas cobram pelo mesmo mercado. Só que de-vig precisa de todos os
    lados (`value.models.Market.completo`), e a oferta guarda apenas a seleção
    turbinada — daí esta tabela paralela.

    Não custa request novo: o `GetEventDetails` já devolve o evento inteiro, e
    o parser descartava tudo que não fosse perna de boost.
    """

    casa: str
    evento_id: str
    market_id: str
    market_nome: str
    selecao: str
    preco: float
    capturado_em: str = field(default_factory=_now_iso)
    # Identidade do evento — nome, kickoff (ISO UTC) e liga. `None` por
    # default: quem grava sem preenchê-los (não deveria haver ninguém, mas se
    # houver) não quebra. Sem isto o `evento_id` do pool não casa com o de
    # outra casa (ver comentário de `mercados_casa` em `storage.SCHEMA`).
    evento: str | None = None
    inicio_evento: str | None = None
    liga: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "casa": self.casa,
            "evento_id": self.evento_id,
            "market_id": self.market_id,
            "market_nome": self.market_nome,
            "selecao": self.selecao,
            "preco": self.preco,
            "capturado_em": self.capturado_em,
            "evento": self.evento,
            "inicio_evento": self.inicio_evento,
            "liga": self.liga,
        }


@dataclass
class Offer:
    """Uma oferta turbinada capturada.

    `offer_id` é a identidade *estável* da oferta: fonte + evento + mercado.
    Ele deliberadamente NÃO inclui as odds — senão um reajuste de boost viraria
    "oferta velha sumiu + oferta nova apareceu" e o diff nunca conseguiria
    reportar "mudou de odd", que é justamente um dos casos de interesse.
    A variação de preço é rastreada em `content_hash`.
    """

    fonte: str  # "mr12" | "smartpick" | "esportiva_1x2" | "esportiva_boost"
    evento_id: str
    evento: str
    mercado: str
    odd_original: float | None
    odd_boost: float
    url: str
    liga: str | None = None
    valido_ate: str | None = None
    # Kickoff de verdade, em ISO UTC. Fica FORA do `content_hash`: a casa
    # remarcando o jogo não é "a oferta mudou de odd", e tratar como mudança
    # jogaria a oferta de volta na fila de avaliação sem motivo.
    inicio_evento: str | None = None
    boost_pct: float | None = None
    casa: str = "Betano"    # nome da casa, exibido no alerta
    # Teto de aposta imposto pela casa. Boost muito alto costuma vir com limite
    # baixo — sem isso o stake sugerido seria impossível de executar.
    limite_aposta: float | None = None
    capturado_em: str = field(default_factory=_now_iso)

    def __post_init__(self) -> None:
        # Normaliza na entrada, não na gravação: cada casa manda o kickoff num
        # formato (epoch ms, ISO com Z, ISO com -03:00) e a coluna é comparada
        # e ordenada em SQL. Misturar offsets na mesma tabela faria a
        # comparação lexicográfica do SQLite mentir sobre quem começa antes.
        self.inicio_evento = to_utc_iso(self.inicio_evento)

    @property
    def offer_id(self) -> str:
        # A casa entra na identidade: duas casas podem ter o mesmo evento e o
        # mesmo mercado, e são ofertas distintas com odds distintas.
        base = f"{self.casa}|{self.fonte}|{self.evento_id}|{self.mercado}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    @property
    def content_hash(self) -> str:
        """Muda sempre que as odds mudam — é o gatilho de 'oferta alterada'."""
        base = f"{self.offer_id}|{self.odd_original}|{self.odd_boost}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    @property
    def alert_hash(self) -> str:
        """Identidade do *alerta*, que é coisa diferente do `content_hash`.

        O `content_hash` inclui `odd_original`, e tem que incluir: reajuste de
        preço pré-boost é uma alteração real e o diff precisa vê-la.

        Só que `odd_original` **não aparece no alerta** — o que o usuário lê é
        odd turbinada, edge, justa e stake. Usar `content_hash` como chave de
        dedup fazia a odd pré-boost oscilar sozinha, o hash mudar e o mesmo
        alerta, byte por byte, sair de novo. Aconteceu com `dc32e1e88f69cf16`
        (12:07 e 12:11, edge 8.46 nos dois) e com mais três ofertas em 24h.

        `odd_boost` também não entra mais na chave (regressão de 2026-08-07):
        uma oferta cuja odd derretia devagar (4.22 → 4.13 → 4.18 → 4.07) gerava
        um `alert_hash` novo a cada movimento e virava flood da mesma oferta.
        `alert_hash` agora é 1:1 com `offer_id` — é `Storage.ja_alertou` quem
        decide se um segundo envio se justifica, comparando o edge atual
        contra o melhor já alertado.
        """
        return calcular_alert_hash(self.offer_id)

    @property
    def ganho_pct(self) -> float | None:
        """Quanto a odd turbinada supera a original, em %."""
        if not self.odd_original:
            return None
        return round((self.odd_boost / self.odd_original - 1) * 100, 1)

    def describe(self) -> str:
        original = f"{self.odd_original:.2f}" if self.odd_original else "?"
        ganho = f" (+{self.ganho_pct:.1f}%)" if self.ganho_pct is not None else ""
        return f"{self.evento} — {self.mercado} — {original} → {self.odd_boost:.2f}{ganho}"

    def to_row(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "content_hash": self.content_hash,
            "casa": self.casa,
            "fonte": self.fonte,
            "evento_id": self.evento_id,
            "evento": self.evento,
            "liga": self.liga,
            "mercado": self.mercado,
            "odd_original": self.odd_original,
            "odd_boost": self.odd_boost,
            "boost_pct": self.boost_pct,
            "valido_ate": self.valido_ate,
            "inicio_evento": self.inicio_evento,
            "url": self.url,
            "limite_aposta": self.limite_aposta,
            "capturado_em": self.capturado_em,
        }
