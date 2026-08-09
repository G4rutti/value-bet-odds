"""Avalia as super odds capturadas contra a odd justa da Pinnacle.

    python avaliar.py                    # ofertas ativas
    python avaliar.py --fonte mr12       # só as SuperOdds de Resultado Final
    python avaliar.py --todas            # inclui as já expiradas
    python avaliar.py --limit 20
    python avaliar.py --calibrar-modelo  # erro do modelo vs. odd real
    python avaliar.py --trace            # chave + label da Pinnacle por perna
"""

from __future__ import annotations

import argparse
import logging
import os
import sqlite3
import sys
from collections import Counter

from betano_superodds import config as scraper_config
from betano_superodds.storage import Storage
from betano_superodds.value.pipeline import avaliar_ofertas, formatar


def setup(verbose: bool) -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)


def carregar(fonte: str | None, todas: bool, limit: int) -> list[dict]:
    if not scraper_config.DB_PATH.exists():
        print(f"banco não encontrado em {scraper_config.DB_PATH}")
        print("rode primeiro: python -m betano_superodds.main --once")
        return []

    conn = sqlite3.connect(scraper_config.DB_PATH)
    conn.row_factory = sqlite3.Row
    where, params = [], []
    if not todas:
        where.append("active = 1")
    if fonte:
        where.append("fonte = ?")
        params.append(fonte)
    sql = "SELECT * FROM offers"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY odd_boost DESC LIMIT ?"
    params.append(limit)
    try:
        return [dict(r) for r in conn.execute(sql, params)]
    finally:
        conn.close()


def calibrar_modelo(jogos_alvo: int) -> int:
    """Compara a odd DERIVADA com a odd REAL onde as duas existem.

    É o único teste honesto do modelo de placar, e é de graça: em ~1 de cada
    12 jogos a Pinnacle publica placar exato / HT/FT / gols exatos, então dá
    pra medir o erro do modelo contra o preço de verdade, sem esperar
    resultado de jogo nenhum.

    O número que sai daqui é o que decide `EDGE_MIN_MODELO` — um threshold
    abaixo do erro típico do modelo transforma ruído de ajuste em "value" — e
    se `MODELO_ALERTA_ATIVO` pode ou não ser ligado.
    """
    import statistics

    from betano_superodds.value.fair_odds import probabilidade_derivada, remover_vig
    from betano_superodds.value.modelo_gols import modelo_do_jogo
    from betano_superodds.value.pinnacle import PinnacleScraper

    erros: dict[str, list[float]] = {}
    residuos: list[float] = []
    com_especial = 0

    with PinnacleScraper() as p:
        jogos = [m for m in p.jogos_normalizados() if m.sport == "soccer"]
        print(f"{len(jogos)} jogos de futebol; procurando os que publicam especiais\n")
        for matchup in jogos:
            if com_especial >= jogos_alvo:
                break
            try:
                matchup = p.carregar_mercados(matchup)
            except Exception:  # noqa: BLE001 — um jogo ruim não para a medição
                continue
            if "correct_score" not in matchup.markets:
                continue
            com_especial += 1

            modelo = modelo_do_jogo(matchup, "")
            if modelo is None:
                continue
            residuos.append(modelo.erro)
            print(f"  {matchup.display_name[:46]:47} "
                  f"λ {modelo.lambda_casa:.2f}/{modelo.lambda_fora:.2f}  "
                  f"resíduo {modelo.erro:.6f}")

            for chave in ("correct_score", "exact_goals", "ht_ft"):
                market = matchup.markets.get(chave)
                reais = remover_vig(market) if market else None
                if not reais:
                    continue
                for nome, real in reais.items():
                    derivada = probabilidade_derivada(chave, nome, matchup)
                    if derivada is None:
                        continue
                    erros.setdefault(chave, []).append(
                        abs(derivada.probabilidade - real.probabilidade)
                        / real.probabilidade)

    print(f"\n{com_especial} jogo(s) com especial publicado")
    if residuos:
        residuos.sort()
        passaram = sum(1 for r in residuos if r <= modelo_erro_max())
        print(f"resíduo do ajuste: mediana {statistics.median(residuos):.6f}  "
              f"| {passaram}/{len(residuos)} abaixo de MODELO_ERRO_MAX")
    if not erros:
        print("\nnenhuma comparação possível — o filtro de resíduo barrou tudo.")
        return 1

    print("\nerro relativo da odd derivada contra a odd real:")
    for chave, vals in sorted(erros.items()):
        vals.sort()
        print(f"  {chave:16} n={len(vals):4}  mediana={statistics.median(vals)*100:5.1f}%"
              f"  p90={vals[int(len(vals) * 0.9)] * 100:5.1f}%"
              f"  máx={vals[-1] * 100:6.1f}%")
    print("\nEDGE_MIN_MODELO precisa ficar ACIMA do p90 — abaixo dele, o que o "
          "sistema chamaria de value seria erro do próprio modelo.")
    return 0


def modelo_erro_max() -> float:
    from betano_superodds.value import config as vconfig
    return vconfig.MODELO_ERRO_MAX


def main() -> int:
    p = argparse.ArgumentParser(description="Avalia value bet das super odds")
    p.add_argument("--fonte", choices=["mr12", "smartpick"])
    p.add_argument("--todas", action="store_true", help="inclui ofertas expiradas")
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--calibrar-modelo", type=int, nargs="?", const=8, metavar="JOGOS",
                   help="mede o erro do modelo de placar contra os especiais reais")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--trace", action="store_true",
                   help="loga chave resolvida + label da Pinnacle por perna "
                        "(FAIR_ODDS_TRACE=1) — pra achar mercado restrito "
                        "casado com referência ampla")
    args = p.parse_args()

    setup(args.verbose)
    if args.trace:
        # Setado aqui, não antes do parse: `fair_odds._trace_ativo()` lê a
        # env var em cada chamada, então não importa que o módulo já tenha
        # sido importado no topo do arquivo — só precisa estar setada antes
        # de `avaliar_ofertas` rodar, mais abaixo.
        os.environ["FAIR_ODDS_TRACE"] = "1"

    if args.calibrar_modelo:
        return calibrar_modelo(args.calibrar_modelo)

    ofertas = carregar(args.fonte, args.todas, args.limit)
    if not ofertas:
        print("nenhuma oferta pra avaliar")
        return 1

    print(f"avaliando {len(ofertas)} oferta(s)...\n")
    # Com storage a avaliação também tenta o consenso entre casas, que é a
    # única referência possível pros props que a Pinnacle não publica.
    with Storage() as storage:
        resultados = avaliar_ofertas(ofertas, storage)

    for r in resultados:
        print(formatar(r))
        print()

    status = Counter(r["status"] for r in resultados)
    values = [r for r in resultados if r.get("is_value")]
    print("=" * 70)
    print("resumo:", dict(status))
    print(f"value bets encontradas: {len(values)}")
    for r in sorted(values, key=lambda x: x["edge_pct"], reverse=True):
        print(f"  +{r['edge_pct']:.1f}%  {r['evento']} — {r['mercado']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
