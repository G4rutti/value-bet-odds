"""CasaDeAposta (API de odds) — fonte de REFERÊNCIA para o consenso, não fonte
de oferta.

Mesmo papel que `superbet.py` já cumpre pro pool: 4ª família de feed
independente, ao lado de `altenar`, `betano` e `superbet`
(`value.consenso._familia_da_casa` deriva a família automaticamente pelo
nome da casa — nenhuma mudança em `consenso.py` foi necessária).

Por que esta rota, e por que ela não é a mesma do `casadeaposta.py`
──────────────────────────────────────────────────────────────────
`casadeaposta.py` bate no CMS de promoções (`zizy-cms.casadeapostas.tv`) pra
achar os combos "SUPER ODDS" — é fonte de OFERTA. Esta é outra API, no
próprio domínio da casa, first-party, que devolve o livro de mercados
inteiro (odds normais, sem boost):

    GET https://casadeapostas.bet.br/br/sports              warm-up (cookie)
    GET https://casadeapostas.bet.br/api/odds/games          listagem paginada

Sondado ao vivo em 2026-08-19 com `curl_cffi.Session`: sem login, um GET
simples em `/br/sports` já basta pra obter os 2 cookies de sessão anônima
que `/api/odds/games` exige (nenhum deles de autenticação). A listagem já
vem com `markets[].odds[]` PRONTOS — diferente da Superbet, não precisa de
um segundo request de detalhe por evento.

⚠️ NÃO filtrar por `marketTypeIds` na URL
──────────────────────────────────────────
O `CASAS-PENDENTES.md` documentava uma lista de `marketTypeIds` chutada de
uma sessão anterior (17,1,1028,175,206,10,27,24,9,11,186,7,8,189). Testada ao
vivo contra 150 jogos reais, essa lista só devolveu 7 tipos de mercado (1x2,
Total, Dupla chance, Ambas Marcam, Empate devolve aposta, Ímpar/Par, Próximo
gol) — nenhum cartão, escanteio ou chute. Chamando o MESMO endpoint SEM o
parâmetro `marketTypeIds`, a API devolve o catálogo completo (até
`openMarketCount`) — aí sim aparecem `Total de escanteios`, `Escanteios
1x2`, `Escanteios handicap` etc., que é exatamente a família que mais falta
no pool (cartões/escanteios, ver o cabeçalho de `superbet.py`). A lista
antiga estava incompleta; melhor não filtrar do que herdar um filtro errado.

⚠️ Duas armadilhas de forma que só apareceram sondando ao vivo
─────────────────────────────────────────────────────────────
1. **Mercado de JOGADOR não é um livro de 2 lados.** `Jogador - Cartões` vem
   com literalmente UMA linha por instância ("Recio, Angel 1+" = 3.92, mais
   nada) — mesmo problema que a Superbet já documentou pra prop de jogador
   dela. Pior: `Jogador - Chutes no Gol` agrupa 1+/2+/3+/4+/5+ do MESMO
   jogador sob o MESMO `market.id` — não são lados complementares, são
   patamares cumulativos do mesmo evento (P(2+) já está contido em P(1+)).
   De-vigar isso como se fossem outcomes mutuamente exclusivos produziria
   probabilidade errada silenciosamente — a mesma classe de erro que
   `remover_vig` normalmente pega recusando o consenso, só que aqui passaria
   batido porque tecnicamente são "2+ lados". Por isso **todo mercado cujo
   nome começa com "Jogador"/"Jogadores" fica de fora**, sem exceção — mesma
   postura que a Superbet já adotou pro catálogo dela, só que aqui a guarda
   tem que ser explícita (denylist de nome) porque o filtro por
   `marketTypeIds` foi removido.
2. **Nome de mercado às vezes é um TEMPLATE não resolvido.** Ex.:
   `"{Game.Competitors[0].Competitor.Name} total de escanteios"` — o valor
   literal com chaves, sem substituição. Resolver o template exigiria juntar
   dado de outro canto do payload (`specialStringValue1`, nome do
   competidor) e não vale o risco agora; mercado com "{" no nome é
   descartado.

⚠️ `startDate` é UTC sem sufixo — NÃO é hora local de Brasília
────────────────────────────────────────────────────────────────
Confirmado comparando jogos do Brasileirão/Sul-Americana reais: "CR Flamengo
RJ - Cruzeiro MG" veio com `startDate = "2026-08-20T00:30:00"`, que só bate
com o horário real de bola rolando (21:30 em Brasília, o horário padrão de
jogo da Globo) se lido como UTC. `models.to_utc_iso` trata string SEM fuso
como hora LOCAL da máquina — passar o valor cru pra ela inverteria o sinal
do fuso (-3h em vez de nada). Por isso este módulo gruda `+00:00` no valor
antes de chamar `to_utc_iso`.

⚠️ `liga` nunca vem preenchida
───────────────────────────────
A listagem não traz nome de campeonato (`league` sempre `None`, só
`leagueId` numérico) e não achei endpoint irmão que resolva o nome
(`bettable-sports`, `leagues`, `game-categories`, `tournaments` — todos 404
ou vazios na sondagem). Sem `liga`, `matcher.genero_conflita` não enxerga
marcador de gênero e a ponte de evento recusa o casamento só pra jogos com
marcador (mesma degradação documentada em `superbet.py` quando `/struct`
falha) — não bloqueia o resto. Aceito como limitação conhecida; achar o
endpoint de nome de liga fica pra uma rodada futura.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Any

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import MercadoCasa, Offer, to_utc_iso
from .scraper import ScraperError
from .value import config as vconfig
from .value.matcher import _parse_data, encontrar_evento, split_times
from .value.models import Matchup

log = logging.getLogger(__name__)

CASA = "CasaDeAposta"
BASE = "https://casadeapostas.bet.br"
WARMUP_URL = f"{BASE}/br/sports"
GAMES_URL = f"{BASE}/api/odds/games"

# Confirmado ao vivo em 2026-08-19 (`sportName` na própria resposta). Mesmos
# 3 esportes que a Pinnacle carrega (`PinnacleScraper.jogos_normalizados`).
ESPORTES_REAIS: dict[int, str] = {1: "Futebol", 2: "Basquete", 6: "Tênis"}

_PAGE_SIZE = 50

# ALLOWLIST de mercado, não denylist — e por um motivo concreto, não só por
# princípio (mesmo motivo de `ESPORTES_REAIS`). Sondado ao vivo em
# 2026-08-19 sem filtro de `marketTypeIds`: de 530 nomes de mercado
# distintos vistos, **~450 eram "<Nome do Jogador> para marcar (incl.
# prolongamento)"** — um mercado por artilheiro em POTENCIAL, um por jogo.
# Um denylist só de `startswith("jogador")` deixa passar isso inteiro (e
# ainda vazava "Primeiro jogador a receber cartão", "Último marcador",
# "Marcador a qualquer altura", "<Time> 1st player to score" — todos
# variações de "quem marca/quem é expulso primeiro" sem a palavra "jogador"
# no começo). Em vez de perseguir cada grafia nova de mercado-por-jogador
# com mais uma regra de exclusão, a lista positiva abaixo é o que passou a
# sondagem manual: só os mercados de JOGO/TIME, N-way COMPLEMENTAR de
# verdade. Mercado fora daqui fica de fora até alguém confirmar ao vivo que
# o book é seguro pra de-vigar — "não suportado" é melhor que "suportado
# errado" (mesmo princípio de `sportingtech.py`/`market_parser.py`).
_MERCADOS_SEGUROS = frozenset({
    "1x2", "total", "total (incluindo prorrogacao)", "total de escanteios",
    "total games", "dupla chance", "ambas marcam", "ambas as equipes marcam",
    "empate devolve aposta", "escala de escanteios", "escanteios 1x2",
    "escanteios handicap", "escanteios impar/par", "impar/par",
    "impar/par (incluindo prorrogacao)", "handicap", "handicap asiatico",
    "handicap de games", "handicap de sets", "vencedor",
    "vencedor (incluindo prolongamento)", "games: impar / par",
})

# "1º tempo - ", "2º tempo - " etc. na frente do nome — o mercado por trás
# do prefixo é o mesmo book, só recortado por período. Descolar antes de
# comparar contra `_MERCADOS_SEGUROS` em vez de listar cada combinação.
_PREFIXO_PERIODO_RE = re.compile(r"^\d\s*[ºo°]?\s*(tempo|periodo)\s*-\s*", re.I)


def _sem_acento(texto: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", texto)
                   if unicodedata.category(c) != "Mn")


def _tem_template(nome: str) -> bool:
    return "{" in nome


def _mercado_seguro(nome: str) -> bool:
    n = _sem_acento(nome.strip().lower())
    n = _PREFIXO_PERIODO_RE.sub("", n)
    return n in _MERCADOS_SEGUROS


class CasaDeApostaLivroScraper:
    """Livro de mercados da CasaDeAposta para o pool de consenso.

    `scrape()` sempre devolve `[]` — mesmo contrato de `SuperbetScraper`:
    esta rota não diz qual seleção está turbinada, então não inventa oferta.
    """

    def __init__(self, session: "curl_requests.AsyncSession | None" = None,
                 alvos: list[dict] | None = None) -> None:
        self.mercados_vistos: list[MercadoCasa] = []
        # Diferente da Superbet, a listagem já traz o livro completo de cada
        # jogo sem request extra — não há orçamento de DETALHE a repartir
        # entre fila e resto. `alvos` fica só como sinal de log/diagnóstico
        # aqui (quantos da fila apareceram nas páginas puxadas), não decide
        # o que pedir.
        self.alvos = list(alvos or [])
        self._session = session
        self._owns = session is None

    async def __aenter__(self) -> "CasaDeApostaLivroScraper":
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

    async def _warmup(self) -> None:
        """Sem isto `/api/odds/games` nunca chega a ser chamado com cookie —
        o `AsyncSession` guarda o cookie jar automaticamente pras chamadas
        seguintes."""
        assert self._session is not None
        try:
            await self._session.get(WARMUP_URL)
        except RequestException as exc:
            raise ScraperError(f"warm-up de sessão falhou: {exc}") from exc

    async def _get_json(self, url: str, **params: Any) -> dict:
        """Único ponto de chamada de rede — seam de teste (ADAPTER_CONTRACT.md §7.1)."""
        assert self._session is not None, "use o scraper como context manager"
        await asyncio.sleep(random.uniform(*config.REQUEST_JITTER))
        try:
            r = await self._session.get(url, params=params or None)
        except RequestException as exc:
            raise ScraperError(f"falha de rede em {url}: {exc}") from exc
        if r.status_code != 200:
            raise ScraperError(f"{url} respondeu HTTP {r.status_code}")
        try:
            return r.json()
        except ValueError as exc:
            raise ScraperError(f"{url} não devolveu JSON: {exc}") from exc

    # ------------------------------------------------------------------

    async def scrape(self) -> list[Offer]:
        """Enche `mercados_vistos`. Devolve `[]` — não é fonte de oferta."""
        await self._warmup()

        agora = datetime.now(timezone.utc)
        fim = agora + timedelta(hours=config.ALTENAR_HORIZONTE_HORAS)

        n_jogos = 0
        todos_itens: list[dict] = []
        for sport_id, nome_esporte in ESPORTES_REAIS.items():
            try:
                itens = await self._listar_esporte(sport_id, agora, fim)
            except ScraperError as exc:
                log.warning("%s: listagem de %s falhou: %s", CASA, nome_esporte, exc)
                continue
            todos_itens += itens
            for item in itens:
                self.mercados_vistos += self._mercados_do_jogo(item)
                n_jogos += 1

        if n_jogos == 0:
            # Catálogo dessa casa costuma ter centenas de jogos — zero é tão
            # anormal quanto na Superbet, e some sem erro se não logar alto.
            log.warning("%s: nenhum jogo nos esportes reais — investigar "
                        "antes de assumir que é normal", CASA)

        log.info("%s: %d mercado(s) de %d jogo(s)",
                 CASA, len(self.mercados_vistos), n_jogos)

        if self.alvos:
            n_achados = self._contar_alvos_batidos(todos_itens)
            log.info("%s: %d de %d alvo(s) da fila apareceram nas páginas "
                     "puxadas", CASA, n_achados, len(self.alvos))

        return []

    def _contar_alvos_batidos(self, itens: list[dict]) -> int:
        """Quantos `self.alvos` (eventos da fila de avaliação) apareceram nas
        páginas puxadas neste ciclo. Puro diagnóstico — diferente da
        Superbet/Altenar, esta fonte não tem orçamento de detalhe pra
        repartir entre fila e resto (a listagem já traz o livro completo),
        então isto não decide o que pedir. Mesmo casador/tolerância que
        `superbet._eventos_da_fila` e `esportiva._eventos_da_fila` usam."""
        candidatos: list[Matchup] = []
        for item in itens:
            nome = item.get("name") or ""
            times = split_times(nome)
            inicio = _parse_data(item.get("startDate"))
            if times is None or inicio is None:
                continue
            candidatos.append(Matchup(id=item.get("id"), league="",
                                      home_team=times[0], away_team=times[1],
                                      commence_time=inicio))
        if not candidatos:
            return 0

        n_achados = 0
        for alvo in self.alvos:
            m = encontrar_evento(alvo.get("evento") or "",
                                 alvo.get("inicio_evento"), candidatos,
                                 min_score=vconfig.CONSENSO_MATCH_MIN_SCORE,
                                 max_horas=vconfig.CONSENSO_MATCH_MAX_HORAS)
            if m is not None:
                n_achados += 1
        return n_achados

    async def _listar_esporte(self, sport_id: int, agora: datetime,
                              fim: datetime) -> list[dict]:
        """Pagina `/api/odds/games` até `CASADEAPOSTA_LIVRO_MAX_PAGINAS` ou
        até a página vir mais curta que `_PAGE_SIZE` (fim do catálogo).

        Sem filtro de `marketTypeIds` de propósito — ver o cabeçalho do
        módulo.
        """
        vistos: dict[int, dict] = {}
        for pagina in range(config.CASADEAPOSTA_LIVRO_MAX_PAGINAS):
            dados = await self._get_json(
                GAMES_URL,
                startDate=agora.strftime("%Y-%m-%dT%H:%M:%S"),
                endDate=fim.strftime("%Y-%m-%dT%H:%M:%S"),
                languageId=21, gameMode=3,
                pageNumber=pagina, pageSize=_PAGE_SIZE,
                sportId=sport_id,
            )
            itens = dados.get("items") or []
            for it in itens:
                jid = it.get("id")
                if jid is not None:
                    vistos[jid] = it
            if len(itens) < _PAGE_SIZE:
                break
        return list(vistos.values())

    def _mercados_do_jogo(self, item: dict) -> list[MercadoCasa]:
        """Um `MercadoCasa` por seleção, agrupado por `market["id"]`
        (instância — não `marketTypeId`, que é o TIPO e se repete entre
        linhas de total diferentes, mesma armadilha documentada em
        `superbet.py`).
        """
        nome_evento = item.get("name") or ""
        if not nome_evento or split_times(nome_evento) is None:
            return []
        evento_id = item.get("id")
        if evento_id is None:
            return []
        inicio_evento = to_utc_iso(f"{item['startDate']}+00:00") \
            if item.get("startDate") else None

        saida: list[MercadoCasa] = []
        for mkt in item.get("markets") or []:
            nome_mkt = str(mkt.get("name") or "").strip()
            mkt_id = mkt.get("id")
            if not nome_mkt or mkt_id is None:
                continue
            if _tem_template(nome_mkt) or not _mercado_seguro(nome_mkt):
                continue
            if not mkt.get("published", True):
                continue

            selecoes: list[dict] = []
            for odd in mkt.get("odds") or []:
                if odd.get("state") != 1:
                    continue   # linha suspensa/fechada — preço não confiável
                nome_sel = odd.get("name")
                preco = odd.get("value")
                if not nome_sel:
                    continue
                try:
                    preco = float(preco)
                except (TypeError, ValueError):
                    continue
                if preco <= 1.0:
                    continue
                selecoes.append({"nome": str(nome_sel), "preco": preco})

            if len(selecoes) < 2:
                continue   # sem os dois lados não há margem a remover

            for s in selecoes:
                saida.append(MercadoCasa(
                    casa=CASA,
                    evento_id=str(evento_id),
                    market_id=str(mkt_id),
                    market_nome=nome_mkt,
                    selecao=s["nome"],
                    preco=s["preco"],
                    evento=nome_evento,
                    inicio_evento=inicio_evento,
                    liga=None,   # ver o aviso no cabeçalho do módulo
                ))
        return saida


async def scrape_mercados(alvos: list[dict] | None = None) -> list[MercadoCasa]:
    """Atalho pra uso avulso (script, teste manual)."""
    async with CasaDeApostaLivroScraper(alvos=alvos) as sc:
        await sc.scrape()
        return sc.mercados_vistos
