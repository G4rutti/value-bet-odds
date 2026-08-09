"""Segunda camada de sinal: histórico do SofaScore, por cima do edge de odds.

Isto NÃO substitui o edge — o mercado (Pinnacle) já precifica o jogo melhor do
que qualquer estatística que a gente calcule por fora. O uso legítimo é
estreito, em duas frentes:

1. **Corrigir combo.** A odd justa de um combo multiplica as pernas assumindo
   independência (ver `value-bet-methodology`, seção 4). Quando as pernas são
   função do placar (HT/FT), dá pra medir a frequência conjunta REAL no
   histórico recente dos times e comparar com o produto das marginais.
2. **Red flag de notícia fresca.** Desfalque de titular ou troca de técnico
   que o mercado pode não ter precificado ainda — motivo pra DESCONFIAR mais
   de um edge alto, nunca pra confiar mais. `flag_noticia_fresca=True` só
   empurra a confiança pra baixo; nunca pra cima.

Fonte: `api.sofascore.com` — pública, sem login, sem cookie (ver a seção
"Achado real — SofaScore" na skill `network-endpoint-recon`).

Limite honesto de escopo
-------------------------
`freq_conjunta_historica` só é calculável quando TODAS as pernas do combo são
função do placar (HT/FT) de UM time em comum (ou são fatos do jogo inteiro,
tipo BTTS/total de gols, que não precisam de time específico). Perna de
cartão, chute a gol, escanteio ou prop de jogador não tem dado nenhum aqui —
outro endpoint, outro volume. Nesses casos o campo sai `None`, nunca `False`:
`False` afirmaria "não diverge", que é um sinal inventado.

Reusa `market_parser.parse_leg` pra decidir SE uma perna é função de placar
(mesma lógica que decide se a Pinnacle cobre) e `matcher.encontrar_evento` /
`matcher.normalizar` pra casar o evento com o ID do SofaScore — nenhum fuzzy
matching novo é escrito aqui. Match errado é pior que nenhum match.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import urlencode

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from .. import config as root_config
from . import config
from .market_parser import Leg, parse_leg
from .matcher import encontrar_evento, normalizar, split_times
from .models import Matchup

log = logging.getLogger(__name__)

SOFASCORE_BASE = "https://api.sofascore.com/api/v1"

# Mesmo motivo do resto do projeto: o WAF/TLS fingerprint do host derruba
# `requests` puro mas deixa passar o handshake do Chrome.
IMPERSONATE = getattr(config, "IMPERSONATE", "chrome")
REQUEST_TIMEOUT = getattr(config, "REQUEST_TIMEOUT", 30.0)

# --- rate limit + cache ------------------------------------------------------
#
# Sondado ao vivo em 2026-08: 15 requests seguidos sem pausa devolveram 200
# (ver skill de recon). Mesmo assim, o host manda `cache-control: max-age=60,
# s-maxage=7200` — ele ESPERA ser cacheado. Sem cache aqui, a fonte pode cortar
# no pior momento (mesma lição já paga com a Betano e a Pinnacle).
RATE_LIMIT_MIN_INTERVALO_S = float(os.getenv("SOFASCORE_MIN_INTERVALO_S", "0.4"))

# TTL por categoria de endpoint — não é tudo igual: id de time não muda, notícia
# de lesão muda todo dia.
TTL_BUSCA_HORAS = 24.0 * 7        # busca de time por nome: o ID é estável
TTL_JOGOS_HORAS = 6.0             # próximos/últimos jogos do time
TTL_LINEUPS_HORAS = 0.5           # escalação/desfalques: muda perto do jogo
# Placar e estatística de UMA partida. Curto porque o mesmo endpoint responde
# durante o jogo: cachear longo congelaria um placar parcial como final. O
# armazenamento durável do resultado é `partidas_sofascore` (storage.py).
TTL_EVENTO_HORAS = 0.25

_CACHE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sofascore_cache (
    url          TEXT PRIMARY KEY,
    corpo        TEXT NOT NULL,
    capturado_em TEXT NOT NULL,
    expira_em    TEXT NOT NULL
);
"""


class SofaScoreError(RuntimeError):
    pass


# ------------------------------------------------------------------
# cliente HTTP: cache em sqlite (mesmo banco do projeto) + rate limit
# ------------------------------------------------------------------

class SofaScoreClient:
    """Sessão contra `api.sofascore.com`, com cache sqlite e rate limit.

    O cache vive na MESMA base sqlite do projeto (`config.DB_PATH`), numa
    tabela própria — não mexe no schema de `storage.Storage` nem compete pela
    conexão dela (`Storage` roda com `check_same_thread=False` porque a
    avaliação acontece em `asyncio.to_thread`; esta conexão segue o mesmo
    padrão pelo mesmo motivo).
    """

    def __init__(self, *, session: "curl_requests.Session | None" = None,
                db_path: Path | str | None = None) -> None:
        self._session = session
        self._owns_session = session is None
        self.db_path = Path(db_path) if db_path is not None else root_config.DB_PATH
        self._conn: sqlite3.Connection | None = None
        self._lock = threading.Lock()
        self._ultima_chamada = 0.0

    def __enter__(self) -> "SofaScoreClient":
        if self._session is None:
            self._session = curl_requests.Session(impersonate=IMPERSONATE,
                                                   timeout=REQUEST_TIMEOUT)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.execute(_CACHE_SCHEMA)
        self._conn.commit()
        return self

    def __exit__(self, *exc: object) -> None:
        if self._owns_session and self._session is not None:
            self._session.close()
            self._session = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # -- cache -----------------------------------------------------------

    def _cache_get(self, url: str) -> str | None:
        assert self._conn is not None
        agora = datetime.now(timezone.utc).isoformat(timespec="seconds")
        row = self._conn.execute(
            "SELECT corpo FROM sofascore_cache WHERE url = ? AND expira_em > ?",
            (url, agora),
        ).fetchone()
        return row[0] if row else None

    def _cache_set(self, url: str, corpo: str, ttl_horas: float) -> None:
        assert self._conn is not None
        agora = datetime.now(timezone.utc)
        expira = (agora + timedelta(hours=ttl_horas)).isoformat(timespec="seconds")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO sofascore_cache (url, corpo, capturado_em, expira_em)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(url) DO UPDATE SET
                    corpo = excluded.corpo,
                    capturado_em = excluded.capturado_em,
                    expira_em = excluded.expira_em
                """,
                (url, corpo, agora.isoformat(timespec="seconds"), expira),
            )

    # -- request -----------------------------------------------------------

    def _get(self, path: str, *, ttl_horas: float, params: dict | None = None):
        assert self._session is not None, "use como context manager"
        url = f"{SOFASCORE_BASE}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"

        cache_hit = self._cache_get(url)
        if cache_hit is not None:
            return json.loads(cache_hit)

        with self._lock:
            espera = RATE_LIMIT_MIN_INTERVALO_S - (time.monotonic() - self._ultima_chamada)
            if espera > 0:
                time.sleep(espera)
            try:
                r = self._session.get(url)
            except RequestException as exc:
                raise SofaScoreError(f"falha de rede em {path}: {exc}") from exc
            finally:
                self._ultima_chamada = time.monotonic()

        if r.status_code == 404:
            # 404 é resposta válida pra "não existe" (ex.: jogo sem lineups
            # publicadas ainda). Cacheia curto pra não bater de novo no mesmo
            # ciclo, mas não é erro.
            self._cache_set(url, "null", min(ttl_horas, 0.25))
            return None
        if r.status_code != 200:
            raise SofaScoreError(f"{path} respondeu HTTP {r.status_code}")

        corpo = r.text
        self._cache_set(url, corpo, ttl_horas)
        return json.loads(corpo)

    # -- endpoints -----------------------------------------------------------

    def buscar(self, termo: str) -> dict:
        dados = self._get("/search/all", ttl_horas=TTL_BUSCA_HORAS,
                          params={"q": termo})
        return dados or {"results": []}

    def jogos_time(self, team_id: int, *, direcao: str, pagina: int = 0,
                   ttl_horas: float | None = None) -> list[dict]:
        """`direcao`: "next" (futuros) ou "last" (encerrados, forma recente).

        `ttl_horas` sobrescreve o padrão. A liquidação precisa disso: um jogo
        que ACABOU de terminar não aparece numa página cacheada há 6h, e sem
        poder pedir fresco a partida ficaria pendente até o cache expirar.
        """
        dados = self._get(f"/team/{team_id}/events/{direcao}/{pagina}",
                          ttl_horas=TTL_JOGOS_HORAS if ttl_horas is None else ttl_horas)
        return (dados or {}).get("events", [])

    def lineups(self, event_id: int) -> dict | None:
        return self._get(f"/event/{event_id}/lineups", ttl_horas=TTL_LINEUPS_HORAS)

    def evento(self, event_id: int) -> dict | None:
        """Dados da partida, incluindo `status.type` e o placar final.

        TTL curto de propósito: cachear longo uma partida em andamento
        congelaria o placar do intervalo como se fosse o final, para sempre.
        Quem guarda o resultado depois de `finished` é a tabela
        `partidas_sofascore` (durável), não este cache.
        """
        return self._get(f"/event/{event_id}", ttl_horas=TTL_EVENTO_HORAS)

    def estatisticas(self, event_id: int) -> dict | None:
        """Estatísticas da partida por período (`ALL` / `1ST` / `2ND`).

        É o que dá escanteios, cartões e chutes no gol — nada disso existe no
        payload de placar. Mesmo TTL curto e mesmo motivo do `evento`.
        """
        return self._get(f"/event/{event_id}/statistics", ttl_horas=TTL_EVENTO_HORAS)

    # `/event/{id}/managers` também é público e confirmado no recon (skill
    # `network-endpoint-recon`), mas não tem cliente aqui: "troca de técnico"
    # precisaria de um "técnico anterior" gravado pra saber se a troca é
    # RECENTE, e esta versão não persiste esse estado (ver `_noticia_fresca`).
    # Fica documentado como endpoint disponível, não como método morto.


# ------------------------------------------------------------------
# casamento do evento com o ID do SofaScore
# ------------------------------------------------------------------

def _candidatos_time(cliente: SofaScoreClient, nome: str, *,
                     max_candidatos: int = 2) -> list[dict]:
    """Times de futebol cujo nome bate com a busca — sem julgar qualidade
    ainda, isso é trabalho do `matcher` lá na frente."""
    try:
        dados = cliente.buscar(nome)
    except SofaScoreError as exc:
        log.warning("sofascore: busca de time falhou para %r: %s", nome, exc)
        return []
    candidatos = []
    for r in dados.get("results", []):
        if r.get("type") != "team":
            continue
        ent = r.get("entity", {})
        if (ent.get("sport") or {}).get("slug") != "football":
            continue
        candidatos.append(ent)
        if len(candidatos) >= max_candidatos:
            break
    return candidatos


@dataclass
class _CandidatoEvento:
    matchup: Matchup
    home_id: int | None
    away_id: int | None
    home_nome: str
    away_nome: str


def _matchups_candidatos(cliente: SofaScoreClient, casa: str, fora: str, *,
                         direcao: str = "next",
                         ttl_horas: float | None = None,
                         ) -> dict[int, _CandidatoEvento]:
    """Jogos dos times encontrados pra `casa`/`fora`, prontos pra passar em
    `matcher.encontrar_evento` — reuso, não reimplementação.

    `direcao="next"` (padrão) são jogos futuros, que é o que a camada de stats
    precisa. `direcao="last"` traz os encerrados, que é o que a liquidação
    precisa pra achar a partida que já aconteceu.
    """
    saida: dict[int, _CandidatoEvento] = {}
    for nome in (casa, fora):
        for time_ in _candidatos_time(cliente, nome):
            try:
                eventos = cliente.jogos_time(time_["id"], direcao=direcao,
                                             ttl_horas=ttl_horas)
            except SofaScoreError as exc:
                log.warning("sofascore: jogos (%s) falharam para %r: %s",
                           direcao, time_.get("name"), exc)
                continue
            for ev in eventos:
                eid = ev.get("id")
                home = ev.get("homeTeam") or {}
                away = ev.get("awayTeam") or {}
                if eid is None or eid in saida or not home.get("name") or not away.get("name"):
                    continue
                ts = ev.get("startTimestamp")
                commence = (datetime.fromtimestamp(ts, tz=timezone.utc)
                           if ts else None)
                torneio = (ev.get("tournament") or {}).get("name", "")
                saida[eid] = _CandidatoEvento(
                    matchup=Matchup(id=eid, league=torneio, home_team=home["name"],
                                    away_team=away["name"], commence_time=commence,
                                    sport="soccer"),
                    home_id=home.get("id"), away_id=away.get("id"),
                    home_nome=home["name"], away_nome=away["name"],
                )
    return saida


def _extrair_quando(evento: dict) -> str | datetime | None:
    return (evento.get("inicio_evento") or evento.get("valido_ate")
            or evento.get("capturado_em"))


# ------------------------------------------------------------------
# histórico de placar de um time
# ------------------------------------------------------------------

@dataclass
class JogoHistorico:
    """Um jogo já encerrado, normalizado pra perspectiva do time observado."""

    gols_pro: int
    gols_contra: int
    ht_pro: int | None
    ht_contra: int | None


def _jogo_valido(ev: dict) -> bool:
    status = (ev.get("status") or {}).get("type")
    return status == "finished"


def _historico_time(cliente: SofaScoreClient, team_id: int,
                    n_minimo: int = 6, n_alvo: int = 15) -> list[JogoHistorico]:
    """Últimos jogos ENCERRADOS de um time, mais recente por último no payload
    (sondado ao vivo: `events/last/0` vem em ordem CRESCENTE de data — ver a
    skill de recon). Busca a página seguinte só se a primeira não render o
    mínimo depois de filtrar cancelados/sem placar.
    """
    saida: list[JogoHistorico] = []
    for pagina in (0, 1):
        try:
            eventos = cliente.jogos_time(team_id, direcao="last", pagina=pagina)
        except SofaScoreError as exc:
            log.warning("sofascore: histórico falhou para time %s (pág %d): %s",
                       team_id, pagina, exc)
            break
        for ev in eventos:
            if not _jogo_valido(ev):
                continue
            home, away = ev.get("homeTeam") or {}, ev.get("awayTeam") or {}
            hs, as_ = ev.get("homeScore") or {}, ev.get("awayScore") or {}
            gh, ga = hs.get("normaltime"), as_.get("normaltime")
            if gh is None or ga is None:
                continue
            eh_casa = home.get("id") == team_id
            if not eh_casa and away.get("id") != team_id:
                continue  # o time não está neste jogo (não deveria acontecer)
            gols_pro, gols_contra = (gh, ga) if eh_casa else (ga, gh)
            ht_h, ht_a = hs.get("period1"), as_.get("period1")
            if ht_h is None or ht_a is None:
                ht_pro = ht_contra = None
            else:
                ht_pro, ht_contra = (ht_h, ht_a) if eh_casa else (ht_a, ht_h)
            saida.append(JogoHistorico(gols_pro=gols_pro, gols_contra=gols_contra,
                                       ht_pro=ht_pro, ht_contra=ht_contra))
        if len(saida) >= n_minimo:
            break
    # mais recente primeiro (a lista da API vem crescente; a gente lê do fim)
    saida.reverse()
    return saida[:n_alvo]


# ------------------------------------------------------------------
# classificação de perna -> predicado sobre JogoHistorico
# ------------------------------------------------------------------
#
# Reusa `market_parser.parse_leg`: se a Pinnacle cobre o mercado (`suportado`),
# o `market_key`/`selecao`/`time_nome` já diz exatamente que fato de placar a
# perna descreve. Só "marca em ambos os tempos" (que o market_parser marca
# como SEM_COBERTURA de propósito — não é função simples do 1X2, é uma
# combinação que a Pinnacle não publica) ganha um regex próprio aqui, porque
# esta camada tem uma fonte que a Pinnacle não tem: o placar HT completo do
# histórico.

_MARCA_AMBOS_TEMPOS = re.compile(
    r"^(?P<time>.+?)\s+(?:marcar\s+em\s+ambos\s+os\s+tempos"
    r"|para\s+ganhar\s+um\s+dos\s+tempos)\s*$", re.IGNORECASE)


@dataclass
class _Predicado:
    """O que checar num `JogoHistorico`, e de qual time (None = indiferente:
    fato do jogo inteiro, como BTTS ou total de gols, que vale a partir do
    histórico de qualquer um dos dois lados)."""

    time_interesse: int | None   # sofascore team id, ou None se indiferente
    avaliar: Callable[["JogoHistorico"], bool | None]


def _valores(jogo: JogoHistorico, primeiro_tempo: bool) -> tuple[int, int] | None:
    if primeiro_tempo:
        if jogo.ht_pro is None or jogo.ht_contra is None:
            return None
        return jogo.ht_pro, jogo.ht_contra
    return jogo.gols_pro, jogo.gols_contra


def _resolver_time(nome: str, home_nome: str, away_nome: str,
                   home_id: int | None, away_id: int | None) -> int | None:
    alvo = normalizar(nome)
    if alvo and alvo in normalizar(home_nome):
        return home_id
    if alvo and alvo in normalizar(away_nome):
        return away_id
    return None


def _predicado_da_perna(leg: Leg, cand: _CandidatoEvento) -> tuple[_Predicado | None, str | None]:
    """Devolve (predicado, motivo_recusa). Predicado None quando a perna não é
    função de placar coberta por esta primeira versão — motivo explica por quê.
    """
    texto = leg.texto.strip()

    m = _MARCA_AMBOS_TEMPOS.match(texto)
    if m:
        tid = _resolver_time(m.group("time"), cand.home_nome, cand.away_nome,
                            cand.home_id, cand.away_id)
        if tid is None:
            return None, "marca em ambos os tempos: time não casou com o evento"

        def _pred(jogo: JogoHistorico) -> bool | None:
            if jogo.ht_pro is None:
                return None
            return jogo.ht_pro > 0 and (jogo.gols_pro - jogo.ht_pro) > 0

        return _Predicado(time_interesse=tid, avaliar=_pred), None

    if not leg.suportado or not leg.market_key:
        return None, leg.motivo or "perna sem mercado reconhecido"

    # O prefixo (antes do primeiro ":") é o que identifica o TIPO de mercado;
    # a linha/lado vêm depois. "_1t" mora dentro do prefixo ("totals_1t:2.5"),
    # nunca no fim da chave inteira — por isso o corte é no prefixo, não na
    # chave completa (uma chave com linha tipo "totals_1t:2.5" não termina
    # em "_1t").
    prefixo = leg.market_key.split(":")[0]
    primeiro_tempo = prefixo.endswith("_1t")
    base = prefixo[:-3] if primeiro_tempo else prefixo

    if base == "h2h":
        if leg.selecao == "draw":
            def _pred(jogo: JogoHistorico) -> bool | None:
                v = _valores(jogo, primeiro_tempo)
                return None if v is None else v[0] == v[1]
            return _Predicado(time_interesse=None, avaliar=_pred), None

        # O lado vem por "home"/"away" ("Resultado Final 1") OU por nome de
        # time ("Resultado do 1º Tempo Flamengo", achado real do
        # `market_parser`: sem "1"/"2"/"Empate" no rótulo, ele preenche
        # `time_nome` em vez de `selecao`). Os dois casam pro MESMO predicado:
        # "o time de interesse teve mais gols que o adversário" — o `pro`/
        # `contra` de `JogoHistorico` já está na perspectiva de quem for
        # `tid`, então a fórmula não muda com quem é mandante/visitante.
        if leg.selecao in ("home", "away"):
            tid = cand.home_id if leg.selecao == "home" else cand.away_id
        elif leg.time_nome:
            tid = _resolver_time(leg.time_nome, cand.home_nome, cand.away_nome,
                                cand.home_id, cand.away_id)
        else:
            tid = None
        if tid is None:
            return None, "h2h: time não casou com o evento"

        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            return None if v is None else v[0] > v[1]
        return _Predicado(time_interesse=tid, avaliar=_pred), None

    if base == "btts":
        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            if v is None:
                return None
            eh_sim = v[0] > 0 and v[1] > 0
            return eh_sim if leg.selecao != "No" else not eh_sim
        return _Predicado(time_interesse=None, avaliar=_pred), None

    if base == "totals":
        m_linha = re.search(r":(\d+(?:\.\d+)?)$", leg.market_key)
        if not m_linha or leg.selecao not in ("over", "under"):
            return None, "total de gols sem linha reconhecível"
        linha = float(m_linha.group(1))

        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            if v is None:
                return None
            total = v[0] + v[1]
            return total > linha if leg.selecao == "over" else total < linha
        return _Predicado(time_interesse=None, avaliar=_pred), None

    if base == "team_total":
        m_linha = re.search(r":(\d+(?:\.\d+)?)$", leg.market_key)
        if not m_linha or leg.selecao not in ("over", "under") or not leg.time_nome:
            return None, "total de equipe sem linha/time reconhecível"
        linha = float(m_linha.group(1))
        tid = _resolver_time(leg.time_nome, cand.home_nome, cand.away_nome,
                            cand.home_id, cand.away_id)
        if tid is None:
            return None, "total de equipe: time não casou com o evento"

        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            if v is None:
                return None
            return v[0] > linha if leg.selecao == "over" else v[0] < linha
        return _Predicado(time_interesse=tid, avaliar=_pred), None

    if base == "team_exact_goals":
        if not leg.time_nome or leg.selecao is None or not leg.selecao.isdigit():
            return None, "gols exatos de equipe sem time/valor reconhecível"
        alvo = int(leg.selecao)
        tid = _resolver_time(leg.time_nome, cand.home_nome, cand.away_nome,
                            cand.home_id, cand.away_id)
        if tid is None:
            return None, "gols exatos de equipe: time não casou com o evento"

        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            return None if v is None else v[0] == alvo
        return _Predicado(time_interesse=tid, avaliar=_pred), None

    if base == "exact_goals":
        if leg.selecao is None or not leg.selecao.isdigit():
            return None, "gols exatos sem valor reconhecível"
        alvo = int(leg.selecao)

        def _pred(jogo: JogoHistorico) -> bool | None:
            v = _valores(jogo, primeiro_tempo)
            return None if v is None else (v[0] + v[1]) == alvo
        return _Predicado(time_interesse=None, avaliar=_pred), None

    # correct_score, ht_ft, double_chance, corners*, spread, player props,
    # tênis, basquete: fora do escopo desta primeira versão (ver docstring do
    # módulo). `correct_score`/`ht_ft`/`double_chance` carregam token
    # "{home}"/"{away}" não resolvido — resolver exigiria duplicar
    # `_token_1x2`/`ALTERNATIVAS` de `market_parser`, e o risco de discordar
    # sutilmente do parser oficial é maior que o valor de cobrir mais 3 chaves
    # agora.
    return None, f"fora do escopo de placar (market_key={leg.market_key!r})"


# ------------------------------------------------------------------
# frequência conjunta
# ------------------------------------------------------------------

N_JOGOS_MINIMO = 6
LIMIAR_DIVERGENCIA = 0.12  # pontos de probabilidade (0-1)


def _freq_conjunta(cliente: SofaScoreClient, mercado_pernas: list[str],
                   cand: _CandidatoEvento,
                   eventos_por_perna: list[str | None] | None = None) -> dict:
    """Tenta calcular a frequência conjunta real das pernas, no histórico.

    Recusa (devolve tudo None) em qualquer uma destas condições, e o motivo
    vai pro log em vez de virar um número inventado:
      - menos de 2 pernas (não há o que correlacionar);
      - as pernas vêm de jogos DIFERENTES (combo multi-jogo, ver
        `eventos_por_perna` abaixo) — frequência conjunta é uma medida sobre
        UM jogo, não existe "conjunta" entre dois jogos sem relação;
      - alguma perna não é função de placar coberta nesta versão;
      - as pernas apontam pra times DIFERENTES (não dá pra medir conjunta sem
        o histórico de confrontos diretos, que este endpoint não devolve — só
        o placar agregado, ver a skill de recon);
      - menos de `N_JOGOS_MINIMO` jogos utilizáveis no histórico.

    `eventos_por_perna`, quando informado, é `[leg.evento_texto for leg in
    legs]` — alinhado por índice com `mercado_pernas`. Combo multi-jogo
    (Novibet "Festival de Gols") marca TODAS as pernas com o jogo dela; se
    duas pernas discordam de jogo, `cand` (o único evento casado por
    `checar_stats`) só pode ser o de UMA delas — medir "conjunta" ali
    mediria a mesma perna consigo mesma, foi o `0.8667` degenerado do log de
    2026-08-07 (P(over 2.5) do Cincinnati contado 3x como se fosse conjunta
    de 3 jogos).
    """
    if len(mercado_pernas) < 2:
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": "mercado simples, nada a correlacionar"}

    if eventos_por_perna and len({e for e in eventos_por_perna if e}) > 1:
        log.info("sofascore: combo multi-jogo, sem freq_conjunta entre jogos "
                 "diferentes: %s", mercado_pernas)
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": "pernas de jogos diferentes (combo multi-jogo)"}

    legs = [parse_leg(texto) for texto in mercado_pernas]
    predicados: list[_Predicado] = []
    for leg in legs:
        pred, motivo = _predicado_da_perna(leg, cand)
        if pred is None:
            log.info("sofascore: combo sem freq_conjunta (%s): %s", motivo, leg.texto)
            return {"freq_conjunta_historica": None,
                   "diverge_da_estimativa_independente": None,
                   "_motivo": motivo}
        predicados.append(pred)

    times_especificos = {p.time_interesse for p in predicados if p.time_interesse is not None}
    if len(times_especificos) > 1:
        log.info("sofascore: combo referencia times diferentes, sem H2H pra medir "
                 "conjunta — %s", mercado_pernas)
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": "pernas de times diferentes (sem endpoint de H2H detalhado)"}

    time_id = next(iter(times_especificos), None) or cand.home_id
    if time_id is None:
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": "id do time não disponível"}

    jogos = _historico_time(cliente, time_id)
    if len(jogos) < N_JOGOS_MINIMO:
        log.info("sofascore: histórico insuficiente pro time %s (%d jogos, "
                 "mínimo %d)", time_id, len(jogos), N_JOGOS_MINIMO)
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": f"histórico insuficiente ({len(jogos)} jogos)"}

    # Uma linha por jogo, uma coluna por perna — cada jogo avaliado só UMA vez
    # por predicado. Jogo com qualquer perna não-avaliável (ex.: faltou HT)
    # sai da amostra inteira: a marginal de cada perna usa exatamente a MESMA
    # base da conjunta, senão a comparação "conjunta vs produto das marginais"
    # estaria misturando amostras diferentes.
    linhas: list[list[bool]] = []
    for jogo in jogos:
        resultados = [p.avaliar(jogo) for p in predicados]
        if any(r is None for r in resultados):
            continue
        linhas.append(resultados)  # type: ignore[arg-type]

    usaveis = len(linhas)
    if usaveis < N_JOGOS_MINIMO:
        return {"freq_conjunta_historica": None,
               "diverge_da_estimativa_independente": None,
               "_motivo": f"jogos usáveis insuficientes ({usaveis})"}

    conjuntos = sum(1 for linha in linhas if all(linha))
    marginais = [sum(1 for linha in linhas if linha[i]) / usaveis
                for i in range(len(predicados))]

    freq_conjunta = conjuntos / usaveis
    prod_independente = 1.0
    for m in marginais:
        prod_independente *= m

    diverge = abs(freq_conjunta - prod_independente) >= LIMIAR_DIVERGENCIA
    return {
        "freq_conjunta_historica": round(freq_conjunta, 4),
        "diverge_da_estimativa_independente": diverge,
        "_freq_estimativa_independente": round(prod_independente, 4),
        "_n_jogos_amostra": usaveis,
    }


# ------------------------------------------------------------------
# desfalque / notícia fresca
# ------------------------------------------------------------------

# `reason` no payload de `missingPlayers` diferencia lesão (1) de suspensão
# por cartão (11/13) — mas o filtro abaixo não distingue os dois de propósito:
# suspensão também é titular fora e o mercado pode não ter reagido. A
# `description` textual do payload (guardada verbatim no rótulo) já entrega
# esse detalhe pra quem lê o log, sem precisar decodificar o número.
#
# Heurística de "é titular" — não temos o elenco inteiro pra comparar rating,
# então usa o valor de mercado como proxy: um jogador residual (reserva de
# fim de banco) raramente passa deste piso. Documentado como heurística, não
# como fato.
VALOR_MERCADO_TITULAR_MIN = 3_000_000  # EUR


def _desfalques(lineups: dict | None, lado: str, nome_time: str) -> list[str]:
    if not lineups:
        return []
    bloco = lineups.get(lado) or {}
    saida = []
    for mp in bloco.get("missingPlayers") or []:
        jogador = mp.get("player") or {}
        nome = jogador.get("name")
        if not nome:
            continue
        valor = (jogador.get("proposedMarketValueRaw") or {}).get("value") or 0
        tipo = mp.get("type")  # "missing" | "doubtful"
        descricao = mp.get("description") or tipo or "desfalque"
        if tipo not in ("missing", "doubtful"):
            continue
        if valor < VALOR_MERCADO_TITULAR_MIN:
            continue
        rotulo = "dúvida" if tipo == "doubtful" else descricao
        saida.append(f"{nome} ({nome_time}) — {rotulo}")
    return saida


def _noticia_fresca(cliente: SofaScoreClient, event_id: int,
                    home_nome: str, away_nome: str) -> dict:
    """Desfalques de titular pras escalações do jogo casado.

    `lineups` responde mesmo pra jogo ainda não confirmado (`confirmed:false`)
    — sondado ao vivo, ver a skill de recon — então isto funciona mesmo dias
    antes do apito, que é justamente quando a notícia ainda pode não estar no
    preço.

    Troca de técnico fica de fora desta versão: dá pra ler o técnico ATUAL
    (`/event/{id}/managers`), mas sem um "técnico anterior" gravado em algum
    lugar não dá pra afirmar que a troca é RECENTE — e "recente" é o que
    importa aqui (o mercado já teve tempo de precificar uma troca de meses
    atrás). Fica registrado como lacuna, não como falso "sem troca".
    """
    try:
        lineups = cliente.lineups(event_id)
    except SofaScoreError as exc:
        log.warning("sofascore: lineups falharam pro evento %s: %s", event_id, exc)
        return {"desfalque_recente": None, "flag_noticia_fresca": None,
               "_motivo": f"lineups indisponíveis: {exc}"}

    desfalques = (_desfalques(lineups, "home", home_nome)
                 + _desfalques(lineups, "away", away_nome))
    return {"desfalque_recente": desfalques, "flag_noticia_fresca": bool(desfalques)}


# ------------------------------------------------------------------
# entrada pública
# ------------------------------------------------------------------

def checar_stats(evento: dict, mercado_pernas: list[str], *,
                 cliente: SofaScoreClient | None = None,
                 eventos_por_perna: list[str | None] | None = None) -> dict:
    """Segunda camada de sinal sobre uma oferta já casada com a Pinnacle.

    `evento` é o dict da oferta (mesma forma usada em `pipeline.avaliar_oferta`
    — precisa de `evento` (texto "Casa - Fora") e alguma referência de horário
    (`inicio_evento`/`valido_ate`/`capturado_em`). `mercado_pernas` é a lista
    de textos das pernas (`[l.texto for l in legs]`, o mesmo que o
    `market_parser` recebeu). `eventos_por_perna`, opcional, é
    `[l.evento_texto for l in legs]` — alinhado por índice, usado só pra
    recusar `freq_conjunta` entre pernas de jogos diferentes (ver
    `_freq_conjunta`). Sem ele, o comportamento é o de antes.

    Nunca levanta exceção: qualquer falha de rede/parse vira campo `None` +
    log explícito, nunca um resultado zerado silenciosamente.
    """
    saida = {
        "freq_conjunta_historica": None,
        "diverge_da_estimativa_independente": None,
        "desfalque_recente": None,
        "flag_noticia_fresca": None,
    }

    nome_evento = evento.get("evento", "")
    times = split_times(nome_evento)
    if not times:
        log.debug("sofascore: não separei os times de %r", nome_evento)
        return saida
    casa, fora = times
    quando = _extrair_quando(evento)

    def _rodar(cli: SofaScoreClient) -> dict:
        candidatos = _matchups_candidatos(cli, casa, fora)
        if not candidatos:
            log.debug("sofascore: nenhum candidato de evento pra %r", nome_evento)
            return saida
        matchups = [c.matchup for c in candidatos.values()]
        match = encontrar_evento(nome_evento, quando, matchups)
        if match is None:
            log.debug("sofascore: sem match pra %r entre %d candidato(s)",
                     nome_evento, len(candidatos))
            return saida

        cand = candidatos[match.matchup.id]
        resultado = dict(saida)

        try:
            resultado.update(_freq_conjunta(cli, mercado_pernas, cand, eventos_por_perna))
        except Exception as exc:  # nunca deixa a camada de stats derrubar a avaliação
            log.warning("sofascore: freq_conjunta falhou pra %r: %s", nome_evento, exc)

        try:
            resultado.update(_noticia_fresca(cli, match.matchup.id,
                                            cand.home_nome, cand.away_nome))
        except Exception as exc:
            log.warning("sofascore: notícia fresca falhou pra %r: %s", nome_evento, exc)

        # campos de diagnóstico (prefixo "_") não fazem parte do contrato
        # documentado, mas ajudam quem lê o log a entender o número.
        return resultado

    if cliente is not None:
        return _rodar(cliente)
    try:
        with SofaScoreClient() as cli:
            return _rodar(cli)
    except SofaScoreError as exc:
        log.warning("sofascore: cliente falhou pra %r: %s", nome_evento, exc)
        return saida
