"""Sondagem read-only: quanto vale a pena atacar cada motivo de SEM_COBERTURA
que ainda não tem entrada em `_FAMILIA_POR_MOTIVO` — ou seja, cada motivo que
hoje faz `chave_consenso()` devolver `None` e a perna cair em "sem chave de
consenso" por CONSTRUÇÃO, não por falta de regex.

Não escreve nada no banco. Reaproveita os regexes de `SEM_COBERTURA` via
import (nunca reescrever à mão — duas cópias do "mesmo" regex divergem e
mentem uma pra outra). Roda contra o histórico já sentado em
`betano_superodds.db`: não precisa do bot rodando, responde em segundos.

O número que decide viabilidade não é a contagem bruta de linha no pool — é
quantos EVENTOS alcançam `CONSENSO_MIN_CASAS`/`_PROP` casas distintas. Um
motivo pode ter milhares de linhas no pool e zero eventos formando consenso
(cobertura espalhada demais entre poucas casas por jogo).

Uso: `python sondar_familias_consenso.py`
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict

from betano_superodds import config
from betano_superodds.value import config as vconfig
from betano_superodds.value.market_parser import SEM_COBERTURA, SEPARADOR_PERNAS, \
    _FAMILIA_POR_MOTIVO

# Motivo estruturalmente irrecuperável — o payload perde o `marketId` antes
# de chegar aqui (ver comentário em market_parser.py). Nunca vira candidato.
_IRRECUPERAVEL = "seleção sem mercado (bet-builder)"

_N_EXEMPLOS = 4


def _candidatos() -> list[tuple[re.Pattern, str]]:
    """Motivos de SEM_COBERTURA sem entrada em _FAMILIA_POR_MOTIVO — os que
    fazem chave_consenso() devolver None hoje, por falta de família."""
    vistos: set[str] = set()
    saida: list[tuple[re.Pattern, str]] = []
    for padrao, motivo in SEM_COBERTURA:
        if motivo in _FAMILIA_POR_MOTIVO or motivo == _IRRECUPERAVEL:
            continue
        if motivo in vistos:
            continue   # mesmo motivo, regex extra (ex. "vencer de zero")
        vistos.add(motivo)
        saida.append((re.compile(padrao, re.I), motivo))
    return saida


def _regex_do_motivo(motivo: str) -> re.Pattern:
    """Uma perna pode casar em QUALQUER regex do motivo (há motivos com mais
    de uma linha em SEM_COBERTURA, ex. "probabilidade conjunta entre
    tempos") — junta todos num OR pra sondagem, sem duplicar a lógica de
    `_sem_cobertura` (que já para no primeiro match, ordem importa lá; aqui
    só queremos saber "esse texto bateria em alguma variante deste motivo")."""
    padroes = [p for p, m in SEM_COBERTURA if m == motivo]
    return re.compile("|".join(f"(?:{p})" for p in padroes), re.I)


def sondar_ofertas(conn: sqlite3.Connection,
                    candidatos: list[tuple[re.Pattern, str]]) -> dict[str, dict]:
    """Lado da OFERTA: quantas pernas (não ofertas) casam em cada motivo."""
    rows = conn.execute("SELECT mercado FROM offers").fetchall()
    saida: dict[str, dict] = {m: {"pernas": 0, "exemplos": []} for _, m in candidatos}
    for (mercado,) in rows:
        if not mercado:
            continue
        for perna in mercado.split(SEPARADOR_PERNAS):
            for _, motivo in candidatos:
                regex = _regex_do_motivo(motivo)
                if regex.search(perna):
                    d = saida[motivo]
                    d["pernas"] += 1
                    if len(d["exemplos"]) < _N_EXEMPLOS:
                        d["exemplos"].append(perna.strip())
    return saida


def sondar_pool(conn: sqlite3.Connection,
                 candidatos: list[tuple[re.Pattern, str]]) -> dict[str, dict]:
    """Lado do POOL (`mercados_casa`): linhas, eventos distintos, e quantos
    eventos alcançam o mínimo de casas pra formar consenso de verdade."""
    rows = conn.execute(
        "SELECT casa, evento_id, market_nome, selecao FROM mercados_casa"
    ).fetchall()

    saida: dict[str, dict] = {
        m: {"linhas": 0, "casas_por_evento": defaultdict(set), "exemplos": []}
        for _, m in candidatos
    }
    for casa, evento_id, market_nome, selecao in rows:
        # Mesmo formato que `consenso._chave_da_linha` monta pra casar —
        # a linha às vezes só revela o motivo real com a seleção junto.
        texto = f"{market_nome or ''} {selecao or ''}".strip()
        if not texto:
            continue
        for _, motivo in candidatos:
            regex = _regex_do_motivo(motivo)
            if regex.search(texto):
                d = saida[motivo]
                d["linhas"] += 1
                d["casas_por_evento"][evento_id].add(casa)
                if len(d["exemplos"]) < _N_EXEMPLOS:
                    d["exemplos"].append((market_nome, selecao))
    return saida


def main() -> None:
    conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
    candidatos = _candidatos()

    print(f"banco: {config.DB_PATH}")
    print(f"CONSENSO_MIN_CASAS={vconfig.CONSENSO_MIN_CASAS} "
          f"CONSENSO_MIN_CASAS_PROP={vconfig.CONSENSO_MIN_CASAS_PROP}")
    print(f"{len(candidatos)} motivo(s) candidato(s) (sem família hoje)\n")

    ofertas = sondar_ofertas(conn, candidatos)
    pool = sondar_pool(conn, candidatos)

    linha_fmt = "{:<55} {:>8} {:>10} {:>9} {:>7} {:>7}"
    print(linha_fmt.format("motivo", "pernas", "linhas_pool", "eventos",
                            f">={vconfig.CONSENSO_MIN_CASAS}c",
                            f">={vconfig.CONSENSO_MIN_CASAS_PROP}c"))
    for _, motivo in candidatos:
        n_pernas = ofertas[motivo]["pernas"]
        p = pool[motivo]
        n_eventos = len(p["casas_por_evento"])
        n_min = sum(1 for casas in p["casas_por_evento"].values()
                    if len(casas) >= vconfig.CONSENSO_MIN_CASAS)
        n_prop = sum(1 for casas in p["casas_por_evento"].values()
                     if len(casas) >= vconfig.CONSENSO_MIN_CASAS_PROP)
        print(linha_fmt.format(motivo[:55], n_pernas, p["linhas"], n_eventos,
                                n_min, n_prop))

    print("\n--- exemplos crus (até %d por motivo) ---" % _N_EXEMPLOS)
    for _, motivo in candidatos:
        print(f"\n{motivo}")
        print("  oferta:", ofertas[motivo]["exemplos"] or "(nenhum)")
        print("  pool:  ", pool[motivo]["exemplos"] or "(nenhum)")

    conn.close()


if __name__ == "__main__":
    main()
