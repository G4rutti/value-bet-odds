"""Confere, no instante do alerta, se a odd ainda está lá.

Por que isto existe
───────────────────
Em 2026-08-04 um alerta da BetGorillas (escanteios Mais de 9.5 @ 2.42) saiu às
12:21 com odd capturada às 11:29. No site a linha já era Mais de 9 @ 1.70. O
usuário entrou no segundo em que a mensagem chegou e não achou a aposta.

A defasagem é estrutural e composta:

    rodízio de casas (3 de 10 por ciclo)  ×  rodízio da janela de detalhes

A BetGorillas foi raspada 11:46, 12:00 e 12:14 e mesmo assim aquela oferta não
atualizou, porque o evento dela só cai na janela `@36` — vista 11:27, próxima
só 12:33. São 64 minutos sem refresh. Nenhum ajuste de frequência resolve isso
sem estourar o orçamento de request; o que resolve é perguntar de novo, uma vez,
na hora de mandar a mensagem.

Só a Altenar
────────────
A Betano é raspada **todo ciclo** — o dado dela nunca passa de ~3 minutos, e
revalidar custaria uma listagem inteira por esporte pra confirmar algo que já
está fresco. Quem sofre com defasagem é o rodízio da Altenar, e é lá que a
revalidação age. Pra Betano a resposta é `indeterminada`, e quem segura o
extremo é `IDADE_MAX_PARA_ALERTA`.

Indeterminada não bloqueia
──────────────────────────
Falha de rede não pode virar alerta perdido: value bet é raro e nem sempre
volta. Só bloqueia o que se sabe ter mudado ou sumido.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from . import config
from .esportiva import EsportivaScraper, ScraperError

log = logging.getLogger("betano.revalidacao")


@dataclass(frozen=True)
class Revalidacao:
    status: str            # "confirmada" | "mudou" | "sumiu" | "indeterminada"
    odd: float | None
    motivo: str = ""

    @property
    def bloqueia(self) -> bool:
        """Só o que se SABE estar errado impede o envio."""
        return self.status in ("mudou", "sumiu")


def _casa_altenar(nome: str | None) -> config.CasaAltenar | None:
    return next((c for c in config.CASAS_ALTENAR if c.nome == nome), None)


async def revalidar_oferta(row: dict) -> Revalidacao:
    """1 request na casa; devolve o veredito sobre a odd guardada."""
    guardada = row.get("odd_boost")
    casa = _casa_altenar(row.get("casa"))
    if casa is None:
        return Revalidacao("indeterminada", None, "casa sem revalidação")
    if not row.get("evento_id") or not row.get("mercado") or guardada is None:
        return Revalidacao("indeterminada", None, "linha incompleta")

    try:
        async with EsportivaScraper(casa) as sc:
            atual = await sc.revalidar(str(row["evento_id"]), str(row["mercado"]))
    except (ScraperError, ValueError, OSError) as exc:
        # Não dá pra afirmar que sumiu — a request é que falhou.
        return Revalidacao("indeterminada", None, f"falha ao revalidar: {exc}")

    if atual is None:
        # Inclui o caso que motivou tudo isto: a casa mexeu na LINHA
        # (escanteios 9.5 → 9). O rótulo deixa de bater, e com razão — não é
        # a mesma aposta.
        return Revalidacao("sumiu", None, "mercado não está mais na casa")

    if abs(atual - float(guardada)) <= config.TOLERANCIA_REVALIDACAO:
        return Revalidacao("confirmada", atual)

    return Revalidacao("mudou", atual, f"odd {guardada:.2f} → {atual:.2f}")
