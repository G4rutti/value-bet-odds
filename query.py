"""Consulta rápida ao banco de ofertas.

    python query.py                     # ofertas ativas, maior ganho primeiro
    python query.py --casa EstrelaBet   # só de uma casa (aceita pedaço do nome)
    python query.py --casas             # quantas ofertas cada casa tem
    python query.py --fonte mr12        # só as SuperOdds de Resultado Final
    python query.py --min-ganho 15      # só boosts acima de 15%
    python query.py --historico         # últimas mudanças registradas
    python query.py --runs              # log dos ciclos de scraping
    python query.py --curadoria         # veredito da curadoria x resultado real
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from betano_superodds import config


def setup_stdout() -> None:
    # O console do Windows costuma vir em cp1252 e engasga com acento.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def show_casas(conn: sqlite3.Connection, incluir_inativas: bool) -> None:
    """Quantas ofertas cada casa tem — com 10 casas isso vira a visão principal."""
    onde = "" if incluir_inativas else " WHERE active = 1"
    rows = list(conn.execute(f"""
        SELECT casa,
               COUNT(*)                                   AS total,
               SUM(active)                                AS ativas,
               MAX(odd_boost / NULLIF(odd_original, 0))   AS melhor
          FROM offers{onde}
      GROUP BY casa
      ORDER BY ativas DESC, total DESC
    """))
    if not rows:
        print("nenhuma oferta no banco")
        return

    print(f"{'CASA':<18} {'ATIVAS':>7} {'TOTAL':>7} {'MELHOR GANHO':>13}")
    print("-" * 50)
    for r in rows:
        melhor = f"+{(r['melhor'] - 1) * 100:.1f}%" if r["melhor"] else "?"
        print(f"{r['casa']:<18} {r['ativas'] or 0:>7} {r['total']:>7} {melhor:>13}")
    print("-" * 50)
    print(f"{'':<18} {sum(r['ativas'] or 0 for r in rows):>7} "
          f"{sum(r['total'] for r in rows):>7}")


def show_offers(conn: sqlite3.Connection, fonte: str | None, min_ganho: float,
                incluir_inativas: bool, casa: str | None = None) -> None:
    where = [] if incluir_inativas else ["active = 1"]
    params: list[object] = []
    if fonte:
        where.append("fonte = ?")
        params.append(fonte)
    if casa:
        # `LIKE` para aceitar "estrela" em vez de exigir "EstrelaBet" exato.
        where.append("casa LIKE ?")
        params.append(f"%{casa}%")

    sql = "SELECT * FROM offers"
    if where:
        sql += " WHERE " + " AND ".join(where)

    rows = []
    for r in conn.execute(sql, params):
        ganho = (r["odd_boost"] / r["odd_original"] - 1) * 100 if r["odd_original"] else None
        if ganho is not None and ganho < min_ganho:
            continue
        rows.append((ganho if ganho is not None else -1, r))

    rows.sort(key=lambda x: x[0], reverse=True)

    if not rows:
        print("nenhuma oferta encontrada com esse filtro")
        return

    print(f"{'GANHO':>7}  {'ODDS':>15}  {'CASA':<15} EVENTO / MERCADO")
    print("-" * 100)
    for ganho, r in rows:
        odds = f"{r['odd_original'] or 0:.2f} -> {r['odd_boost']:.2f}"
        g = f"+{ganho:.1f}%" if ganho >= 0 else "  ?"
        flag = "" if r["active"] else "  [inativa]"
        print(f"{g:>7}  {odds:>15}  {r['casa'][:15]:<15} {r['evento']}{flag}")
        print(f"{'':>7}  {'':>15}  {'':<15} {r['mercado']}")
        if r["valido_ate"]:
            print(f"{'':>7}  {'':>15}  {'':<15} até {r['valido_ate']}  {r['url']}")
        print()

    print(f"total: {len(rows)} oferta(s)")


def show_history(conn: sqlite3.Connection, limit: int) -> None:
    sql = """
        SELECT observado_em, evento_tipo, evento, mercado, odd_original, odd_boost
        FROM offer_history ORDER BY id DESC LIMIT ?
    """
    for r in conn.execute(sql, (limit,)):
        odds = f"{r['odd_original'] or 0:.2f} -> {r['odd_boost'] or 0:.2f}"
        print(f"[{r['observado_em'][11:19]}] {r['evento_tipo'].upper():9} {odds:>16}  "
              f"{r['evento']} — {r['mercado'][:60]}")


def show_runs(conn: sqlite3.Connection, limit: int) -> None:
    print(f"{'HORA':<10} {'OFERTAS':>8} {'NOVAS':>6} {'ALT':>5} {'EXP':>5}  ERRO")
    print("-" * 70)
    rows = list(conn.execute(
        "SELECT * FROM scrape_runs ORDER BY id DESC LIMIT ?", (limit,)))
    for r in reversed(rows):
        print(f"{r['executado_em'][11:19]:<10} {r['ofertas']:>8} {r['novas']:>6} "
              f"{r['alteradas']:>5} {r['expiradas']:>5}  {r['erro'] or ''}")


def show_curadoria(conn: sqlite3.Connection, limit: int) -> None:
    """Compara o veredito da curadoria estruturada (`value/curadoria.py`)
    contra o resultado REAL da liquidação — é o que decide quando promover
    `CURADORIA_ENFORCE` de sombra pra valendo (ver `value/config.py`).

    Junta por `offer_id`: uma linha por oferta que TEM veredito registrado
    (gravado mesmo em modo sombra, mesmo pra oferta que não virou alerta —
    ver `alerts.py`) E já foi liquidada green/red. `void`/`desconhecido`
    ficam de fora: não são sinal de acerto nem de erro do veredito.
    """
    rows = list(conn.execute("""
        SELECT v.decisao, v.motivo, v.modo, v.confianca_original,
               l.resultado, l.fim_partida, o.evento, o.mercado
          FROM veredito_curadoria v
          JOIN liquidacoes l ON l.offer_id = v.offer_id
          LEFT JOIN offers o ON o.offer_id = v.offer_id
         WHERE l.resultado IN ('green', 'red')
         ORDER BY l.fim_partida DESC
    """))
    if not rows:
        print("nenhum veredito de curadoria liquidado ainda — precisa de "
              "CURADORIA_ATIVA=1 rodando por um tempo e jogos já encerrados")
        return

    por_decisao: dict[str, dict[str, int]] = {}
    for r in rows:
        d = por_decisao.setdefault(r["decisao"], {"green": 0, "red": 0})
        d[r["resultado"]] = d.get(r["resultado"], 0) + 1

    print(f"{'DECISÃO':<10} {'N':>5} {'GREEN':>6} {'RED':>5} {'TAXA RED':>9}")
    print("-" * 45)
    for decisao in ("aprovado", "degrau", "vetado"):
        d = por_decisao.get(decisao)
        if not d:
            continue
        n = d["green"] + d["red"]
        taxa = d["red"] / n * 100 if n else 0.0
        print(f"{decisao:<10} {n:>5} {d['green']:>6} {d['red']:>5} {taxa:>8.1f}%")

    aprovado = por_decisao.get("aprovado", {"green": 0, "red": 0})
    vetado = por_decisao.get("vetado", {"green": 0, "red": 0})
    n_aprovado = aprovado["green"] + aprovado["red"]
    n_vetado = vetado["green"] + vetado["red"]
    print()
    if n_aprovado and n_vetado:
        taxa_aprovado = aprovado["red"] / n_aprovado
        taxa_vetado = vetado["red"] / n_vetado
        print(f"red em 'vetado' ({taxa_vetado:.0%}, n={n_vetado}) vs "
              f"'aprovado' ({taxa_aprovado:.0%}, n={n_aprovado})")
        if n_vetado < 30 or n_aprovado < 30:
            print("amostra pequena — critério proposto no plano é 30+ "
                 "liquidados dos dois lados antes de considerar "
                 "CURADORIA_ENFORCE=1 (ver value/config.py)")
    else:
        print("ainda sem amostra suficiente pra comparar 'vetado' vs 'aprovado'")

    print(f"\núltimos {min(limit, len(rows))} vereditos liquidados:")
    print("-" * 100)
    for r in rows[:limit]:
        print(f"[{r['resultado'].upper():5}] {r['decisao']:<9} modo={r['modo']:<7} "
              f"{(r['evento'] or '?')[:40]:<40} {r['motivo'] or ''}")


def main() -> int:
    p = argparse.ArgumentParser(description="Consulta o banco de Super Odds")
    # Sem `choices`: com 10 casas na Altenar, a fonte é `<slug>_1x2`/`<slug>_boost`,
    # então uma lista fixa aqui envelheceria a cada casa nova.
    p.add_argument("--fonte", help="filtra por fonte (mr12, smartpick, esportiva_boost, ...)")
    p.add_argument("--casa", help="filtra por casa; aceita pedaço do nome (ex: estrela)")
    p.add_argument("--casas", action="store_true", help="resumo por casa")
    p.add_argument("--min-ganho", type=float, default=0.0, help="ganho mínimo em %%")
    p.add_argument("--todas", action="store_true", help="inclui ofertas já expiradas")
    p.add_argument("--historico", action="store_true", help="mostra o histórico de mudanças")
    p.add_argument("--runs", action="store_true", help="mostra o log de ciclos")
    p.add_argument("--curadoria", action="store_true",
                   help="veredito da curadoria (value/curadoria.py) x resultado real")
    p.add_argument("--limit", type=int, default=30, help="linhas em histórico/runs")
    args = p.parse_args()

    setup_stdout()

    if not config.DB_PATH.exists():
        print(f"banco não encontrado em {config.DB_PATH}")
        print("rode primeiro: python -m betano_superodds.main --once")
        return 1

    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        if args.historico:
            show_history(conn, args.limit)
        elif args.runs:
            show_runs(conn, args.limit)
        elif args.curadoria:
            show_curadoria(conn, args.limit)
        elif args.casas:
            show_casas(conn, args.todas)
        else:
            show_offers(conn, args.fonte, args.min_ganho, args.todas, args.casa)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
