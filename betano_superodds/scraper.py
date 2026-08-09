"""Scraper das Super Odds da Betano.

A Betano (plataforma Kaizen) expõe uma API JSON pública em `/api/...` — sem
login, sem cookie de sessão, sem token. Ofertas turbinadas aparecem por duas
vias distintas, e o scraper cobre as duas:

1. Mercado `MR12` ("Resultado Final SuperOdds", typeId 2850). Na listagem de
   jogos ele *substitui* o `MRES` normal, então a listagem só entrega a odd
   turbinada. A odd original vem da página do evento, que traz `MRES` e `MR12`
   lado a lado. A página do evento também confirma a promoção via
   `marketOffersData.marketOffers[<marketId>]` com `offerTypeId: 1000`.

2. `/api/smart-picks` — combos de Criar Aposta turbinados. Esses já vêm com
   `originalPrice`, `boostedPrice` e `percentageOffer` no próprio payload, sem
   precisar de request extra.

Transporte: curl_cffi. Ver a nota sobre fingerprint TLS em `config.IMPERSONATE`.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from typing import Any, Iterator

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import Offer, millis_to_iso

log = logging.getLogger(__name__)


class ScraperError(RuntimeError):
    """Falha que impediu a captura por completo (rede fora, API mudou, etc.)."""


def _event_url(event_name: str, event_id: str) -> str:
    """Monta o link do evento no formato da Betano.

    O smart-picks só devolve o eventId, não a URL. `/odds/{id}/` dá 404, mas
    `/match-odds/{slug}/{id}/` redireciona pro canônico mesmo com o slug errado —
    então basta um slug aproximado a partir do nome do evento.
    """
    normalized = unicodedata.normalize("NFKD", event_name)
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii").lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only).strip("-") or "evento"
    return f"{config.BASE_URL}/match-odds/{slug}/{event_id}/"


def _walk_events(payload: Any) -> Iterator[dict]:
    """Varre um payload de listagem e devolve todo evento com mercados.

    Feito na marra em vez de indexar `data.blocks[i].events` porque a Betano
    muda o aninhamento entre tipos de página; o que é estável é o formato do
    objeto de evento.
    """
    if isinstance(payload, dict):
        if "markets" in payload and "participants" in payload and "id" in payload:
            yield payload
            return
        for value in payload.values():
            yield from _walk_events(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _walk_events(item)


def _find_markets(payload: Any) -> Iterator[dict]:
    if isinstance(payload, dict):
        if "selections" in payload and "type" in payload:
            yield payload
            return
        for value in payload.values():
            yield from _find_markets(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _find_markets(item)


class BetanoScraper:
    """Cliente da API pública da Betano, focado em ofertas turbinadas."""

    def __init__(self, session: "curl_requests.AsyncSession | None" = None) -> None:
        self._session = session
        self._owns_session = session is None
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY)

    async def __aenter__(self) -> "BetanoScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers=config.HEADERS,
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
                max_clients=config.MAX_CONCURRENCY,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, path: str) -> dict:
        assert self._session is not None, "use o scraper como async context manager"
        url = f"{config.BASE_URL}{path}"
        async with self._semaphore:
            # Jitter pra não desenhar um padrão de polling perfeitamente regular.
            await asyncio.sleep(random.uniform(*config.REQUEST_JITTER))
            response = await self._session.get(url, allow_redirects=True)

        if response.status_code != 200:
            raise ScraperError(f"{path} respondeu HTTP {response.status_code}")
        if "json" not in response.headers.get("content-type", ""):
            # 403 do WAF e rotas removidas voltam como HTML — vale distinguir.
            raise ScraperError(f"{path} respondeu HTML em vez de JSON (rota mudou ou bloqueio?)")
        return response.json()

    # ------------------------------------------------------------------
    # Fonte 1: mercado MR12 nas listagens de jogos
    # ------------------------------------------------------------------

    async def _scrape_mr12_for_sport(self, sport: config.Sport) -> list[Offer]:
        path = f"/api/sport/{sport.slug}/jogos-de-hoje/?req={config.REQ_LISTING}"
        payload = await self._get_json(path)

        candidates: list[tuple[dict, dict]] = []
        for event in _walk_events(payload):
            for market in event.get("markets") or []:
                if market.get("type") == config.SUPERODDS_MARKET_TYPE:
                    candidates.append((event, market))

        if not candidates:
            log.info("%s: nenhum MR12 na listagem", sport.name)
            return []

        log.info("%s: %d evento(s) com SuperOdds, buscando odds originais", sport.name, len(candidates))
        results = await asyncio.gather(
            *(self._build_mr12_offers(event, market) for event, market in candidates),
            return_exceptions=True,
        )

        offers: list[Offer] = []
        for (event, _), result in zip(candidates, results):
            if isinstance(result, Exception):
                # Um evento problemático não pode derrubar o ciclo inteiro.
                log.warning("falha lendo evento %s: %s", event.get("name"), result)
                continue
            offers.extend(result)
        return offers

    async def _build_mr12_offers(self, event: dict, listing_market: dict) -> list[Offer]:
        """Cruza o MR12 com o MRES da página do evento pra achar a odd original."""
        event_url = event.get("url") or ""
        originals: dict[str, float] = {}
        confirmed = False

        if event_url:
            try:
                detail = await self._get_json(f"/api{event_url}?req={config.REQ_EVENT}")
                originals, confirmed = self._extract_originals(detail, listing_market.get("id"))
            except (RequestException, ScraperError, ValueError) as exc:
                # Sem a página do evento ainda dá pra registrar a odd turbinada;
                # só ficamos sem a original. Melhor do que perder a oferta.
                log.warning("sem detalhe do evento %s: %s", event.get("name"), exc)

        if confirmed:
            log.debug("evento %s: SuperOdds confirmada via offerTypeId", event.get("name"))

        evento = event.get("name") or " - ".join(
            p.get("name", "") for p in event.get("participants") or []
        )
        valido_ate = millis_to_iso(
            listing_market.get("marketCloseTimeMillis") or event.get("startTime")
        )
        # Separado de `valido_ate` de propósito: aquele é o fechamento do
        # MERCADO (`marketCloseTimeMillis`), que não é o apito inicial. Quem
        # decide "faltam X minutos pro jogo" precisa do kickoff cru.
        inicio_evento = millis_to_iso(event.get("startTime"))
        full_url = f"{config.BASE_URL}{event_url}" if event_url else config.BASE_URL

        offers: list[Offer] = []
        for selection in listing_market.get("selections") or []:
            price = selection.get("price")
            if not price:
                continue
            outcome = selection.get("fullName") or selection.get("name") or "?"
            offers.append(
                Offer(
                    fonte="mr12",
                    evento_id=str(event.get("id")),
                    evento=evento,
                    liga=event.get("leagueName"),
                    mercado=f"Resultado Final: {outcome}",
                    odd_original=originals.get(str(selection.get("name"))),
                    odd_boost=float(price),
                    url=full_url,
                    valido_ate=valido_ate,
                    inicio_evento=inicio_evento,
                )
            )
        return offers

    @staticmethod
    def _extract_originals(detail: dict, superodds_market_id: Any) -> tuple[dict[str, float], bool]:
        """Devolve ({nome_da_selecao: odd_original}, promoção_confirmada)."""
        data = detail.get("data") or {}

        confirmed = False
        offers_by_market = ((data.get("marketOffersData") or {}).get("marketOffers")) or {}
        for offer in offers_by_market.get(str(superodds_market_id), []):
            if offer.get("offerTypeId") == config.SUPERODDS_OFFER_TYPE_ID:
                confirmed = True
                break

        originals: dict[str, float] = {}
        for market in _find_markets(data):
            if market.get("type") != config.BASE_MARKET_TYPE:
                continue
            for selection in market.get("selections") or []:
                name, price = selection.get("name"), selection.get("price")
                if name and price:
                    originals[str(name)] = float(price)
            break

        return originals, confirmed

    # ------------------------------------------------------------------
    # Fonte 2: smart-picks (combos turbinados)
    # ------------------------------------------------------------------

    async def _scrape_smart_picks_for_sport(self, sport: config.Sport) -> list[Offer]:
        include = "".join(f"&includeTypes={t}" for t in config.SMART_PICKS_INCLUDE_TYPES)
        payload = await self._get_json(f"/api/smart-picks?sportId={sport.sport_id}{include}")
        picks = (payload.get("data") or {}).get("smartPicks") or []

        offers: list[Offer] = []
        for pick in picks:
            data = pick.get("data") or {}
            market = data.get("market") or {}
            selections = market.get("selections") or []
            if not selections:
                continue

            selection = selections[0]
            boost = selection.get("boostedPrice")
            original = selection.get("originalPrice")
            # Picks sem boost (type 0/1/2) são sugestões comuns, não promoção.
            if not boost or not original or boost <= original:
                continue

            mercado = " + ".join(
                " ".join(part.get("text", "") for part in label.get("parts") or []).strip()
                for label in market.get("labels") or []
            ) or "Criar Aposta turbinada"

            evento = " - ".join(p.get("name", "") for p in data.get("participants") or [])
            event_id = str(data.get("eventId") or "")

            offers.append(
                Offer(
                    fonte="smartpick",
                    evento_id=event_id,
                    evento=evento or event_id,
                    liga=data.get("leagueName"),
                    mercado=mercado,
                    odd_original=float(original),
                    odd_boost=float(boost),
                    url=_event_url(evento or event_id, event_id),
                    valido_ate=millis_to_iso(data.get("eventStartDate")),
                    # Esta é a fonte que produziu TODOS os alertas pós-kickoff
                    # medidos (tênis): a Betano continua publicando o combo com
                    # o jogo em andamento. Aqui o campo só é gravado; quem barra
                    # é o diff.
                    inicio_evento=millis_to_iso(data.get("eventStartDate")),
                    boost_pct=data.get("percentageOffer"),
                )
            )
        return offers

    # ------------------------------------------------------------------

    async def scrape(self) -> list[Offer]:
        """Roda todas as fontes habilitadas e devolve as ofertas deduplicadas.

        Erra só se *tudo* falhar — uma fonte quebrada não derruba as outras.
        """
        tasks: list[asyncio.Future] = []
        labels: list[str] = []

        for sport in config.SPORTS:
            if config.ENABLE_MR12:
                tasks.append(asyncio.ensure_future(self._scrape_mr12_for_sport(sport)))
                labels.append(f"mr12/{sport.slug}")
            if config.ENABLE_SMART_PICKS:
                tasks.append(asyncio.ensure_future(self._scrape_smart_picks_for_sport(sport)))
                labels.append(f"smartpicks/{sport.slug}")

        if not tasks:
            raise ScraperError("nenhuma fonte habilitada em config.py")

        results = await asyncio.gather(*tasks, return_exceptions=True)

        offers: dict[str, Offer] = {}
        failures: list[str] = []
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                failures.append(f"{label}: {result}")
                log.warning("fonte %s falhou: %s", label, result)
                continue
            count_before = len(offers)
            for offer in result:
                offers[offer.offer_id] = offer
            added = len(offers) - count_before
            if added:
                log.info("  %s: %d oferta(s)", label, added)

        if failures and len(failures) == len(tasks):
            raise ScraperError("todas as fontes falharam: " + " | ".join(failures))

        return list(offers.values())


async def scrape_offers() -> list[Offer]:
    """Atalho: abre o cliente, raspa uma vez, fecha."""
    async with BetanoScraper() as scraper:
        return await scraper.scrape()
