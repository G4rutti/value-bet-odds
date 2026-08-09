"""Scraper da CasaDeAposta.

Etapa 0 — Reconhecimento (resultado)
────────────────────────────────────
O domínio real é `casadeapostas.bet.br` (plural — o levantamento original
chutou o singular e o DNS nem resolvia). A API de odds ao vivo do site é
first-party (`/api/odds/games`, plataforma própria estilo BookmakerNext), mas
os cards "SUPER ODDS" da home **não vêm de lá**: vêm de um CMS separado,

    GET https://zizy-cms.casadeapostas.tv/api/home/widget/promocoes?userId=0&lg=1

achado em 2026-08-04 por captura de XHR em browser real (ver
`CASAS-PENDENTES.md` seção 0). `userId` é decorativo — `0` funciona tão bem
quanto o id de uma conta logada, e a rota não pede cookie nenhum.

Cada item de `sports` é um combo de bet-builder curado editorialmente (2
pernas, ex. "Resultado: Juventude ou empate" + "Total de gols: Mais de 2.5"),
com as duas odds já juntas — mesmo formato que o `smart-picks` da Betano e o
array `boosts` da Altenar. Não tem odd de mercado único: só combo.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import Offer
from .scraper import ScraperError

log = logging.getLogger(__name__)

CASA = "CasaDeAposta"
CMS_URL = "https://zizy-cms.casadeapostas.tv/api/home/widget/promocoes"

_EVENT_ID_RE = re.compile(r"/event/(\d+)")


def _valido_ate(data_str: str | None) -> str | None:
    """`championship.date` vem "AAAA-MM-DD HH:MM:SS" em horário de Brasília."""
    if not data_str:
        return None
    try:
        dt = datetime.strptime(data_str, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    return dt.strftime("%Y-%m-%dT%H:%M:%S-03:00")


class CasaDeApostaScraper:
    """Busca os combos "SUPER ODDS" do widget de promoções da CasaDeAposta."""

    def __init__(self, session: "curl_requests.AsyncSession | None" = None) -> None:
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "CasaDeApostaScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers={"accept": "application/json"},
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str, params: dict[str, Any]) -> Any:
        assert self._session is not None, "use o scraper como context manager"
        try:
            r = await self._session.get(url, params=params)
        except RequestException as exc:
            raise ScraperError(f"falha de rede no widget de promoções: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"widget de promoções respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"widget de promoções não devolveu JSON: {exc}") from exc

    async def scrape(self) -> list[Offer]:
        dados = await self._get_json(CMS_URL, {"userId": 0, "lg": 1})

        ofertas = [o for item in (dados.get("sports") or []) if (o := self._parse_item(item))]
        log.info("%s: %d oferta(s)", CASA, len(ofertas))
        return ofertas

    @staticmethod
    def _parse_item(item: dict[str, Any]) -> Offer | None:
        ev = item.get("event") or {}
        original, boosted = ev.get("originalOdds"), ev.get("boostedOdds")
        if not original or not boosted:
            return None
        try:
            odd_original, odd_boost = float(original), float(boosted)
        except (TypeError, ValueError):
            return None
        if odd_boost <= odd_original:
            return None

        game_link = ev.get("gameLink") or ""
        match = _EVENT_ID_RE.search(game_link)
        if not match:
            # Sem id de evento não dá pra montar `offer_id` estável nem
            # revalidar depois — melhor descartar do que gravar algo frágil.
            return None
        evento_id = match.group(1)

        # Revalidado em 2026-08-05: o payload real traz até TRÊS pernas
        # (option/option1/option2), não duas. Faltando a terceira, o
        # `mercado` gravado não é a combinada real que a odd precifica —
        # exatamente a classe "mercado restrito casado com referência mais
        # ampla" que já custou o falso +141,8% do Mirassol, só que aqui do
        # lado da CAPTURA (perna real omitida) em vez do parsing.
        pernas = [
            f"{ev.get(opt_key)}: {ev.get(choice_key)}"
            for opt_key, choice_key in (
                ("option", "choice"), ("option1", "choice1"), ("option2", "choice2"))
            if ev.get(opt_key) and ev.get(choice_key)
        ]
        if not pernas:
            return None

        team1, team2 = ev.get("team1"), ev.get("team2")
        evento = f"{team1} - {team2}" if team1 and team2 else evento_id
        championship = item.get("championship") or {}

        return Offer(
            casa=CASA,
            fonte="casadeaposta_combo",
            evento_id=evento_id,
            evento=evento,
            mercado=" + ".join(pernas),
            odd_original=odd_original,
            odd_boost=odd_boost,
            url=game_link,
            liga=championship.get("label"),
            valido_ate=_valido_ate(championship.get("date")),
            inicio_evento=_valido_ate(championship.get("date")),
            boost_pct=round((odd_boost / odd_original - 1) * 100, 1),
        )


async def scrape_offers() -> list[Offer]:
    async with CasaDeApostaScraper() as scraper:
        return await scraper.scrape()
