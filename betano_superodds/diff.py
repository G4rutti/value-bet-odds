"""Comparação entre o snapshot recém-raspado e o último estado salvo."""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

from . import config
from .models import Offer, minutos_ate_inicio

log = logging.getLogger(__name__)


@dataclass
class OfferChange:
    """Uma oferta que continua no ar mas teve a odd reajustada."""

    offer: Offer
    odd_boost_anterior: float | None
    odd_original_anterior: float | None

    def describe(self) -> str:
        antes = f"{self.odd_boost_anterior:.2f}" if self.odd_boost_anterior else "?"
        return f"{self.offer.evento} — {self.offer.mercado} — boost {antes} → {self.offer.odd_boost:.2f}"


@dataclass
class DiffResult:
    novas: list[Offer] = field(default_factory=list)
    alteradas: list[OfferChange] = field(default_factory=list)
    expiradas: list[dict] = field(default_factory=list)
    inalteradas: list[Offer] = field(default_factory=list)
    # Ofertas ativas de casas que não foram raspadas neste ciclo (rodízio).
    # Não sumiram — ninguém olhou. Ficam intocadas no banco.
    fora_do_ciclo: list[dict] = field(default_factory=list)

    @property
    def has_changes(self) -> bool:
        return bool(self.novas or self.alteradas or self.expiradas)

    @property
    def total_ativas(self) -> int:
        return (len(self.novas) + len(self.alteradas)
                + len(self.inalteradas) + len(self.fora_do_ciclo))


def _ja_comecou(dado: dict | Offer, agora: datetime | None = None) -> bool:
    """O jogo já começou / a promoção já acabou?

    Rede de segurança para o escopo de sondagem: uma oferta que não foi olhada
    fica pendurada como ativa até a próxima visita àquele evento, e com o
    rodízio isso pode levar dezenas de minutos. Uma oferta vencida está morta
    independentemente de alguém ter ido conferir — sem esta checagem ela
    continuaria sendo avaliada e podia virar alerta de um jogo em andamento.

    Aceita tanto linha do banco quanto `Offer` porque agora vale para os dois
    lados do diff: o teste antigo só rodava sobre o que SUMIU do snapshot, e
    por isso oferta nova de jogo em andamento entrava lisa (a Betano publica
    combo de tênis com o jogo rolando — foi assim que 7 dos 50 alertas medidos
    saíram depois do apito inicial).

    Sem data legível, não expira: chutar seria pior que esperar.
    """
    faltam = minutos_ate_inicio(dado, agora)
    return faltam is not None and faltam <= 0


def _foi_olhado(row: dict, casas_raspadas: set[str] | None,
                escopo_detalhe: set[tuple[str, str]] | None) -> bool:
    """Este ciclo olhou no lugar onde esta oferta apareceria?

    Só quem foi olhado pode expirar. Uma oferta ausente porque ninguém foi
    conferir não "acabou" — e tratá-la como expirada a faz renascer no ciclo
    seguinte, poluindo o histórico e furando a dedup de alerta (a oferta volta
    como "nova" e realerta).
    """
    if casas_raspadas is not None and row.get("casa") not in casas_raspadas:
        return False   # casa fora do rodízio deste ciclo

    fonte = row.get("fonte") or ""
    if escopo_detalhe is not None and fonte.endswith(config.SUFIXO_FONTE_DETALHE):
        # Oferta que só existe no detalhe do evento: vale apenas se o detalhe
        # daquele evento foi realmente pedido nesta visita.
        return (row.get("casa"), str(row.get("evento_id"))) in escopo_detalhe

    return True


def diff_offers(previous: dict[str, dict], current: list[Offer],
                casas_raspadas: set[str] | None = None,
                escopo_detalhe: set[tuple[str, str]] | None = None) -> DiffResult:
    """Compara o estado salvo com o snapshot novo.

    `previous` mapeia offer_id -> linha do banco (ofertas ativas).
    `current` é o que o scraper acabou de capturar.

    `casas_raspadas` — casas que este ciclo realmente visitou.
    `escopo_detalhe` — pares `(casa, evento_id)` cujo detalhe foi pedido.

    Os dois existem pelo mesmo motivo, em dois níveis: o ciclo não olha tudo.
    O rodízio pula casas inteiras, e dentro de cada casa o orçamento de
    detalhes cobre só uma fatia da listagem. Sem esse escopo, tudo que não foi
    olhado viraria "expirada" e renasceria depois. `None` nos dois mantém o
    comportamento antigo (tudo foi raspado).
    """
    result = DiffResult()
    seen: set[str] = set()
    # Descarte por jogo em andamento não logava nada (era um `continue` mudo)
    # — foi isso que fez uma família inteira de oferta (tênis "Total de
    # Games" da Betano) sumir do alerta sem nenhuma pista no log. Conta por
    # (casa, fonte) — a granularidade que `Offer` de fato carrega — e loga
    # um resumo no fim do ciclo, não linha a linha (o volume pode ser alto).
    descartadas_em_andamento: Counter[tuple[str, str]] = Counter()

    for offer in current:
        offer_id = offer.offer_id
        # Jogo em andamento não é oferta: a casa pode continuar publicando (a
        # Betano publica), mas não há o que apostar naquele preço. Sai do
        # snapshot antes de qualquer classificação — se já existia no banco,
        # cai no laço de baixo e expira; se é nova, simplesmente não entra.
        if _ja_comecou(offer):
            descartadas_em_andamento[(offer.casa, offer.fonte)] += 1
            continue
        seen.add(offer_id)
        before = previous.get(offer_id)

        if before is None:
            result.novas.append(offer)
        elif before.get("content_hash") != offer.content_hash:
            result.alteradas.append(
                OfferChange(
                    offer=offer,
                    odd_boost_anterior=before.get("odd_boost"),
                    odd_original_anterior=before.get("odd_original"),
                )
            )
        else:
            result.inalteradas.append(offer)

    for offer_id, row in previous.items():
        if offer_id in seen:
            continue
        if _ja_comecou(row) or _foi_olhado(row, casas_raspadas, escopo_detalhe):
            result.expiradas.append(row)
        else:
            result.fora_do_ciclo.append(row)

    if descartadas_em_andamento:
        total = sum(descartadas_em_andamento.values())
        detalhe = ", ".join(f"{casa}/{fonte}: {n}"
                            for (casa, fonte), n in descartadas_em_andamento.most_common())
        log.info("descartadas %d oferta(s) com jogo já em andamento (%s)", total, detalhe)

    return result
