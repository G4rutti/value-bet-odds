"""Scraper de casas SportingTech (EsportesDaSorte, OleyBet).

Etapa 0 — Reconhecimento (resultado)
────────────────────────────────────
O levantamento original já sabia que a EsportesDaSorte roda **SportingTech**,
first-party, no domínio da própria casa, com o formato de rota
`/api-v2/<endpoint>/d/<languageId>/<tenant>/...`. Faltava achar onde mora a
oferta turbinada — dois candidatos óbvios (`getPopularOdds`, `betBooster`)
foram investigados a fundo lendo o bundle Angular e **descartados**:

- `/api/generic/sportbet/getPopularOdds?languageId=23&deviceType=d` funciona
  (200, sem cookie), mas é "apostas populares/em alta" — uma odd só por
  item, sem par original/turbinada.
- `betBoosterDataService` é um widget de terceiro (**LVision**, domínio
  `betbooster-widget.lvision.io` achado no bundle) — um "bet builder" que o
  cliente monta combinando mercados do mesmo jogo, calculado no browser a
  partir do `fixture-detail` normal. Não é oferta pré-pronta da casa.

A oferta turbinada de verdade é um **esporte virtual dedicado**: `stN =
"Super Odds"` (`stId 712`), achado em 2026-08-05 via captura de XHR em
browser real (usuário navegou até a seção "Odds Turbinadas" do site — ver
`CASAS-PENDENTES.md` seção 0). O endpoint é

    GET /api-v2/fixture/category-details/d/<languageId>/<tenant>/null/false/ante/<limite>/esportes-super-odds/<categoria>/multis

— NÃO usa o construtor de URL genérico (`prerenderService.getUrl`, que só
cola valores de `requestBody` no path); usa um construtor irmão,
`getOrderUrl`, que monta o path a partir de `antePostEvent`/`betTypeGroupLimit`
mais `stN`/`cN`/`seaN` (aqui fixos: `esportes-super-odds`/`football`/`multis`).
Cada combo turbinado (`fos[]`) já vem com a odd atual em `hO` e a odd
original **embutida no próprio nome do mercado**, como `"...(Era 3.4)"` — é
o mesmo texto que aparece na tela e que o usuário vinha copiando pros
próprios registros de aposta. Sem cookie de sessão — mas **precisa** dos
headers `origin` + `sec-fetch-*` (sem eles a rota responde `200` só que com
`NO_DATA_FOUND`, um "sucesso vazio" enganoso, não um erro).

Payload de contexto que o header `encodedbody` carrega (achado lendo o
bundle): `{"requestBody":{"antePostEvent":true,"betTypeGroupLimit":20,
"bragiUrl":"https://bragi.sportingtech.com/"}}`, sempre o mesmo — não muda
por casa nem por chamada.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import Offer, to_utc_iso
from .scraper import ScraperError
from .value.market_parser import SEPARADOR_PERNAS

log = logging.getLogger(__name__)

_LANGUAGE_ID = 23
_BET_TYPE_GROUP_LIMIT = 20
_ENCODED_BODY = base64.b64encode(
    json.dumps(
        {
            "requestBody": {
                "antePostEvent": True,
                "betTypeGroupLimit": _BET_TYPE_GROUP_LIMIT,
                "bragiUrl": "https://bragi.sportingtech.com/",
            }
        },
        separators=(",", ":"),
    ).encode()
).decode()

_ERA_RE = re.compile(r"\s*\(Era ([\d.,]+)\)\s*$")

# A API separa as pernas de uma combinada com "&" ou "|" — o resto do
# projeto usa `SEPARADOR_PERNAS` (" + "). Sem essa troca, `parse_mercado()`
# nunca quebra a frase em pernas: uma combinada de 2 pernas vira "mercado
# simples" e casa contra o preço avulso de só uma delas (edge inflado, caso
# real: "Ambas Marcam & Mais de 10.5 Escanteios" virou odd justa da perna
# "Ambas Marcam" sozinha — +110% de "value" falso). Ver CASAS-PENDENTES.md.
_SEPARADORES_API_RE = re.compile(r"\s*[&|]\s*")

# A EsportesDaSorte escreve o total de gols do JOGO sem a palavra "total"
# ("Mais de 2.5 Gols"), mas `market_parser._parse_total_gols` exige esse
# sinal de propósito — é o que distingue "total do jogo" de "total de uma
# equipe só" (ver o comentário sobre o incidente do Mirassol nesse mesmo
# arquivo). Só normaliza a forma sem nome de time antes: com nome de time
# ("Palmeiras Mais de 1.5 Gols") ou o combo-de-vários-jogos ("... em Todas
# as Partidas") ficam de fora de propósito — não suportados é melhor que
# suportados errado.
#
# Revalidado em 2026-08-05: apareceu uma terceira grafia ao vivo, "Mais de
# 0.5 Gols no 1º Tempo" — bare goals do jogo, mas com qualificador de
# período no fim. Sem cobrir isso a perna caía direto em "mercado não
# reconhecido" (não tem "total" em lugar nenhum da frase). Cobrimos só o
# sufixo de 1º TEMPO/PRIMEIRO TEMPO explicitamente: depois de virar "Total
# de Gols: ...", `_parse_total_gols` decide o sufixo (`_sufixo`) varrendo a
# frase inteira e qualquer período que NÃO seja 1º tempo cai em "" (jogo
# inteiro) — normalizar um bare "... no 2º Tempo" deixaria o total do 2º
# tempo casado contra a referência do jogo inteiro, a mesma classe do
# incidente do Mirassol. Por isso o grupo do período é opcional e restrito
# a 1º tempo/intervalo; "2º Tempo" continua de fora, sem match, de propósito.
_GOLS_BARE_RE = re.compile(
    r"^(mais|menos)\s+de\s+[\d.,]+\s+gols"
    r"(?:\s+no\s+1[.°ºo]?\s*tempo|\s+no\s+intervalo)?$",
    re.I,
)


def _normalizar_perna(perna: str) -> str:
    perna = perna.strip()
    if _GOLS_BARE_RE.match(perna):
        return f"Total de Gols: {perna}"
    return perna


@dataclass(frozen=True)
class CasaSportingTech:
    nome: str
    tenant: str
    dominio: str  # ex.: "https://esportesdasorte.bet.br"
    categoria: str = "football"  # segmento `cN` da rota — só futebol confirmado

    @property
    def super_odds_url(self) -> str:
        return (
            f"{self.dominio}/api-v2/fixture/category-details/d/{_LANGUAGE_ID}/"
            f"{self.tenant}/null/false/ante/{_BET_TYPE_GROUP_LIMIT}/"
            f"esportes-super-odds/{self.categoria}/multis"
        )

    @property
    def pagina_super_odds(self) -> str:
        return f"{self.dominio}/ptb/bet/esportes-super-odds/{self.categoria}/multis"


# Confirmada em 2026-08-05: sem cookie de sessão. A OleyBet roda a mesma
# plataforma (mesmo levantamento original) — testado ao vivo em 2026-08-05,
# ainda inconclusivo: `CasaSportingTech("OleyBet", "oleybet",
# "https://oleybet.bet.br")` sem cookie devolve 200 com "NO_DATA_FOUND"
# (sucesso vazio, headers origin/sec-fetch-* enviados certinho); visitar a
# home antes na mesma sessão (`AsyncSession` guarda cookie `NCC=PTB`) e
# repetir a chamada não mudou o resultado. `getPopularOdds` (endpoint sem
# tenant no path) funciona normal pra OleyBet e devolve jogos reais, então a
# casa não está bloqueada pelo WAF — o "oleybet" como `tenant` específico é
# que não está confirmado (pode ser o tenant errado, ou a OleyBet
# simplesmente não ter o produto "Super Odds" habilitado). Não achei o
# tenant real sem inspecionar o bundle Angular em um browser (fora do
# escopo desta sessão, sem ferramenta de browser disponível). NÃO
# adicionar linha nova aqui sem confirmar de verdade — ver CASAS-PENDENTES.md.
CASAS_SPORTINGTECH: tuple[CasaSportingTech, ...] = (
    CasaSportingTech("EsportesDaSorte", "esportesdasortevip", "https://esportesdasorte.bet.br"),
)


class SportingTechScraper:
    """Busca as ofertas do esporte virtual "Super Odds" de uma casa SportingTech."""

    def __init__(self, casa: CasaSportingTech, session: "curl_requests.AsyncSession | None" = None) -> None:
        self.casa = casa
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "SportingTechScraper":
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers={
                    "accept": "application/json, text/plain, */*",
                    "bragiurl": "https://bragi.sportingtech.com/",
                    "customorigin": self.casa.dominio,
                    "device": "m",
                    "encodedbody": _ENCODED_BODY,
                    "languageid": str(_LANGUAGE_ID),
                    "origin": self.casa.dominio,
                    "referer": self.casa.pagina_super_odds,
                    "sec-fetch-dest": "empty",
                    "sec-fetch-mode": "cors",
                    "sec-fetch-site": "same-origin",
                },
                impersonate=config.IMPERSONATE,
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def _get_json(self, url: str) -> Any:
        """Único ponto de chamada de rede — seam de teste (ADAPTER_CONTRACT.md §7.1).

        `ScraperFake` em `tests/test_sportingtech.py` subclassa e sobrescreve só
        este método, sem tocar em `scrape()`/`_parse_combo`.
        """
        assert self._session is not None, "use o scraper como context manager"
        try:
            r = await self._session.get(url)
        except RequestException as exc:
            raise ScraperError(f"falha de rede no {self.casa.nome}: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"{self.casa.nome} respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"{self.casa.nome} não devolveu JSON: {exc}") from exc

    async def scrape(self) -> list[Offer]:
        dados = await self._get_json(self.casa.super_odds_url)

        if not dados.get("success"):
            # "NO_DATA_FOUND" é sucesso vazio, não erro — sem oferta hoje.
            log.info("%s: 0 oferta(s) (%s)", self.casa.nome,
                     (dados.get("responseCodes") or [{}])[0].get("responseKey", "?"))
            return []

        ofertas: list[Offer] = []
        for esporte in dados.get("data") or []:
            for categoria in esporte.get("cs") or []:
                for temporada in categoria.get("sns") or []:
                    for fixture in temporada.get("fs") or []:
                        for grupo in fixture.get("btgs") or []:
                            for combo in grupo.get("fos") or []:
                                oferta = self._parse_combo(
                                    combo, fixture, liga_fallback=categoria.get("cN")
                                )
                                if oferta is not None:
                                    ofertas.append(oferta)

        log.info("%s: %d oferta(s)", self.casa.nome, len(ofertas))
        return ofertas

    def _parse_combo(
        self, combo: dict[str, Any], fixture: dict[str, Any], liga_fallback: str | None
    ) -> Offer | None:
        nome = combo.get("btN") or combo.get("hSh") or ""
        match = _ERA_RE.search(nome)
        odd_boost = combo.get("hO")
        if not match or not odd_boost:
            return None
        try:
            odd_original = float(match.group(1).replace(",", "."))
            odd_boost = float(odd_boost)
        except (TypeError, ValueError):
            return None
        if odd_boost <= odd_original:
            return None

        evento_id = fixture.get("fId")
        if not evento_id:
            return None

        nome_sem_era = _ERA_RE.sub("", nome).strip()
        if not nome_sem_era:
            return None
        pernas = [_normalizar_perna(p) for p in _SEPARADORES_API_RE.split(nome_sem_era) if p.strip()]
        mercado = SEPARADOR_PERNAS.join(pernas)

        valido_ate = to_utc_iso(fixture.get("fsd"))

        return Offer(
            casa=self.casa.nome,
            fonte="sportingtech_super_odds",
            evento_id=str(evento_id),
            evento=fixture.get("hcN") or str(evento_id),
            mercado=mercado,
            odd_original=odd_original,
            odd_boost=odd_boost,
            url=self.casa.pagina_super_odds,
            liga=liga_fallback,
            valido_ate=valido_ate,
            inicio_evento=valido_ate,
            boost_pct=round((odd_boost / odd_original - 1) * 100, 1),
        )


async def scrape_offers(casa: CasaSportingTech) -> list[Offer]:
    async with SportingTechScraper(casa) as scraper:
        return await scraper.scrape()
