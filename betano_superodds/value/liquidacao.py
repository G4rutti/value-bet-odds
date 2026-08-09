"""Liquidação: a aposta que o bot alertou deu green ou red?

Até aqui o projeto parava no alerta — calculava edge, mandava a mensagem e
nunca mais olhava. Este módulo fecha o ciclo: pega o resultado real da partida
no SofaScore e decide o veredito de cada aposta, pro resumo diário poder dizer
o que aconteceu com o que ele recomendou.

O que este módulo NÃO é
-----------------------
Não é precificação. Ele nunca produz odd justa, nunca alimenta `fair_odds` e
nunca decide se uma aposta vale a pena. Cobertura de *liquidação* e cobertura
de *precificação* são perguntas diferentes: a Pinnacle não publicar cartão não
impede o SofaScore de contar quantos cartões saíram. Confundir as duas é como
nasce o bug "mercado restrito × referência ampla" (ver README) — por isso aqui
nenhum `SEM_COBERTURA` do `market_parser` é removido e nenhum `Leg.suportado`
é virado.

Por que não reusar `stats_check._predicado_da_perna`
---------------------------------------------------
Parece o reuso óbvio e é uma regressão. `_freq_conjunta` aborta o combo inteiro
assim que uma perna não é coberta (`stats_check.py`), então ampliar a cobertura
lá muda *quais* combos ganham `freq_conjunta_historica`, o que muda
`diverge_da_estimativa_independente`, o que muda o rebaixamento de confiança em
`pipeline.py`, o que **muda a stake de Kelly**. Uma feature de relatório não
pode mexer em quanto se aposta. Além disso, lá o predicado roda sobre um
histórico que só tem placar; aqui roda sobre UMA partida que tem placar *e*
estatística. Domínios diferentes.

A ideia que carrega o módulo: `Intervalo`
-----------------------------------------
`/event/{id}/statistics` **omite** a estatística que vale zero — não existe item
"Red cards" quando não houve vermelho. Ausência é ambígua (0 ou não-reportado),
e ler ausência como 0 inventaria green. Então nada aqui é escalar: tudo é
intervalo `[mínimo, máximo]`, e o veredito só sai quando é **invariante sob o
desconhecido**. Não é política de arredondamento, é aritmética.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from .config import MATCH_MIN_SCORE
from .market_parser import Leg, parse_mercado
from .matcher import (_score_nome, encontrar_evento, genero_conflita, normalizar,
                      split_times, uf_conflita)
from .models import Matchup
# `_matchups_candidatos` é privado do stats_check, mas é a mesma camada e o
# mesmo pacote — e reimplementar "buscar time -> listar jogos -> montar
# Matchup" aqui seria um segundo caminho de casamento pra divergir do primeiro.
from .stats_check import SofaScoreClient, SofaScoreError, _matchups_candidatos

log = logging.getLogger(__name__)

INFINITO = float("inf")

# Quanto tempo depois do apito inicial a partida é considerada encerrada, pra
# efeito da janela de 24h do relatório. 90 de jogo + intervalo + acréscimos.
DURACAO_PARTIDA_MIN = 105

# Períodos como o SofaScore nomeia. O `market_parser` recusa mercado de 2º
# tempo, então "2ND" nunca é consultado — mas é guardado porque vem de graça.
PERIODO_JOGO = "ALL"
PERIODO_1T = "1ST"

# Nome de exibição do SofaScore -> nome interno. Nome NÃO mapeado é tratado
# como ausente (desconhecido), nunca como zero.
_STATS_SOFASCORE = {
    "corner kicks": "escanteios",
    "yellow cards": "cartoes_amarelos",
    "red cards": "cartoes_vermelhos",
    "shots on target": "chutes_no_gol",
    "fouls": "faltas",
    "offsides": "impedimentos",
}


# ------------------------------------------------------------------
# aritmética com desconhecido
# ------------------------------------------------------------------

@dataclass(frozen=True)
class Intervalo:
    """O que se sabe sobre um número. `maximo=inf` = sem teto conhecido.

    Um valor reportado vira `Intervalo(n, n)`. Um valor ausente e ambíguo vira
    `Intervalo(0, inf)`. Toda comparação devolve `None` quando o desconhecido
    é quem decide — e é isso que impede green inventado.
    """

    minimo: float
    maximo: float

    @property
    def exato(self) -> bool:
        return self.minimo == self.maximo

    def __add__(self, outro: "Intervalo") -> "Intervalo":
        return Intervalo(self.minimo + outro.minimo, self.maximo + outro.maximo)

    def acima_de(self, linha: float) -> bool | None:
        if self.minimo > linha:
            return True
        if self.maximo < linha:
            return False
        return None

    def abaixo_de(self, linha: float) -> bool | None:
        if self.maximo < linha:
            return True
        if self.minimo > linha:
            return False
        return None

    def igual_a(self, alvo: float) -> bool | None:
        if self.exato:
            return self.minimo == alvo
        if alvo < self.minimo or alvo > self.maximo:
            return False
        return None

    def maior_que(self, outro: "Intervalo") -> bool | None:
        """Compara dois intervalos. Usado no 1x2 de escanteios/cartões."""
        if self.minimo > outro.maximo:
            return True
        if self.maximo < outro.minimo:
            return False
        return None

    @staticmethod
    def exatamente(valor: float) -> "Intervalo":
        return Intervalo(valor, valor)

    @staticmethod
    def desconhecido() -> "Intervalo":
        return Intervalo(0.0, INFINITO)


# ------------------------------------------------------------------
# o resultado da partida
# ------------------------------------------------------------------

@dataclass(frozen=True)
class ResultadoPartida:
    status: str                      # "finished" | "postponed" | "inprogress"...
    home_nome: str
    away_nome: str
    # período -> (gols casa, gols fora). Chave ausente = placar não publicado.
    gols: dict[str, tuple[int, int]] = field(default_factory=dict)
    # período -> nome interno -> (casa, fora)
    stats: dict[str, dict[str, tuple[Intervalo, Intervalo]]] = field(default_factory=dict)

    @property
    def encerrada(self) -> bool:
        return self.status == "finished"

    def placar(self, periodo: str) -> tuple[int, int] | None:
        return self.gols.get(periodo)

    def estatistica(self, nome: str, periodo: str
                    ) -> tuple[Intervalo, Intervalo] | None:
        """`None` = não reportada. Nunca devolve zero por ausência."""
        return self.stats.get(periodo, {}).get(nome)

    def cartoes(self, periodo: str) -> tuple[Intervalo, Intervalo] | None:
        """Cartões por lado = amarelos + vermelhos.

        O vermelho costuma vir ausente (o SofaScore omite estatística zerada),
        e ausente é ambíguo. Somar `Intervalo(0, inf)` faz o total virar
        `[amarelos, inf)`, que ainda decide todo "over" e todo "under" abaixo
        do número de amarelos — e se recusa a decidir o resto.
        """
        am = self.estatistica("cartoes_amarelos", periodo)
        if am is None:
            return None
        ver = self.estatistica("cartoes_vermelhos", periodo)
        if ver is None:
            ver = (Intervalo.desconhecido(), Intervalo.desconhecido())
        return (am[0] + ver[0], am[1] + ver[1])

    def lado_do_time(self, nome: str) -> str | None:
        """Nome de time -> "home"/"away". Mesma normalização do matcher."""
        alvo = normalizar(nome)
        if not alvo:
            return None
        casa, fora = normalizar(self.home_nome), normalizar(self.away_nome)
        # Contenção nos dois sentidos: a casa escreve "FC Salzburgo" e o
        # SofaScore "Salzburg"; nenhum contém o outro inteiro, então o
        # empate é decidido por quem tem mais token em comum.
        if alvo == casa:
            return "home"
        if alvo == fora:
            return "away"
        em_casa = alvo in casa or casa in alvo
        em_fora = alvo in fora or fora in alvo
        if em_casa and not em_fora:
            return "home"
        if em_fora and not em_casa:
            return "away"
        if em_casa and em_fora:
            return None  # ambíguo: não chuta
        return _lado_por_tokens(alvo, casa, fora)


def _lado_por_tokens(alvo: str, casa: str, fora: str) -> str | None:
    """Último recurso: qual lado divide mais palavras com o nome procurado."""
    t_alvo = set(alvo.split())
    if not t_alvo:
        return None
    n_casa = len(t_alvo & set(casa.split()))
    n_fora = len(t_alvo & set(fora.split()))
    if n_casa > n_fora:
        return "home"
    if n_fora > n_casa:
        return "away"
    return None


# ------------------------------------------------------------------
# leitura do payload do SofaScore
# ------------------------------------------------------------------

def _intervalos_do_item(item: dict) -> tuple[Intervalo, Intervalo] | None:
    """Um item de estatística -> par de intervalos exatos.

    Valor não numérico (posse de bola vem como "62%") é descartado: melhor
    ausente que um número que quer dizer outra coisa.
    """
    try:
        casa = float(item.get("home"))
        fora = float(item.get("away"))
    except (TypeError, ValueError):
        return None
    return (Intervalo.exatamente(casa), Intervalo.exatamente(fora))


def _parse_estatisticas(payload: dict | None
                        ) -> dict[str, dict[str, tuple[Intervalo, Intervalo]]]:
    saida: dict[str, dict[str, tuple[Intervalo, Intervalo]]] = {}
    for bloco in (payload or {}).get("statistics", []) or []:
        periodo = bloco.get("period")
        if not periodo:
            continue
        do_periodo = saida.setdefault(periodo, {})
        for grupo in bloco.get("groups", []) or []:
            for item in grupo.get("statisticsItems", []) or []:
                interno = _STATS_SOFASCORE.get((item.get("name") or "").lower())
                if not interno or interno in do_periodo:
                    continue
                par = _intervalos_do_item(item)
                if par is not None:
                    do_periodo[interno] = par
    return saida


def _parse_placar(evento: dict) -> dict[str, tuple[int, int]]:
    hs, as_ = evento.get("homeScore") or {}, evento.get("awayScore") or {}
    gols: dict[str, tuple[int, int]] = {}
    # `normaltime` é o placar dos 90 minutos. Usar `current` traria prorrogação
    # e pênaltis, que não é o que mercado de gol resolve.
    gh, ga = hs.get("normaltime"), as_.get("normaltime")
    if gh is not None and ga is not None:
        gols[PERIODO_JOGO] = (int(gh), int(ga))
    h1, a1 = hs.get("period1"), as_.get("period1")
    if h1 is not None and a1 is not None:
        gols[PERIODO_1T] = (int(h1), int(a1))
    return gols


def montar_resultado(evento_payload: dict,
                     estatisticas_payload: dict | None) -> ResultadoPartida | None:
    """Payloads crus do SofaScore -> `ResultadoPartida`."""
    ev = evento_payload.get("event") or evento_payload
    home = (ev.get("homeTeam") or {}).get("name")
    away = (ev.get("awayTeam") or {}).get("name")
    status = ((ev.get("status") or {}).get("type") or "").lower()
    if not home or not away or not status:
        return None
    return ResultadoPartida(
        status=status, home_nome=home, away_nome=away,
        gols=_parse_placar(ev),
        stats=_parse_estatisticas(estatisticas_payload),
    )


# ------------------------------------------------------------------
# tokens de 1X2 vindos do market_parser
# ------------------------------------------------------------------

_TOKEN_TIME = re.compile(r"\{time:(?P<nome>[^}]+)\}")


def resolver_lado_1x2(token: str, partida: ResultadoPartida) -> str | None:
    """`{home}` / `{away}` / `{time:Nome}` / `Draw` -> "home"/"away"/"draw".

    O `market_parser` emite estes tokens porque o lado só é resolvível depois
    do match (`_token_1x2`). Lá em `fair_odds` eles viram nome de time da
    Pinnacle; aqui viram um LADO, que é tudo que a liquidação precisa — e é por
    isso que a duplicação some pra três literais e uma normalização.
    """
    token = (token or "").strip()
    if not token:
        return None
    if re.fullmatch(r"draw|empate|x", token, re.IGNORECASE):
        return "draw"
    if token == "{home}":
        return "home"
    if token == "{away}":
        return "away"
    m = _TOKEN_TIME.fullmatch(token)
    if m:
        return partida.lado_do_time(m.group("nome"))
    return partida.lado_do_time(token)


def _vencedor(placar: tuple[int, int]) -> str:
    if placar[0] > placar[1]:
        return "home"
    if placar[1] > placar[0]:
        return "away"
    return "draw"


def _periodo_da_chave(market_key: str) -> tuple[str, str]:
    """"totals_1t:2.5" -> (base "totals", período "1ST").

    O sufixo `_1t` mora no PREFIXO da chave (antes do ":"), nunca no fim da
    chave inteira — "totals_1t:2.5" não termina em "_1t".
    """
    prefixo = market_key.split(":")[0]
    if prefixo.endswith("_1t"):
        return prefixo[:-3], PERIODO_1T
    return prefixo, PERIODO_JOGO


def _linha_da_chave(market_key: str) -> float | None:
    m = re.search(r":(-?\d+(?:\.\d+)?)$", market_key)
    return float(m.group(1)) if m else None


# ------------------------------------------------------------------
# avaliação de uma perna
# ------------------------------------------------------------------

# Terceiro estado, além de True/False/None: a linha bateu exatamente no número
# e a casa devolve a stake. É diferente de "não sei" — aqui se sabe o que
# aconteceu, e chamar de "não apurada" seria impreciso no relatório.
PUSH = "push"


def _sobre_linha(total: Intervalo, selecao: str | None,
                 linha: float) -> bool | None | str:
    if selecao not in ("over", "under"):
        return None
    # Linha inteira ("Menos de 1") com o total exatamente em cima dela: push.
    # Linha .5 nunca cai aqui, e é a esmagadora maioria.
    if total.exato and total.minimo == linha:
        return PUSH
    return total.acima_de(linha) if selecao == "over" else total.abaixo_de(linha)


def _total_gols(placar: tuple[int, int]) -> Intervalo:
    return Intervalo.exatamente(placar[0] + placar[1])


def _avaliar_perna(leg: Leg, partida: ResultadoPartida
                   ) -> tuple[bool | None, str | None]:
    """(veredito, motivo). Veredito `None` = não deu pra resolver.

    Devolver `None` é sempre aceitável; devolver `False` sem certeza não é
    (viraria red inventado). Toda saída `False` aqui vem de um fato observado.
    """
    # Pernas que o market_parser não cobre (cartões, "marcar em ambos os
    # tempos") ainda são apostas reais e alertadas — o consenso entre casas as
    # precifica. Elas têm parser de TEXTO próprio, só aqui.
    if not leg.suportado or not leg.market_key:
        return _avaliar_perna_sem_cobertura(leg, partida)

    base, periodo = _periodo_da_chave(leg.market_key)
    linha = _linha_da_chave(leg.market_key)
    placar = partida.placar(periodo)

    # --- famílias que são função do placar ---------------------------------
    if base in ("h2h", "totals", "btts", "exact_goals", "team_total",
                "team_exact_goals", "double_chance", "ht_ft", "btts_vencedor"):
        if placar is None:
            return None, f"placar do período {periodo} não publicado"

    if base == "h2h":
        if leg.selecao in ("home", "away", "draw"):
            alvo = leg.selecao
        elif leg.time_nome:
            alvo = partida.lado_do_time(leg.time_nome)
        else:
            alvo = None
        if alvo is None:
            return None, "h2h: lado não resolvido"
        return _vencedor(placar) == alvo, None

    if base == "totals":
        if linha is None:
            return None, "total sem linha"
        return _sobre_linha(_total_gols(placar), leg.selecao, linha), None

    if base == "team_total":
        if linha is None or not leg.time_nome:
            return None, "total de equipe sem linha/time"
        lado = partida.lado_do_time(leg.time_nome)
        if lado is None:
            return None, "total de equipe: time não resolvido"
        gols = placar[0] if lado == "home" else placar[1]
        return _sobre_linha(Intervalo.exatamente(gols), leg.selecao, linha), None

    if base == "btts":
        marcaram = placar[0] > 0 and placar[1] > 0
        return (marcaram if leg.selecao != "No" else not marcaram), None

    if base == "exact_goals":
        if leg.selecao is None or not str(leg.selecao).isdigit():
            return None, "gols exatos sem valor"
        return (placar[0] + placar[1]) == int(leg.selecao), None

    if base == "team_exact_goals":
        if leg.selecao is None or not str(leg.selecao).isdigit() or not leg.time_nome:
            return None, "gols exatos de equipe sem valor/time"
        lado = partida.lado_do_time(leg.time_nome)
        if lado is None:
            return None, "gols exatos de equipe: time não resolvido"
        gols = placar[0] if lado == "home" else placar[1]
        return gols == int(leg.selecao), None

    if base == "double_chance":
        cobertos = _lados_da_chance_dupla(leg.selecao, partida)
        if not cobertos:
            return None, "chance dupla: lados não resolvidos"
        return _vencedor(placar) in cobertos, None

    if base == "ht_ft":
        ht = partida.placar(PERIODO_1T)
        if ht is None:
            return None, "intervalo/final: placar do 1º tempo não publicado"
        lados = _lados_do_ht_ft(leg.selecao, partida)
        if lados is None:
            return None, "intervalo/final: lados não resolvidos"
        return (_vencedor(ht) == lados[0] and _vencedor(placar) == lados[1]), None

    if base == "btts_vencedor":
        combo = _btts_vencedor(leg.selecao, partida)
        if combo is None:
            return None, "1x2+ambas marcam: seleção não resolvida"
        quer_btts, lado = combo
        marcaram = placar[0] > 0 and placar[1] > 0
        if marcaram != quer_btts:
            return False, None
        return _vencedor(placar) == lado, None

    # --- famílias que dependem de estatística -------------------------------
    if base == "corners":
        par = partida.estatistica("escanteios", periodo)
        if par is None:
            return None, "escanteios não reportados"
        if linha is None:
            return None, "escanteios sem linha"
        return _sobre_linha(par[0] + par[1], leg.selecao, linha), None

    if base == "corners_h2h":
        par = partida.estatistica("escanteios", periodo)
        if par is None:
            return None, "escanteios não reportados"
        alvo = (leg.selecao if leg.selecao in ("home", "away", "draw")
                else partida.lado_do_time(leg.time_nome or ""))
        if alvo is None:
            return None, "1x2 de escanteios: lado não resolvido"
        return _compara_lados(par, alvo), None

    return None, f"fora do escopo da liquidação (market_key={leg.market_key!r})"


def _compara_lados(par: tuple[Intervalo, Intervalo], alvo: str) -> bool | None:
    """Quem teve mais — com empate como desfecho possível."""
    casa, fora = par
    if alvo == "home":
        return casa.maior_que(fora)
    if alvo == "away":
        return fora.maior_que(casa)
    if alvo == "draw":
        if casa.exato and fora.exato:
            return casa.minimo == fora.minimo
        return None
    return None


def _lados_da_chance_dupla(selecao: str | None,
                           partida: ResultadoPartida) -> set[str]:
    """`'{time:X} Or Draw||Draw Or {time:X}'` -> {"home", "draw"}.

    O `||` separa grafias alternativas do MESMO mercado (a Pinnacle escreve nas
    duas ordens); basta ler a primeira.
    """
    if not selecao:
        return set()
    alternativa = selecao.split("||")[0]
    lados = set()
    for parte in re.split(r"\bOr\b", alternativa, flags=re.IGNORECASE):
        lado = resolver_lado_1x2(parte.strip(), partida)
        if lado is None:
            return set()
        lados.add(lado)
    return lados


def _lados_do_ht_ft(selecao: str | None,
                    partida: ResultadoPartida) -> tuple[str, str] | None:
    """`'{time:X} - {time:Y}'` -> (lado no intervalo, lado no final)."""
    if not selecao:
        return None
    partes = selecao.split("||")[0].split(" - ")
    if len(partes) != 2:
        return None
    ht = resolver_lado_1x2(partes[0].strip(), partida)
    ft = resolver_lado_1x2(partes[1].strip(), partida)
    if ht is None or ft is None:
        return None
    return ht, ft


def _btts_vencedor(selecao: str | None,
                   partida: ResultadoPartida) -> tuple[bool, str] | None:
    """`'Yes & {time:Fluminense}'` -> (True, "home")."""
    if not selecao:
        return None
    quer_btts: bool | None = None
    lado: str | None = None
    for parte in selecao.split("||")[0].split("&"):
        parte = parte.strip()
        if re.fullmatch(r"yes|sim", parte, re.IGNORECASE):
            quer_btts = True
        elif re.fullmatch(r"no|n[aã]o", parte, re.IGNORECASE):
            quer_btts = False
        else:
            lado = resolver_lado_1x2(parte, partida)
    if quer_btts is None or lado is None:
        return None
    return quer_btts, lado


# ------------------------------------------------------------------
# pernas que o market_parser recusa, mas que a liquidação resolve
# ------------------------------------------------------------------
#
# ⚠️ Estes parsers existem SÓ aqui e SÓ para liquidar. Dar `market_key` a eles
# no `market_parser` faria `fair_odds` precificá-los contra a referência errada
# — o bug de mercado restrito × referência ampla. Ver o topo do módulo.

_RE_CARTOES_TOTAL = re.compile(
    r"^(?:total\s+de\s+)?cart(?:õ|o)es\s*:?\s*"
    r"(?P<lado>mais|menos|over|under)\s+de\s+(?P<linha>\d+(?:[.,]\d+)?)\s*$",
    re.IGNORECASE)

_RE_CARTOES_1X2 = re.compile(
    r"^cart(?:õ|o)es\s+(?:1x2|resultado\s+final)\s*:\s*(?P<time>.+?)\s*$",
    re.IGNORECASE)

_RE_MARCA_AMBOS_TEMPOS = re.compile(
    r"^(?P<time>.+?)\s+(?:para\s+)?marcar\s+em\s+ambos\s+os\s+tempos\s*:\s*"
    r"(?P<sim>sim|n[aã]o)\s*$", re.IGNORECASE)

# ⚠️ Mercado DIFERENTE do de cima, apesar da redação parecida: aqui é vencer um
# tempo, não marcar nos dois. `stats_check._MARCA_AMBOS_TEMPOS` junta os dois
# numa regex só e aplica o predicado de "marcou nos dois" — bug registrado
# separadamente. Aqui nascem separados.
#
# "um|uma" não é tolerância gratuita: a casa escreve "um" na primeira perna do
# combo e "uma" na segunda ("Vitória para vencer um dos tempos: Sim + Athletico
# PR para vencer uma dos tempos: Sim"). Medido: 6 das 13 ofertas desta família.
_RE_VENCER_UM_TEMPO = re.compile(
    r"^(?P<time>.+?)\s+(?:para\s+)?(?:vencer|ganhar)\s+um[a]?\s+dos\s+tempos\s*:\s*"
    r"(?P<sim>sim|n[aã]o)\s*$", re.IGNORECASE)

# "Ambos os tempos mais de 0.5: Sim" — saiu gol nos DOIS tempos, por qualquer
# equipe. Não confundir com o de cima, que é de um time específico.
_RE_AMBOS_TEMPOS_LINHA = re.compile(
    r"^ambos\s+os\s+tempos\s+(?P<lado>mais|menos)\s+de\s+"
    r"(?P<linha>\d+(?:[.,]\d+)?)\s*:\s*(?P<sim>sim|n[aã]o)\s*$", re.IGNORECASE)

# 2º tempo: o `market_parser` recusa a família inteira (a Pinnacle não publica),
# mas para LIQUIDAR basta subtrair — o placar do 2º tempo é final menos
# intervalo. Cobrir aqui não afeta precificação nenhuma.
_RE_2T_BTTS = re.compile(
    r"^2[ºo°]?\s*tempo\s*-\s*ambas\s+(?:as\s+)?equipes\s+marcam\s*:\s*"
    r"(?P<sim>sim|n[aã]o)\s*$", re.IGNORECASE)


def _avaliar_perna_sem_cobertura(leg: Leg, partida: ResultadoPartida
                                 ) -> tuple[bool | None, str | None]:
    texto = leg.texto.strip()

    m = _RE_CARTOES_TOTAL.match(texto)
    if m:
        par = partida.cartoes(PERIODO_JOGO)
        if par is None:
            return None, "cartões não reportados"
        linha = float(m.group("linha").replace(",", "."))
        selecao = "over" if m.group("lado").lower() in ("mais", "over") else "under"
        return _sobre_linha(par[0] + par[1], selecao, linha), None

    m = _RE_CARTOES_1X2.match(texto)
    if m:
        par = partida.cartoes(PERIODO_JOGO)
        if par is None:
            return None, "cartões não reportados"
        lado = partida.lado_do_time(m.group("time"))
        if lado is None:
            return None, "1x2 de cartões: time não resolvido"
        return _compara_lados(par, lado), None

    m = _RE_MARCA_AMBOS_TEMPOS.match(texto)
    if m:
        return _tempos(partida, m, marcar_nos_dois=True)

    m = _RE_VENCER_UM_TEMPO.match(texto)
    if m:
        return _tempos(partida, m, marcar_nos_dois=False)

    m = _RE_AMBOS_TEMPOS_LINHA.match(texto)
    if m:
        ht, ft = partida.placar(PERIODO_1T), partida.placar(PERIODO_JOGO)
        if ht is None or ft is None:
            return None, "placar por tempo não publicado"
        linha = float(m.group("linha").replace(",", "."))
        gols_1t = ht[0] + ht[1]
        gols_2t = (ft[0] - ht[0]) + (ft[1] - ht[1])
        if m.group("lado").lower() == "mais":
            acontece = gols_1t > linha and gols_2t > linha
        else:
            acontece = gols_1t < linha and gols_2t < linha
        return acontece == (m.group("sim").lower() == "sim"), None

    m = _RE_2T_BTTS.match(texto)
    if m:
        ht, ft = partida.placar(PERIODO_1T), partida.placar(PERIODO_JOGO)
        if ht is None or ft is None:
            return None, "placar por tempo não publicado"
        marcaram = (ft[0] - ht[0]) > 0 and (ft[1] - ht[1]) > 0
        return marcaram == (m.group("sim").lower() == "sim"), None

    return None, leg.motivo or "perna sem mercado reconhecido"


def _tempos(partida: ResultadoPartida, m: re.Match, *,
            marcar_nos_dois: bool) -> tuple[bool | None, str | None]:
    ht = partida.placar(PERIODO_1T)
    ft = partida.placar(PERIODO_JOGO)
    if ht is None or ft is None:
        return None, "placar por tempo não publicado"
    lado = partida.lado_do_time(m.group("time"))
    if lado is None:
        return None, "time não resolvido"
    i = 0 if lado == "home" else 1
    j = 1 - i
    pro_1t, contra_1t = ht[i], ht[j]
    pro_2t, contra_2t = ft[i] - ht[i], ft[j] - ht[j]

    if marcar_nos_dois:
        acontece = pro_1t > 0 and pro_2t > 0
    else:
        acontece = (pro_1t > contra_1t) or (pro_2t > contra_2t)

    quer = m.group("sim").lower() == "sim"
    return acontece == quer, None


# ------------------------------------------------------------------
# veredito da aposta inteira
# ------------------------------------------------------------------

GREEN, RED, VOID, DESCONHECIDO = "green", "red", "void", "desconhecido"


@dataclass(frozen=True)
class Liquidacao:
    resultado: str
    motivo: str | None = None
    pernas: tuple[tuple[str, str], ...] = ()

    @property
    def resolvida(self) -> bool:
        return self.resultado in (GREEN, RED)


def liquidar_aposta(mercado: str, partida: ResultadoPartida, *,
                    red_com_perna_indefinida: bool = True) -> Liquidacao:
    """Veredito da oferta inteira.

    Usa `parse_mercado`, o MESMO parser que precificou a aposta. Um segundo
    parser leria o rótulo de outro jeito e o relatório passaria a falar de uma
    aposta que nunca foi feita.

    Ordem das regras (AND de Kleene):
      1. partida não encerrada  -> void  (adiada/cancelada nunca é derrota)
      2. alguma perna False     -> red
      3. alguma perna None      -> desconhecido
      4. todas True             -> green

    `red_com_perna_indefinida=False` inverte 2 e 3, dando a semântica literal
    "uma perna indefinida derruba tudo pra desconhecido". O padrão é `True`
    porque combo quase sempre casa perna de gol resolvível com perna exótica, e
    `red` continua exigindo uma perna **confirmada** perdida — nunca inferida.
    """
    if not partida.encerrada:
        return Liquidacao(VOID, f"partida {partida.status}")

    legs = parse_mercado(mercado or "")
    if not legs:
        return Liquidacao(DESCONHECIDO, "mercado não parseou")

    vereditos: list[object] = []
    detalhe: list[tuple[str, str]] = []
    primeiro_motivo: str | None = None

    for leg in legs:
        veredito, motivo = _avaliar_perna(leg, partida)
        vereditos.append(veredito)
        detalhe.append((leg.texto, {True: "green", False: "red",
                                    PUSH: "push"}.get(veredito, "?")))
        if veredito is None and primeiro_motivo is None:
            primeiro_motivo = f"{leg.texto} — {motivo or 'não resolvida'}"

    tem_falsa = any(v is False for v in vereditos)
    tem_indefinida = any(v is None for v in vereditos)
    tem_push = any(v == PUSH for v in vereditos)
    pernas = tuple(detalhe)

    # Perna perdida derruba a aposta mesmo com push em outra: a casa anula a
    # perna do push e liquida o resto, e o resto já está perdido.
    if tem_falsa and (red_com_perna_indefinida or not tem_indefinida):
        return Liquidacao(RED, None, pernas)
    if tem_indefinida:
        return Liquidacao(DESCONHECIDO, primeiro_motivo, pernas)
    if tem_falsa:
        return Liquidacao(RED, None, pernas)
    # Só sobrou verdadeiro e push. Com push a casa devolve (parte da) stake e
    # a odd efetiva deixa de ser a alertada — sem ela não dá pra dizer o
    # ganho, então `void` (fora do saldo) é a resposta honesta.
    if tem_push:
        return Liquidacao(VOID, "linha exata (push)", pernas)

    return Liquidacao(GREEN, None, pernas)


# ------------------------------------------------------------------
# P&L
# ------------------------------------------------------------------

def unidades_do_resultado(resultado: str, odd: float | None,
                          stake: float | None,
                          apostavel: bool | None = None) -> float | None:
    """Unidades ganhas/perdidas. `None` = não entra no saldo.

    Fora do saldo, e de propósito:
      - veredito não resolvido (desconhecido/void) — não houve ganho nem perda;
      - alerta sem stake gravada (anterior à migração) — inventar a stake
        contradiria "o relatório é registro do que foi recomendado";
      - stake abaixo do mínimo, que o alerta marcou como "não vale a pena":
        creditar essas fabricaria histórico de apostas que o bot desaconselhou.
    """
    if resultado not in (GREEN, RED):
        return None
    if stake is None or odd is None:
        return None
    if apostavel is False:
        return 0.0
    return round(stake * (odd - 1.0), 4) if resultado == GREEN else round(-stake, 4)


# ------------------------------------------------------------------
# identidade da partida e busca no SofaScore
# ------------------------------------------------------------------

def chave_partida(evento: str, inicio_evento: str | datetime | None) -> str | None:
    """Identidade normalizada da partida, compartilhada entre casas.

    As casas Altenar publicam o mesmo jogo com grafias diferentes ("Salzburg -
    Pafos FC" e "FC Salzburgo - Pafos FC"). Chavear pelo texto cru faria a
    mesma busca fuzzy uma vez por casa — e um backoff por grafia.
    """
    times = split_times(evento or "")
    if not times:
        return None
    casa, fora = (normalizar(t) for t in times)
    if not casa or not fora:
        return None
    dia = ""
    quando = _como_datetime(inicio_evento)
    if quando is not None:
        dia = quando.astimezone(timezone.utc).date().isoformat()
    # Ordenado: qual lado é mandante pode divergir entre casa e SofaScore.
    a, b = sorted((casa, fora))
    return f"{a}|{b}|{dia}"


def _como_datetime(valor: str | datetime | None) -> datetime | None:
    if valor is None or isinstance(valor, datetime):
        return valor
    try:
        return datetime.fromisoformat(str(valor))
    except ValueError:
        return None


def fim_estimado(inicio_evento: str | datetime | None) -> str | None:
    """Kickoff + `DURACAO_PARTIDA_MIN`, em ISO UTC — a janela do relatório."""
    quando = _como_datetime(inicio_evento)
    if quando is None:
        return None
    if quando.tzinfo is None:
        quando = quando.replace(tzinfo=timezone.utc)
    return (quando.astimezone(timezone.utc)
            + timedelta(minutes=DURACAO_PARTIDA_MIN)).isoformat(timespec="seconds")


# Quanto o kickoff do candidato pode divergir do nosso pra contar como "o mesmo
# horário". Tem que ser apertado: é a folga que sustenta o desempate abaixo.
TOLERANCIA_KICKOFF_MIN = 5


def _desempate_por_kickoff(evento: str, inicio: datetime | None,
                           candidatos: list[Matchup]) -> Matchup | None:
    """Fallback pro caso de apelido de clube, usando o horário exato.

    O `encontrar_evento` exige que os DOIS lados passem de `MATCH_MIN_SCORE`, e
    isso derruba apelido consagrado: medido em dado real, "Hearts" contra
    "Heart of Midlothian" tira 40, "FC Salzburgo" contra "Red Bull Salzburg"
    tira 61,5. Eram 4 de 26 apostas perdidas por isso.

    O que autoriza afrouxar aqui — e só aqui — é uma evidência que o matcher
    pré-jogo não tem: **um time joga no máximo uma partida por horário**. Se um
    dos lados casa com folga E o apito bate no minuto, a partida está
    determinada; o outro nome ser um apelido não muda isso.

    Continua sendo mais evidência, não menos:
      - os vetos de UF e de gênero seguem valendo (é o que separa Botafogo-SP
        de Botafogo-RJ e o time feminino do masculino);
      - se DOIS candidatos passarem, recusa — ambiguidade não vira palpite.
    """
    if inicio is None:
        return None
    nossos = split_times(evento or "")
    if not nossos:
        return None
    nosso_casa, nosso_fora = nossos

    aprovados: list[Matchup] = []
    for m in candidatos:
        if m.commence_time is None:
            continue
        delta = abs((m.commence_time - inicio).total_seconds()) / 60.0
        if delta > TOLERANCIA_KICKOFF_MIN:
            continue
        if genero_conflita(evento, m.league):
            continue
        # Direto e invertido: quem é mandante pode divergir entre a casa e o
        # SofaScore, e isso não deve reprovar a partida.
        for a, b in ((m.home_team, m.away_team), (m.away_team, m.home_team)):
            if uf_conflita(nosso_casa, a) or uf_conflita(nosso_fora, b):
                continue
            if max(_score_nome(nosso_casa, a), _score_nome(nosso_fora, b)) >= MATCH_MIN_SCORE:
                aprovados.append(m)
                break

    if len(aprovados) != 1:
        if len(aprovados) > 1:
            log.info("liquidação: %d candidatos no mesmo horário para %r — "
                     "ambíguo, não casa", len(aprovados), evento)
        return None
    return aprovados[0]


def encontrar_partida(cliente: SofaScoreClient, evento: str,
                      inicio_evento: str | datetime | None
                      ) -> tuple[int | None, float | None]:
    """Casa nosso evento com um jogo ENCERRADO do SofaScore.

    Primeiro tenta `matcher.encontrar_evento` inteiro — o mesmo do resto do
    projeto, com score mínimo, janela de data, veto de UF e veto de gênero.
    Só quando ele recusa é que entra o desempate por horário exato, que é
    estrito no tempo em troca de tolerante no nome (ver `_desempate_por_kickoff`).

    Match errado reporta P&L errado com confiança total, que é pior que não
    reportar nada — por isso nenhum dos dois caminhos chuta.
    """
    times = split_times(evento or "")
    if not times:
        return None, None
    casa, fora = times
    try:
        # TTL curto: um jogo recém-encerrado não aparece numa página de
        # "últimos jogos" cacheada horas atrás.
        candidatos = _matchups_candidatos(cliente, casa, fora, direcao="last",
                                          ttl_horas=0.25)
    except SofaScoreError as exc:
        log.warning("liquidação: busca de candidatos falhou para %r: %s", evento, exc)
        return None, None
    if not candidatos:
        return None, None

    quando = _como_datetime(inicio_evento)
    matchups = [c.matchup for c in candidatos.values()]
    match = encontrar_evento(evento, quando, matchups)
    if match is not None:
        return match.matchup.id, getattr(match, "score", None)

    alternativo = _desempate_por_kickoff(evento, quando, matchups)
    if alternativo is not None:
        log.info("liquidação: %r casou por horário exato com %s x %s",
                 evento, alternativo.home_team, alternativo.away_team)
        return alternativo.id, None
    return None, None


def carregar_resultado(cliente: SofaScoreClient,
                       sofascore_id: int) -> ResultadoPartida | None:
    """Busca placar + estatística de uma partida. `None` se não deu."""
    try:
        evento = cliente.evento(sofascore_id)
    except SofaScoreError as exc:
        log.warning("liquidação: /event/%s falhou: %s", sofascore_id, exc)
        return None
    if not evento:
        return None
    estatisticas = None
    try:
        estatisticas = cliente.estatisticas(sofascore_id)
    except SofaScoreError as exc:
        # Estatística é opcional: sem ela o placar ainda resolve gol, e as
        # pernas de escanteio/cartão saem como desconhecido — que é correto.
        log.info("liquidação: /statistics/%s indisponível: %s", sofascore_id, exc)
    return montar_resultado(evento, estatisticas)


def serializar_estatisticas(partida: ResultadoPartida) -> str:
    """Estatísticas -> JSON pra `partidas_sofascore.estatisticas`."""
    bruto = {
        periodo: {nome: [par[0].minimo, par[1].minimo]
                  for nome, par in stats.items()
                  if par[0].exato and par[1].exato}
        for periodo, stats in partida.stats.items()
    }
    return json.dumps(bruto, ensure_ascii=False)


def desserializar(linha: dict) -> ResultadoPartida:
    """Linha de `partidas_sofascore` -> `ResultadoPartida`.

    É o caminho que o relatório usa: reconstruir do banco em vez de pedir de
    novo à rede. Chave ausente no JSON continua ausente aqui — nunca vira zero.
    """
    gols: dict[str, tuple[int, int]] = {}
    if linha.get("gols_home") is not None and linha.get("gols_away") is not None:
        gols[PERIODO_JOGO] = (int(linha["gols_home"]), int(linha["gols_away"]))
    if linha.get("ht_home") is not None and linha.get("ht_away") is not None:
        gols[PERIODO_1T] = (int(linha["ht_home"]), int(linha["ht_away"]))
    stats: dict[str, dict[str, tuple[Intervalo, Intervalo]]] = {}
    try:
        bruto = json.loads(linha.get("estatisticas") or "{}")
    except ValueError:
        bruto = {}
    for periodo, itens in bruto.items():
        stats[periodo] = {nome: (Intervalo.exatamente(v[0]), Intervalo.exatamente(v[1]))
                          for nome, v in itens.items() if len(v) == 2}
    return ResultadoPartida(
        status=linha.get("status") or "", home_nome=linha.get("home_nome") or "",
        away_nome=linha.get("away_nome") or "", gols=gols, stats=stats)
