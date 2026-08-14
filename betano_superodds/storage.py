"""Persistência em SQLite: estado atual das ofertas + histórico de mudanças."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config
from .diff import DiffResult
from .models import MercadoCasa, Offer
from .value import matcher as _matcher
from .value.pool_eventos import montar_matchups as _montar_matchups
from .value.pool_eventos import resolver_evento_id as _resolver_evento_id
from .value.pool_eventos import resolver_evento_ids as _resolver_evento_ids

SCHEMA = """
CREATE TABLE IF NOT EXISTS offers (
    offer_id      TEXT PRIMARY KEY,
    content_hash  TEXT NOT NULL,
    casa          TEXT NOT NULL DEFAULT 'Betano',
    fonte         TEXT NOT NULL,
    evento_id     TEXT NOT NULL,
    evento        TEXT NOT NULL,
    liga          TEXT,
    mercado       TEXT NOT NULL,
    odd_original  REAL,
    odd_boost     REAL NOT NULL,
    boost_pct     REAL,
    valido_ate    TEXT,
    -- Kickoff em ISO UTC canônico (`models.to_utc_iso`). Separado de
    -- `valido_ate`, que mistura fim-de-promoção com início de jogo. UTC porque
    -- a fila compara e ordena por ele em SQL: com offsets misturados
    -- ("...Z" da Altenar, "-03:00" da CasaDeAposta) a comparação lexicográfica
    -- do SQLite mentiria.
    inicio_evento TEXT,
    url           TEXT,
    -- Teto de aposta imposto pela casa (`betsLimit` da Altenar). Boost muito
    -- alto costuma vir com limite baixo: sem isto, o alerta recomenda 5un numa
    -- oferta que a casa limita a alguns reais.
    limite_aposta REAL,
    first_seen    TEXT NOT NULL,
    last_seen     TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_offers_active ON offers(active);

CREATE TABLE IF NOT EXISTS offer_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id      TEXT NOT NULL,
    evento        TEXT NOT NULL,
    mercado       TEXT NOT NULL,
    odd_original  REAL,
    odd_boost     REAL,
    evento_tipo   TEXT NOT NULL,  -- nova | alterada | expirada
    observado_em  TEXT NOT NULL,
    FOREIGN KEY (offer_id) REFERENCES offers(offer_id)
);

CREATE INDEX IF NOT EXISTS idx_history_offer ON offer_history(offer_id);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    executado_em  TEXT NOT NULL,
    ofertas       INTEGER NOT NULL,
    novas         INTEGER NOT NULL,
    alteradas     INTEGER NOT NULL,
    expiradas     INTEGER NOT NULL,
    erro          TEXT
);

-- Dedup de notificação. A guarda de "já alertou" (`Storage.ja_alertou`) é
-- sobre `offer_id` + `edge_pct`, não sobre `alert_hash` nem `content_hash`:
-- os dois mudam sozinhos com qualquer oscilação de odd (pré-boost ou
-- turbinada) e já geraram flood da mesma oferta duas vezes (2026-08-04,
-- `odd_original`; 2026-08-07, `odd_boost` — ver `Offer.alert_hash`). Uma
-- oferta só realerta se o edge melhorar de verdade sobre o MAX já alertado.
-- `content_hash`/`alert_hash` continuam gravados por rastreabilidade — dão o
-- vínculo com a versão exata da oferta que gerou o envio.
CREATE TABLE IF NOT EXISTS alertas_enviados (
    offer_id      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    alert_hash    TEXT,
    odd_boost     REAL,
    edge_pct      REAL,
    enviado_em    TEXT NOT NULL,
    PRIMARY KEY (offer_id, content_hash)
);

-- Quando cada versão de cada oferta foi avaliada contra a Pinnacle. É o que
-- transforma o teto por ciclo numa FILA em vez de um descarte: o que não coube
-- num ciclo continua pendente e entra no seguinte.
CREATE TABLE IF NOT EXISTS avaliacoes (
    offer_id      TEXT NOT NULL,
    content_hash  TEXT NOT NULL,
    avaliado_em   TEXT NOT NULL,
    status        TEXT,
    PRIMARY KEY (offer_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_avaliacoes_em ON avaliacoes(avaliado_em);

-- Veredito da curadoria estruturada (`value/curadoria.py`) por versão de
-- oferta — gravado MESMO em modo sombra e MESMO pra oferta que não chegou a
-- virar alerta, porque é isso que permite depois perguntar "das que enviei
-- com veredito 'vetado', quantas deram red?" (join com `liquidacoes` por
-- `offer_id`, ver `query.py --curadoria`). PK igual a `avaliacoes`: o
-- veredito é sobre uma VERSÃO específica da oferta (mesma odd), não sobre a
-- oferta ao longo do tempo.
CREATE TABLE IF NOT EXISTS veredito_curadoria (
    offer_id            TEXT NOT NULL,
    content_hash        TEXT NOT NULL,
    decisao             TEXT NOT NULL,   -- aprovado | degrau | vetado
    motivo              TEXT,
    confianca_original  TEXT,
    confianca_final      TEXT,
    modo                TEXT NOT NULL,   -- sombra | enforce (o que ESTE registro usou)
    sinais_json         TEXT,
    avaliado_em         TEXT NOT NULL,
    PRIMARY KEY (offer_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_veredito_curadoria_decisao ON veredito_curadoria(decisao);

-- Mercados completos das casas, para a referência de consenso.
--
-- Só existe porque a Pinnacle não publica prop de jogador (conferido na API:
-- ela publica 1X2, handicap, totais e specials de gol, mais nada). Para
-- chutes ao gol / cartões / artilheiro, a única referência possível é o que as
-- OUTRAS casas cobram pelo mesmo mercado — e de-vig exige todos os lados, que
-- a tabela `offers` não guarda (ela grava só a seleção turbinada).
--
-- A chave é (casa, evento_id, market_id, selecao): as casas Altenar
-- compartilham o `evento_id` (conferido: 8 casas no mesmo id), o que dispensa
-- casar nome de time entre elas.
CREATE TABLE IF NOT EXISTS mercados_casa (
    casa          TEXT NOT NULL,
    evento_id     TEXT NOT NULL,
    market_id     TEXT NOT NULL,
    market_nome   TEXT NOT NULL,
    selecao       TEXT NOT NULL,
    preco         REAL NOT NULL,
    capturado_em  TEXT NOT NULL,
    -- Identidade do evento (nome, kickoff ISO UTC, liga), pro casamento
    -- fuzzy com o `evento_id` de outra casa (Etapa 3 do plano de cobertura).
    -- Sem isto só 42 dos 546 eventos do pool tinham nome recuperável via
    -- `offers`. Colunas em SCHEMA porque banco NOVO já nasce com elas; banco
    -- existente ganha via `_migrar` (ver comentário lá — índice novo também).
    evento        TEXT,
    inicio_evento TEXT,
    liga          TEXT,
    PRIMARY KEY (casa, evento_id, market_id, selecao)
);

CREATE INDEX IF NOT EXISTS idx_mercados_casa_ev
    ON mercados_casa(evento_id, capturado_em);

-- Estado interno do loop (ex.: data do último resumo diário enviado).
CREATE TABLE IF NOT EXISTS estado (
    chave  TEXT PRIMARY KEY,
    valor  TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Liquidação: a aposta deu green ou red?
--
-- Até aqui o bot parava no alerta e nunca olhava o resultado do jogo. Estas
-- três tabelas são o que permite o resumo diário dizer o que aconteceu com o
-- que ele recomendou.
--
-- ⚠️ TODO horário aqui é ISO **UTC**, igual a `offers.inicio_evento` e ao
-- contrário de `_now()` (que é local, com offset). O corte de 24h do relatório
-- compara com `fim_partida`, então misturar as duas convenções desloca 3h de
-- apostas sem erro em lugar nenhum.
-- ---------------------------------------------------------------------------

-- Nosso evento -> evento do SofaScore, mais o estado de retry do casamento.
--
-- A chave NÃO é (casa, evento_id): as casas Altenar compartilham a mesma
-- partida, e chavear por casa faria a mesma busca fuzzy 8x pro mesmo jogo.
-- `evento_chave` normaliza os dois times + a data UTC do kickoff, colapsando
-- todas as casas numa linha e num fetch só.
CREATE TABLE IF NOT EXISTS eventos_liquidacao (
    evento_chave      TEXT PRIMARY KEY,
    evento            TEXT NOT NULL,   -- texto original, pra diagnóstico
    inicio_evento     TEXT,            -- ISO UTC
    sofascore_id      INTEGER,         -- NULL = ainda não casou
    match_score       REAL,            -- persistido pra auditar match marginal
    tentativas        INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa TEXT,
    motivo            TEXT,
    atualizado_em     TEXT NOT NULL
);

-- Os FATOS da partida. Uma linha por jogo real, compartilhada por todas as
-- ofertas de todas as casas.
--
-- Durável de propósito: `sofascore_cache` (stats_check) é cache com TTL de
-- outra camada, e depender dele faria o relatório precisar de rede num miss —
-- violando "o relatório nunca faz rede".
CREATE TABLE IF NOT EXISTS partidas_sofascore (
    sofascore_id  INTEGER PRIMARY KEY,
    status        TEXT NOT NULL,       -- finished | postponed | inprogress...
    home_nome     TEXT NOT NULL,
    away_nome     TEXT NOT NULL,
    gols_home     INTEGER,
    gols_away     INTEGER,
    ht_home       INTEGER,
    ht_away       INTEGER,
    -- {"ALL": {"escanteios": [5, 7], ...}, "1ST": {...}}
    -- Chave AUSENTE = desconhecido. NUNCA 0 — ver `Intervalo` em liquidacao.py.
    estatisticas  TEXT,                -- JSON
    fim_partida   TEXT NOT NULL,       -- ISO UTC
    capturado_em  TEXT NOT NULL
);

-- O VEREDITO por aposta.
--
-- Chaveado por `offer_id` (não `content_hash`): green/red é propriedade da
-- APOSTA, não da versão de preço. As unidades, que mudam a cada realerta,
-- vêm de `alertas_enviados` na hora de montar o relatório — é esse join que
-- produz "uma linha por alerta".
CREATE TABLE IF NOT EXISTS liquidacoes (
    offer_id      TEXT PRIMARY KEY,
    sofascore_id  INTEGER,
    resultado     TEXT NOT NULL,   -- green | red | void | desconhecido
    motivo        TEXT,            -- qual perna não resolveu, e por quê
    fim_partida   TEXT NOT NULL,   -- ISO UTC — a janela de 24h do relatório
    liquidado_em  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_liquidacoes_fim ON liquidacoes(fim_partida);
"""


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class Storage:
    def __init__(self, db_path: Path | str = config.DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # `check_same_thread=False`: a avaliação de value roda em
        # `asyncio.to_thread` (alerts.py) e consulta `mercados_casa` de lá pra
        # montar o consenso. Sem isto o sqlite3 recusa a conexão por ela ter
        # nascido em outra thread — e recusa mesmo sem concorrência nenhuma.
        #
        # É seguro aqui porque não há acesso simultâneo: o `to_thread` é
        # aguardado na hora, então a thread do loop fica parada enquanto a
        # worker usa a conexão. Se algum dia houver dois usos em paralelo, isto
        # precisa virar uma conexão por thread.
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._migrar()
        self._conn.commit()
        # Cache em memória da ponte fuzzy evento->pool (Etapa 3 do plano de
        # cobertura). Vive aqui, e não em `ProvedorConsenso`, porque este é
        # construído POR OFERTA (`pipeline.avaliar_oferta`) — um cache lá
        # morreria a cada oferta. `Storage` é compartilhado pelo ciclo
        # inteiro, então é o único lugar onde memoizar rende.
        #
        # `_pool_matchups=None` é "ainda não montado neste ciclo", distinto de
        # `[]` ("montei e o pool está vazio") — só o primeiro dispara nova
        # varredura. `invalidar_cache_pool` deve ser chamada sempre que
        # `mercados_casa` mudar (ver `main.py`, logo após `salvar_mercados_casa`).
        self._pool_matchups: list | None = None
        # Memoiza também os negativos (`None`): é isso que evita pagar N×M
        # (ofertas × eventos do pool) pelas ofertas que nunca vão casar.
        self._pool_cache: dict[tuple[str, str], str | None] = {}
        # Mesma chave, valor diferente: TODOS os ids do jogo, não só o melhor
        # (ver `mercados_para_consenso_do_jogo`). Cache separado porque a lista
        # vazia é resposta legítima e não pode ser confundida com "não
        # memoizado".
        self._pool_cache_multi: dict[tuple[str, str], list[str]] = {}

    def _migrar(self) -> None:
        """Colunas novas em banco já existente.

        `CREATE TABLE IF NOT EXISTS` não mexe numa tabela que já existe, então
        quem já rodava antes de `casa` continuaria sem a coluna. As ofertas
        antigas são todas da Betano — daí o default.
        """
        colunas = {r["name"] for r in self._conn.execute("PRAGMA table_info(offers)")}
        if "casa" not in colunas:
            self._conn.execute(
                "ALTER TABLE offers ADD COLUMN casa TEXT NOT NULL DEFAULT 'Betano'")
        if "limite_aposta" not in colunas:
            # Sem default: NULL é a resposta honesta para as ofertas antigas —
            # elas foram capturadas antes de o campo ser gravado, e 0 seria
            # lido como "a casa não deixa apostar nada".
            self._conn.execute("ALTER TABLE offers ADD COLUMN limite_aposta REAL")
        if "inicio_evento" not in colunas:
            # NULL nas ofertas antigas; `models.minutos_ate_inicio` cai no
            # `valido_ate` delas, que é o que existia até aqui. Elas se
            # corrigem sozinhas na próxima vez que a casa for raspada.
            self._conn.execute("ALTER TABLE offers ADD COLUMN inicio_evento TEXT")
        # Fora do SCHEMA de propósito: num banco que já existia, a coluna só
        # nasce no ALTER acima, e um CREATE INDEX antes dele quebraria a
        # abertura do banco.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_offers_inicio ON offers(active, inicio_evento)")

        alertas = {r["name"] for r in
                   self._conn.execute("PRAGMA table_info(alertas_enviados)")}
        if alertas and "alert_hash" not in alertas:
            # Fica NULL nos registros antigos e não dá pra preencher: o
            # `content_hash` deles não permite recuperar a `odd_boost`. A dedup
            # só precisa valer daqui pra frente, e a janela de
            # INTERVALO_MIN_REALERTA cobre a virada.
            self._conn.execute("ALTER TABLE alertas_enviados ADD COLUMN alert_hash TEXT")
        if alertas and "odd_boost" not in alertas:
            self._conn.execute("ALTER TABLE alertas_enviados ADD COLUMN odd_boost REAL")
        # O que a liquidação precisa e o alerta descartava. Sem default: NULL é
        # a resposta honesta pros alertas antigos — a stake deles não foi
        # gravada e não dá pra recuperar (depende de confiança e thresholds que
        # já podem ter mudado). O relatório mostra a linha e a deixa fora do
        # saldo, em vez de inventar um número.
        if alertas and "stake_unidades" not in alertas:
            self._conn.execute(
                "ALTER TABLE alertas_enviados ADD COLUMN stake_unidades REAL")
        if alertas and "stake_apostavel" not in alertas:
            # `stake.apostavel` é falso quando as unidades ficam abaixo de
            # STAKE_MIN_UNIDADES — o alerta saiu dizendo "não vale a pena".
            # Creditar stake nessas fabricaria histórico.
            self._conn.execute(
                "ALTER TABLE alertas_enviados ADD COLUMN stake_apostavel INTEGER")
        if alertas and "odd_justa" not in alertas:
            self._conn.execute(
                "ALTER TABLE alertas_enviados ADD COLUMN odd_justa REAL")
        if alertas and "confianca" not in alertas:
            self._conn.execute(
                "ALTER TABLE alertas_enviados ADD COLUMN confianca TEXT")

        mercados_casa = {r["name"] for r in
                         self._conn.execute("PRAGMA table_info(mercados_casa)")}
        if mercados_casa and "evento" not in mercados_casa:
            # Sem default: NULL é a resposta honesta pras linhas gravadas antes
            # da coluna existir — não dá pra recuperar o nome do evento delas,
            # e elas se renovam sozinhas (retenção de 12h). Mesmo raciocínio de
            # `limite_aposta` acima.
            self._conn.execute("ALTER TABLE mercados_casa ADD COLUMN evento TEXT")
        if mercados_casa and "inicio_evento" not in mercados_casa:
            self._conn.execute(
                "ALTER TABLE mercados_casa ADD COLUMN inicio_evento TEXT")
        if mercados_casa and "liga" not in mercados_casa:
            self._conn.execute("ALTER TABLE mercados_casa ADD COLUMN liga TEXT")
        # Fora do SCHEMA pelo mesmo motivo do índice de `offers` acima: num
        # banco que já existia, a coluna `evento` só nasce no ALTER logo
        # acima, e um CREATE INDEX antes dele quebraria a abertura do banco.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mercados_casa_evento"
            " ON mercados_casa(capturado_em, evento_id)")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Storage":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------

    def active_offers(self) -> dict[str, dict]:
        """Ofertas ativas indexadas por offer_id — é a base do diff."""
        rows = self._conn.execute("SELECT * FROM offers WHERE active = 1").fetchall()
        return {row["offer_id"]: dict(row) for row in rows}

    # --- mercados completos das casas (referência de consenso) ---------

    def salvar_mercados_casa(self, mercados: list[MercadoCasa]) -> int:
        """Grava/atualiza os mercados completos vistos num ciclo.

        `REPLACE` porque o preço muda o tempo todo e só o mais recente
        interessa: o consenso lê por janela de tempo, não por histórico.
        """
        if not mercados:
            return 0
        with self._conn:
            self._conn.executemany(
                """
                REPLACE INTO mercados_casa
                    (casa, evento_id, market_id, market_nome, selecao, preco,
                     capturado_em, evento, inicio_evento, liga)
                VALUES (:casa, :evento_id, :market_id, :market_nome, :selecao,
                        :preco, :capturado_em, :evento, :inicio_evento, :liga)
                """,
                [m.to_row() for m in mercados],
            )
        return len(mercados)

    def mercados_para_consenso(self, evento_id: str,
                               janela_horas: float) -> list[dict]:
        """Mercados do evento vistos nas últimas `janela_horas`, de todas as casas.

        A janela existe por causa do rodízio: só `CASAS_POR_CICLO` casas são
        raspadas por vez, então exigir que as casas tenham sido vistas no MESMO
        ciclo faria o consenso nunca se formar. Ver `value/consenso.py`.
        """
        corte = (datetime.now(timezone.utc) - timedelta(hours=janela_horas)
                 ).astimezone().isoformat(timespec="seconds")
        rows = self._conn.execute(
            """
            SELECT casa, market_id, market_nome, selecao, preco, capturado_em,
                   evento, inicio_evento, liga
              FROM mercados_casa
             WHERE evento_id = ? AND capturado_em >= ?
            """,
            (str(evento_id), corte),
        ).fetchall()
        return [dict(r) for r in rows]

    def eventos_do_pool(self, janela_horas: float) -> list[dict]:
        """Uma linha por `evento_id` distinto do pool, com nome/kickoff/liga.

        Matéria-prima da ponte fuzzy (Etapa 3, futura): sem nome de evento não
        há contra o que casar uma oferta de casa não-Altenar. Só entram linhas
        com `evento` preenchido — os registros antigos (gravados antes desta
        coluna existir) ficam de fora até se renovarem sozinhos.
        """
        corte = (datetime.now(timezone.utc) - timedelta(hours=janela_horas)
                 ).astimezone().isoformat(timespec="seconds")
        rows = self._conn.execute(
            """
            SELECT evento_id, evento, inicio_evento, liga
              FROM mercados_casa
             WHERE evento IS NOT NULL AND capturado_em >= ?
             GROUP BY evento_id
            """,
            (corte,),
        ).fetchall()
        return [dict(r) for r in rows]

    def resolver_evento_consenso(self, evento: str | None,
                                 inicio_evento: str | None,
                                 janela_horas: float) -> str | None:
        """`evento_id` do pool pra uma oferta de casa não-Altenar, via ponte
        fuzzy (`value.pool_eventos`) — ou None se não achou/não deu pra tentar.

        Cacheado no processo: a lista de `Matchup` sintéticos é montada uma
        vez por ciclo (`_pool_matchups`), e o resultado por
        `(evento normalizado, kickoff arredondado à hora)` é memoizado —
        inclusive quando é `None`, pra não pagar a varredura de novo por uma
        oferta que nunca vai casar. `invalidar_cache_pool` reseta os dois no
        único ponto em que `mercados_casa` muda.
        """
        if not evento or not inicio_evento:
            return None

        if self._pool_matchups is None:
            self._pool_matchups = _montar_matchups(self.eventos_do_pool(janela_horas))

        chave = self._chave_cache_pool(evento, inicio_evento)
        if chave is None:
            return None
        if chave in self._pool_cache:
            return self._pool_cache[chave]

        resultado = _resolver_evento_id(evento, inicio_evento, self._pool_matchups)
        self._pool_cache[chave] = resultado
        return resultado

    def mercados_para_consenso_do_jogo(self, evento: str | None,
                                       inicio_evento: str | None,
                                       janela_horas: float) -> list[dict]:
        """Linhas de TODOS os `evento_id` do pool que são o mesmo jogo.

        Cada fonte escreve sob o `evento_id` dela: as Altenar compartilham o
        seu, a Betano tem o próprio, a Superbet idem. Buscar por um id só
        entrega uma família de feed por vez — medido em 2026-08-14, os 347
        eventos do pool tinham exatamente UMA família cada, com fontes
        independentes deitadas no banco sem nunca se somarem.

        Mesmo cache de `resolver_evento_consenso`, com chave própria porque o
        valor é outro (lista, não id único).
        """
        if not evento or not inicio_evento:
            return []

        if self._pool_matchups is None:
            self._pool_matchups = _montar_matchups(self.eventos_do_pool(janela_horas))

        chave = self._chave_cache_pool(evento, inicio_evento)
        if chave is None:
            return []
        ids = self._pool_cache_multi.get(chave)
        if ids is None:
            ids = _resolver_evento_ids(evento, inicio_evento, self._pool_matchups)
            self._pool_cache_multi[chave] = ids

        linhas: list[dict] = []
        for evento_id in ids:
            linhas += self.mercados_para_consenso(evento_id, janela_horas)
        return linhas

    @staticmethod
    def _chave_cache_pool(evento: str, inicio_evento: str) -> tuple[str, str] | None:
        """`(nome normalizado, kickoff arredondado à hora)` — colapsa as várias
        skins Altenar/ofertas do mesmo jogo numa entrada só."""
        quando = _matcher._parse_data(inicio_evento)
        if quando is None:
            return None
        hora = quando.replace(minute=0, second=0, microsecond=0).isoformat()
        return (_matcher.normalizar(evento), hora)

    def invalidar_cache_pool(self) -> None:
        """Zera o cache da ponte fuzzy. Chamar sempre que `mercados_casa`
        mudar — hoje, só depois de `salvar_mercados_casa` (ver `main.py`)."""
        self._pool_matchups = None
        self._pool_cache = {}
        self._pool_cache_multi = {}

    def limpar_mercados_casa(self, mais_velhos_que_horas: float) -> int:
        """Poda o que já não serve pra consenso — a tabela cresce rápido."""
        corte = (datetime.now(timezone.utc)
                 - timedelta(hours=mais_velhos_que_horas)
                 ).astimezone().isoformat(timespec="seconds")
        with self._conn:
            cur = self._conn.execute(
                "DELETE FROM mercados_casa WHERE capturado_em < ?", (corte,))
        return cur.rowcount

    def apply_diff(self, result: DiffResult) -> None:
        """Grava o resultado de um ciclo: novas, alteradas, expiradas e heartbeat."""
        now = _now()
        with self._conn:
            for offer in result.novas:
                self._upsert(offer, now, is_new=True)
                self._log_history(offer.offer_id, offer.evento, offer.mercado,
                                  offer.odd_original, offer.odd_boost, "nova", now)

            for change in result.alteradas:
                offer = change.offer
                self._upsert(offer, now, is_new=False)
                self._log_history(offer.offer_id, offer.evento, offer.mercado,
                                  offer.odd_original, offer.odd_boost, "alterada", now)

            for offer in result.inalteradas:
                # As odds não mudaram, então não vai pro histórico. Mas os metadados
                # (url, validade, liga) precisam ser renovados assim mesmo: eles não
                # entram no content_hash, então sem isto uma oferta que nunca reajusta
                # a odd carregaria pra sempre o valor capturado da primeira vez —
                # inclusive um `valido_ate` errado se a Betano remarcar o jogo.
                # `limite_aposta` entra aqui pelo mesmo motivo: a casa pode
                # apertar o teto sem mexer na odd, e aí o content_hash não muda.
                self._conn.execute(
                    """
                    UPDATE offers
                       SET last_seen = ?, active = 1, url = ?, valido_ate = ?,
                           inicio_evento = ?, liga = ?, limite_aposta = ?
                     WHERE offer_id = ?
                    """,
                    (now, offer.url, offer.valido_ate, offer.inicio_evento,
                     offer.liga, offer.limite_aposta, offer.offer_id),
                )

            for row in result.expiradas:
                self._conn.execute(
                    "UPDATE offers SET active = 0, last_seen = ? WHERE offer_id = ?",
                    (now, row["offer_id"]),
                )
                self._log_history(row["offer_id"], row["evento"], row["mercado"],
                                  row["odd_original"], row["odd_boost"], "expirada", now)

    def _upsert(self, offer: Offer, now: str, *, is_new: bool) -> None:
        row = offer.to_row()
        self._conn.execute(
            """
            INSERT INTO offers (offer_id, content_hash, casa, fonte, evento_id, evento,
                                liga, mercado, odd_original, odd_boost, boost_pct,
                                valido_ate, inicio_evento, url, limite_aposta,
                                first_seen, last_seen, active)
            VALUES (:offer_id, :content_hash, :casa, :fonte, :evento_id, :evento,
                    :liga, :mercado, :odd_original, :odd_boost, :boost_pct,
                    :valido_ate, :inicio_evento, :url, :limite_aposta, :now, :now, 1)
            ON CONFLICT(offer_id) DO UPDATE SET
                content_hash  = excluded.content_hash,
                odd_original  = excluded.odd_original,
                odd_boost     = excluded.odd_boost,
                boost_pct     = excluded.boost_pct,
                valido_ate    = excluded.valido_ate,
                inicio_evento = excluded.inicio_evento,
                url           = excluded.url,
                limite_aposta = excluded.limite_aposta,
                last_seen     = excluded.last_seen,
                active        = 1
            """,
            {**row, "now": now},
        )

    def _log_history(self, offer_id: str, evento: str, mercado: str,
                     odd_original: float | None, odd_boost: float | None,
                     tipo: str, now: str) -> None:
        self._conn.execute(
            """
            INSERT INTO offer_history (offer_id, evento, mercado, odd_original,
                                       odd_boost, evento_tipo, observado_em)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (offer_id, evento, mercado, odd_original, odd_boost, tipo, now),
        )

    # ------------------------------------------------------------------

    def record_run(self, *, ofertas: int, novas: int = 0, alteradas: int = 0,
                   expiradas: int = 0, erro: str | None = None) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO scrape_runs (executado_em, ofertas, novas, alteradas, expiradas, erro)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (_now(), ofertas, novas, alteradas, expiradas, erro),
            )

    def consecutive_failed_runs(self) -> int:
        """Quantos ciclos seguidos (do mais recente pra trás) vieram vazios ou com erro.

        É o detector de quebra: se a Betano mudar a estrutura, o scraper passa a
        capturar 0 ofertas silenciosamente — isso não pode ser confundido com
        "não há promoção no ar agora".
        """
        rows = self._conn.execute(
            "SELECT ofertas, erro FROM scrape_runs ORDER BY id DESC LIMIT ?",
            (config.BREAKAGE_ALERT_AFTER_EMPTY_RUNS,),
        ).fetchall()
        streak = 0
        for row in rows:
            if row["erro"] or row["ofertas"] == 0:
                streak += 1
            else:
                break
        return streak

    # ------------------------------------------------------------------
    # Notificação
    # ------------------------------------------------------------------

    def ja_alertou(self, offer_id: str, edge_pct: float | None = None,
                   intervalo_min_realerta: int = 0,
                   melhora_min_pp: float = 0.0,
                   max_realertas: int = 1) -> bool:
        """Uma oferta alerta uma vez. Só realerta com melhora material.

        `alert_hash` não serve mais de guarda (2026-08-07): ele deixou de
        incluir `odd_boost` justamente pra parar o flood, e por isso passou a
        ser 1:1 com `offer_id` — usá-lo como chave de "já vi isso" bloquearia
        SEMPRE a partir do segundo envio, sem chance de realerta legítimo.
        A guarda agora é toda sobre `edge_pct`:

        1. Nunca alertou → libera.
        2. Já alertou `max_realertas` vezes → bloqueia, ponto final.
        3. `edge_pct` atual tem que superar o MELHOR já alertado (não o
           último) em pelo menos `melhora_min_pp` pontos — comparar contra o
           último deixaria uma odd oscilando pra cima e pra baixo dentro da
           margem reenviar a cada volta.
        4. Mesmo com melhora suficiente, respeita `intervalo_min_realerta`
           como piso de tempo desde o ÚLTIMO envio — é a rede contra campo
           volátil que ainda não conhecemos, mesmo papel que já tinha antes.
        """
        row = self._conn.execute(
            "SELECT MAX(edge_pct) AS melhor, COUNT(*) AS n, MAX(enviado_em) AS ultimo"
            "  FROM alertas_enviados WHERE offer_id = ?",
            (offer_id,),
        ).fetchone()
        if row is None or not row["n"]:
            return False

        if row["n"] >= max_realertas:
            return True

        melhor = row["melhor"]
        # Sem edge registrado (alerta antigo, pré-migração) a comparação não
        # é possível — aí bloqueia, que é o lado seguro.
        if melhor is None or edge_pct is None or edge_pct < melhor + melhora_min_pp:
            return True

        if intervalo_min_realerta > 0 and row["ultimo"]:
            corte = (datetime.now(timezone.utc).astimezone()
                     - timedelta(minutes=intervalo_min_realerta)).isoformat(
                         timespec="seconds")
            if row["ultimo"] > corte:
                return True

        return False

    def registrar_alerta(self, offer_id: str, content_hash: str,
                         alert_hash: str, edge_pct: float | None = None,
                         odd_boost: float | None = None,
                         stake_unidades: float | None = None,
                         stake_apostavel: bool | None = None,
                         odd_justa: float | None = None,
                         confianca: str | None = None) -> None:
        """Grava o alerta enviado.

        Os quatro últimos campos existem para a liquidação: o P&L do relatório
        diário usa a stake que FOI recomendada, nunca uma recalculada. Kelly,
        thresholds e a tabela de confiança mudam com o tempo; o relatório é o
        registro do que o bot mandou fazer naquele momento.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO alertas_enviados
                       (offer_id, content_hash, alert_hash, odd_boost,
                        edge_pct, enviado_em, stake_unidades, stake_apostavel,
                        odd_justa, confianca)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id, content_hash) DO UPDATE SET
                    alert_hash      = excluded.alert_hash,
                    odd_boost       = excluded.odd_boost,
                    edge_pct        = excluded.edge_pct,
                    enviado_em      = excluded.enviado_em,
                    stake_unidades  = excluded.stake_unidades,
                    stake_apostavel = excluded.stake_apostavel,
                    odd_justa       = excluded.odd_justa,
                    confianca       = excluded.confianca
                """,
                (offer_id, content_hash, alert_hash, odd_boost, edge_pct, _now(),
                 stake_unidades,
                 None if stake_apostavel is None else int(stake_apostavel),
                 odd_justa, confianca),
            )

    def _clausula_pendencia(self, reavaliar_apos_minutos: int,
                            agora: datetime | None = None) -> tuple[str, list[object]]:
        """WHERE da fila + parâmetros. Compartilhado pelo SELECT e pelo COUNT,
        senão o log "N pendentes" contaria uma fila diferente da que roda.

        Duas regras além da pendência em si:

        - **jogo iniciado nunca entra.** O diff já expira essas ofertas, mas a
          fila é o último lugar onde vale gastar request da Pinnacle com um
          preço que não dá mais pra pegar.
        - **TTL por distância.** Jogo dentro da janela útil reavalia a cada
          `reavaliar_apos_minutos`; jogo além dela, a cada
          `REAVALIAR_LONGE_MINUTOS`. Reavaliar de hora em hora um jogo de
          amanhã é pagar pra ouvir o mesmo "sem mercado": eram 699 das 800
          ofertas ativas nessa faixa, entupindo um teto de 60 por ciclo com
          uma fila de 114 que nunca drenava.
        """
        agora = (agora or datetime.now(timezone.utc)).astimezone(timezone.utc)
        params: list[object] = []

        if reavaliar_apos_minutos > 0:
            def corte(minutos: int) -> str:
                return (agora.astimezone()
                        - timedelta(minutes=minutos)).isoformat(timespec="seconds")

            horizonte = (agora + timedelta(
                hours=config.HORIZONTE_AVALIACAO_HORAS)).isoformat(timespec="seconds")
            longe = max(reavaliar_apos_minutos, config.REAVALIAR_LONGE_MINUTOS)
            condicao = """(a.avaliado_em IS NULL
                           OR a.avaliado_em < CASE
                                WHEN o.inicio_evento IS NULL OR o.inicio_evento <= ?
                                THEN ? ELSE ? END)"""
            params += [horizonte, corte(reavaliar_apos_minutos), corte(longe)]
        else:
            condicao = "a.avaliado_em IS NULL"

        # Sem `inicio_evento` (oferta gravada antes da coluna existir, ou casa
        # que não publica horário) a oferta segue elegível: não dá pra afirmar
        # que começou.
        condicao += " AND (o.inicio_evento IS NULL OR o.inicio_evento > ?)"
        params.append(agora.isoformat(timespec="seconds"))
        return condicao, params

    def pendentes_de_avaliacao(self, limite: int,
                               reavaliar_apos_minutos: int = 0) -> list[dict]:
        """Ofertas ativas esperando avaliação, as mais urgentes na frente.

        Uma oferta está pendente quando:

        - nunca foi avaliada NESTA versão (`content_hash`) — cobre tanto a
          oferta nova quanto a que teve a odd reajustada; ou
        - foi avaliada há mais tempo que o TTL da faixa dela.

        É este SELECT que garante que o teto por ciclo não vira descarte: quem
        não coube segue pendente e aparece no próximo ciclo.
        """
        condicao, params = self._clausula_pendencia(reavaliar_apos_minutos)
        horizonte = (datetime.now(timezone.utc)
                     + timedelta(hours=config.HORIZONTE_AVALIACAO_HORAS)
                     ).isoformat(timespec="seconds")
        rows = self._conn.execute(
            f"""
            SELECT o.*
              FROM offers o
              LEFT JOIN avaliacoes a
                ON a.offer_id = o.offer_id AND a.content_hash = o.content_hash
             WHERE o.active = 1 AND {condicao}
             -- 1) Janela útil primeiro. A Pinnacle publica mercado perto do
             --    jogo (67% entre 3 e 12h; 3% acima de 48h — ver README), então
             --    é só aí que a avaliação tem chance de virar alerta.
             --
             -- 2) Inéditas antes das reavaliações — MAS depois da janela, e a
             --    ordem entre estes dois critérios é o conserto principal: com
             --    o ineditismo em primeiro lugar, toda reavaliação ia pro fim
             --    da fila, e a reavaliação é justamente o momento em que o
             --    mercado da Pinnacle abriu e o edge finalmente existe. Era o
             --    que produzia alerta de oferta com 15h de vida saindo 19 min
             --    antes do apito.
             --
             -- 3) Dentro da janela, a mais urgente primeiro.
             --
             -- 4) `last_seen`/boost seguem como desempate: o rodízio deixa uma
             --    captura envelhecer até ~1h, e edge de dado velho é sobre um
             --    preço que a casa já mexeu. A casa inteira compartilha o mesmo
             --    `last_seen`, então o boost ainda ordena dentro de cada casa.
             ORDER BY (o.inicio_evento IS NULL OR o.inicio_evento > ?) ASC,
                      (a.avaliado_em IS NOT NULL) ASC,
                      o.inicio_evento ASC,
                      o.last_seen DESC,
                      o.boost_pct DESC
             LIMIT ?
            """,
            [*params, horizonte, limite],
        ).fetchall()
        return [dict(r) for r in rows]

    def contar_pendentes(self, reavaliar_apos_minutos: int = 0) -> int:
        """Tamanho da fila, pra logar honestamente quanto ficou pra trás."""
        condicao, params = self._clausula_pendencia(reavaliar_apos_minutos)
        return self._conn.execute(
            f"""
            SELECT COUNT(*) AS n FROM offers o
              LEFT JOIN avaliacoes a
                ON a.offer_id = o.offer_id AND a.content_hash = o.content_hash
             WHERE o.active = 1 AND {condicao}
            """,
            params,
        ).fetchone()["n"]

    def registrar_avaliacao(self, offer_id: str, content_hash: str,
                            status: str | None = None) -> None:
        """Marca a versão como avaliada — inclusive quando não deu pra avaliar.

        Registrar a falha é o que impede uma oferta sem cobertura (cartões,
        aces) de ser reprocessada em todo ciclo pra sempre.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO avaliacoes (offer_id, content_hash, avaliado_em, status)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(offer_id, content_hash) DO UPDATE SET
                    avaliado_em = excluded.avaliado_em,
                    status      = excluded.status
                """,
                (offer_id, content_hash, _now(), status),
            )

    def registrar_veredito_curadoria(self, offer_id: str, content_hash: str,
                                     veredito) -> None:
        """Grava o veredito de `curadoria.avaliar` — mesmo em modo sombra.

        Recebe o `Veredito` inteiro (não campos soltos) porque quem chama
        (`alerts.py`) já tem o objeto pronto, e `sinais` é um dict livre que
        não vale a pena espalhar em parâmetros posicionais.
        """
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO veredito_curadoria
                       (offer_id, content_hash, decisao, motivo,
                        confianca_original, confianca_final, modo,
                        sinais_json, avaliado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id, content_hash) DO UPDATE SET
                    decisao             = excluded.decisao,
                    motivo              = excluded.motivo,
                    confianca_original  = excluded.confianca_original,
                    confianca_final     = excluded.confianca_final,
                    modo                = excluded.modo,
                    sinais_json         = excluded.sinais_json,
                    avaliado_em         = excluded.avaliado_em
                """,
                (offer_id, content_hash, veredito.decisao, veredito.motivo,
                 veredito.confianca_original, veredito.confianca_final,
                 veredito.modo, json.dumps(veredito.sinais, ensure_ascii=False),
                 _now()),
            )

    def get_estado(self, chave: str) -> str | None:
        row = self._conn.execute(
            "SELECT valor FROM estado WHERE chave = ?", (chave,)).fetchone()
        return row["valor"] if row else None

    def set_estado(self, chave: str, valor: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO estado (chave, valor) VALUES (?, ?)
                ON CONFLICT(chave) DO UPDATE SET valor = excluded.valor
                """,
                (chave, valor),
            )

    def resumo_24h(self) -> dict[str, int]:
        """Números do heartbeat diário, a partir do log de ciclos."""
        corte = (datetime.now(timezone.utc).astimezone() - timedelta(hours=24)).isoformat(
            timespec="seconds")
        row = self._conn.execute(
            """
            SELECT COUNT(*)                                   AS ciclos,
                   COALESCE(SUM(novas), 0)                    AS novas,
                   COALESCE(SUM(alteradas), 0)                AS alteradas,
                   COALESCE(SUM(expiradas), 0)                AS expiradas,
                   COALESCE(SUM(erro IS NOT NULL), 0)         AS ciclos_com_erro
              FROM scrape_runs
             WHERE executado_em >= ?
            """,
            (corte,),
        ).fetchone()
        ativas = self._conn.execute(
            "SELECT COUNT(*) AS n FROM offers WHERE active = 1").fetchone()["n"]
        return {**{k: row[k] for k in row.keys()}, "ativas": ativas}

    def stats(self) -> dict[str, int]:
        cur = self._conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(active), 0) AS ativas FROM offers"
        ).fetchone()
        hist = self._conn.execute("SELECT COUNT(*) AS n FROM offer_history").fetchone()
        return {"total": cur["total"], "ativas": cur["ativas"], "historico": hist["n"]}

    # ------------------------------------------------------------------
    # Liquidação (green/red)
    #
    # ⚠️ Tudo aqui compara com `offers.inicio_evento` e `liquidacoes.fim_partida`,
    # que são ISO **UTC**. `_now()` é local (com offset) e NÃO serve de corte
    # nestas queries — a comparação lexicográfica do SQLite entre "+00:00" e
    # "-03:00" deslocaria 3h sem erro nenhum.
    # ------------------------------------------------------------------

    @staticmethod
    def _agora_utc(agora: datetime | None = None) -> datetime:
        """Normaliza para UTC, que é a convenção de `inicio_evento`/`fim_partida`.

        ⚠️ Um `agora` INGÊNUO é lido como horário LOCAL, não como UTC. É quem
        vem de `datetime.now()` — `alerts.talvez_resumo_diario` usa exatamente
        isso. Tratar ingênuo como UTC deslocaria a janela de 24h em uma
        diferença de fuso inteira (3h aqui), sem erro em lugar nenhum.
        """
        if agora is None:
            return datetime.now(timezone.utc)
        if agora.tzinfo is None:
            agora = agora.astimezone()   # interpreta como local
        return agora.astimezone(timezone.utc)

    def pendentes_de_liquidacao(self, limite: int, atraso_minutos: int,
                                agora: datetime | None = None) -> list[dict]:
        """Apostas alertadas cujo jogo já deve ter acabado e ainda sem veredito.

        Uma linha por `offer_id` (não por alerta): o veredito é da aposta, e
        liquidar a mesma oferta uma vez por realerta gastaria request à toa.

        Não filtra `offers.active`: a oferta expira quando o jogo começa, mas o
        alerta continua valendo — filtrar por `active` devolveria lista vazia
        justamente para tudo que já dá pra liquidar.

        O filtro de backoff NÃO mora aqui: ele é por partida normalizada, e
        normalizar nome de time é trabalho do `matcher`, não do storage. Quem
        chama cruza esta lista com `eventos_em_backoff()`.
        """
        corte = (self._agora_utc(agora)
                 - timedelta(minutes=atraso_minutos)).isoformat(timespec="seconds")
        cur = self._conn.execute(
            """
            SELECT DISTINCT o.offer_id, o.evento, o.mercado, o.casa,
                            o.inicio_evento
              FROM alertas_enviados a
              JOIN offers o ON o.offer_id = a.offer_id
         LEFT JOIN liquidacoes l ON l.offer_id = o.offer_id
             WHERE l.offer_id IS NULL
               AND o.inicio_evento IS NOT NULL
               AND o.inicio_evento <= ?
             ORDER BY o.inicio_evento
             LIMIT ?
            """,
            (corte, limite),
        )
        return [dict(r) for r in cur]

    def eventos_em_backoff(self, agora: datetime | None = None) -> set[str]:
        """Chaves de partida que falharam o casamento e ainda estão de castigo."""
        agora_iso = self._agora_utc(agora).isoformat(timespec="seconds")
        cur = self._conn.execute(
            "SELECT evento_chave FROM eventos_liquidacao"
            " WHERE proxima_tentativa IS NOT NULL AND proxima_tentativa > ?",
            (agora_iso,),
        )
        return {r["evento_chave"] for r in cur}

    def evento_liquidacao(self, evento_chave: str) -> dict | None:
        """Estado de casamento de uma partida (id do SofaScore, tentativas)."""
        row = self._conn.execute(
            "SELECT * FROM eventos_liquidacao WHERE evento_chave = ?",
            (evento_chave,)).fetchone()
        return dict(row) if row else None

    def partida(self, sofascore_id: int) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM partidas_sofascore WHERE sofascore_id = ?",
            (sofascore_id,)).fetchone()
        return dict(row) if row else None

    def registrar_partida(self, sofascore_id: int, *, status: str,
                          home_nome: str, away_nome: str,
                          gols: tuple[int | None, int | None],
                          ht: tuple[int | None, int | None],
                          estatisticas_json: str | None,
                          fim_partida: str) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO partidas_sofascore
                       (sofascore_id, status, home_nome, away_nome,
                        gols_home, gols_away, ht_home, ht_away,
                        estatisticas, fim_partida, capturado_em)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sofascore_id) DO UPDATE SET
                    status       = excluded.status,
                    gols_home    = excluded.gols_home,
                    gols_away    = excluded.gols_away,
                    ht_home      = excluded.ht_home,
                    ht_away      = excluded.ht_away,
                    estatisticas = excluded.estatisticas,
                    capturado_em = excluded.capturado_em
                """,
                (sofascore_id, status, home_nome, away_nome, gols[0], gols[1],
                 ht[0], ht[1], estatisticas_json, fim_partida, _now()),
            )

    def registrar_liquidacao(self, offer_id: str, *, resultado: str,
                             motivo: str | None, fim_partida: str,
                             sofascore_id: int | None = None) -> None:
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO liquidacoes
                       (offer_id, sofascore_id, resultado, motivo,
                        fim_partida, liquidado_em)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(offer_id) DO UPDATE SET
                    sofascore_id = excluded.sofascore_id,
                    resultado    = excluded.resultado,
                    motivo       = excluded.motivo,
                    fim_partida  = excluded.fim_partida,
                    liquidado_em = excluded.liquidado_em
                """,
                (offer_id, sofascore_id, resultado, motivo, fim_partida, _now()),
            )

    def registrar_tentativa_evento(self, evento_chave: str, evento: str, *,
                                   inicio_evento: str | None = None,
                                   sofascore_id: int | None = None,
                                   match_score: float | None = None,
                                   motivo: str | None = None,
                                   backoff_minutos: int = 30,
                                   agora: datetime | None = None) -> int:
        """Marca uma tentativa de casar a partida e agenda a próxima.

        Sem isto, uma partida que nunca casa (amistoso obscuro, nome que o
        matcher não resolve) volta pra fila a cada ciclo e queima o orçamento
        de liquidação para sempre. Devolve o total de tentativas acumuladas.

        `evento_chave` é a partida normalizada (times + data), então as N casas
        que publicam o mesmo jogo compartilham uma linha e um backoff só.
        """
        proxima = (self._agora_utc(agora)
                   + timedelta(minutes=backoff_minutos)).isoformat(timespec="seconds")
        with self._conn:
            self._conn.execute(
                """
                INSERT INTO eventos_liquidacao
                       (evento_chave, evento, inicio_evento, sofascore_id,
                        match_score, tentativas, proxima_tentativa, motivo,
                        atualizado_em)
                VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)
                ON CONFLICT(evento_chave) DO UPDATE SET
                    -- COALESCE pra não apagar um id já casado numa tentativa
                    -- posterior que só falhou de rede.
                    sofascore_id      = COALESCE(excluded.sofascore_id,
                                                 eventos_liquidacao.sofascore_id),
                    match_score       = COALESCE(excluded.match_score,
                                                 eventos_liquidacao.match_score),
                    tentativas        = eventos_liquidacao.tentativas + 1,
                    proxima_tentativa = excluded.proxima_tentativa,
                    motivo            = excluded.motivo,
                    atualizado_em     = excluded.atualizado_em
                """,
                (evento_chave, evento, inicio_evento, sofascore_id, match_score,
                 proxima, motivo, _now()),
            )
        row = self._conn.execute(
            "SELECT tentativas FROM eventos_liquidacao WHERE evento_chave = ?",
            (evento_chave,)).fetchone()
        return row["tentativas"] if row else 1

    def apostas_liquidadas_24h(self, agora: datetime | None = None) -> list[dict]:
        """UMA LINHA POR ALERTA de jogos encerrados nas últimas 24h.

        O join com `alertas_enviados` (PK `(offer_id, content_hash)`) é o que
        produz uma linha por alerta de graça: um realerta com odd melhor é
        outro `content_hash`, logo outra linha.
        """
        corte = (self._agora_utc(agora)
                 - timedelta(hours=24)).isoformat(timespec="seconds")
        cur = self._conn.execute(
            """
            SELECT a.enviado_em, a.odd_boost, a.stake_unidades,
                   a.stake_apostavel, a.edge_pct, a.confianca,
                   o.evento, o.mercado, o.casa,
                   l.resultado, l.motivo, l.fim_partida
              FROM liquidacoes l
              JOIN alertas_enviados a ON a.offer_id = l.offer_id
              JOIN offers o           ON o.offer_id = l.offer_id
             WHERE l.fim_partida >= ?
             ORDER BY l.fim_partida, o.evento, a.enviado_em
            """,
            (corte,),
        )
        return [dict(r) for r in cur]
