"""Loop principal: raspa → compara → loga → salva → avalia value → alerta.

A avaliação e o alerta ficam em `alerts.py`; aqui só se decide *quando* chamar.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from . import config
from .alerts import Alertador
from .casadeaposta import CasaDeApostaScraper
from .diff import DiffResult, diff_offers
from .esportiva import EsportivaScraper
from .novibet import NovibetScraper
from .sportingtech import CASAS_SPORTINGTECH, SportingTechScraper
from .notifier import TelegramNotifier
from .revalidacao import revalidar_oferta
from .scraper import BetanoScraper, ScraperError
from .storage import Storage
from .value import config as vconfig
from .value.market_parser import parse_mercado

log = logging.getLogger("betano")


def setup_logging(verbose: bool = False) -> None:
    # O console do Windows costuma vir em cp1252 e engasga com acento/emoji.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass

    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="[%(asctime)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("curl_cffi").setLevel(logging.WARNING)


def report(result: DiffResult) -> None:
    """Saída no console — por enquanto o único 'alerta' do sistema."""
    for offer in result.novas:
        log.info("NOVA: %s", offer.describe())

    for change in result.alteradas:
        log.info("ALTERADA: %s", change.describe())

    for row in result.expiradas:
        log.info("EXPIRADA: %s — %s", row["evento"], row["mercado"])

    # Sem este sufixo, um ciclo de rodízio pareceria ter perdido ofertas: o
    # número de ativas inclui casas que ninguém visitou agora.
    dormindo = (f", {len(result.fora_do_ciclo)} de casas fora do ciclo"
                if result.fora_do_ciclo else "")

    if not result.has_changes:
        log.info("SEM MUDANÇA (%d ofertas ativas%s)", result.total_ativas, dormindo)
    else:
        log.info(
            "ciclo: %d ativas (+%d novas, ~%d alteradas, -%d expiradas%s)",
            result.total_ativas, len(result.novas), len(result.alteradas),
            len(result.expiradas), dormindo,
        )


def check_breakage(storage: Storage, alertador: Alertador | None = None) -> None:
    streak = storage.consecutive_failed_runs()
    if streak >= config.BREAKAGE_ALERT_AFTER_EMPTY_RUNS:
        log.error(
            "POSSÍVEL QUEBRA DO SCRAPER: %d ciclos seguidos sem capturar nada. "
            "A Betano provavelmente mudou a estrutura da API — revise scraper.py.",
            streak,
        )
        if alertador is not None:
            alertador.alertar_quebra(streak)


CURSOR_RODIZIO = "altenar_cursor"


def casas_do_ciclo(storage: Storage) -> list[config.CasaAltenar]:
    """As casas Altenar deste ciclo, girando a lista a cada chamada.

    Com `CASAS_POR_CICLO=3` e 10 casas ligadas, cada casa é visitada a cada ~4
    ciclos (12 min no intervalo padrão) — bem dentro do TTL de reavaliação de
    60 min, então nenhuma oferta fica sem olhar por tempo demais.

    O cursor mora no banco: reiniciar o processo continua de onde parou em vez
    de bater sempre nas mesmas primeiras casas.
    """
    ativas = list(config.CASAS_ALTENAR_ATIVAS)
    if not ativas:
        return []

    n = config.CASAS_POR_CICLO
    if n <= 0 or n >= len(ativas):
        return ativas

    try:
        cursor = int(storage.get_estado(CURSOR_RODIZIO) or 0)
    except ValueError:
        cursor = 0
    cursor %= len(ativas)

    escolhidas = [ativas[(cursor + i) % len(ativas)] for i in range(n)]
    storage.set_estado(CURSOR_RODIZIO, str((cursor + n) % len(ativas)))
    return escolhidas


def avancar_offset(storage: Storage, casa: config.CasaAltenar) -> int:
    """Onde esta visita começa a sondar a listagem da casa, e avança o cursor.

    Existe porque o boost não fica sempre no topo: na VaiDeBet os combos só
    aparecem a partir do 20º jogo, e um teto de 12 a partir do zero nunca
    chegava lá. Girando o ponto de partida, a janela inteira é coberta em
    algumas visitas sem aumentar o custo por visita.
    """
    chave = f"altenar_offset_{casa.slug}"
    try:
        atual = int(storage.get_estado(chave) or 0)
    except ValueError:
        atual = 0
    janela = max(1, config.ALTENAR_JANELA_SONDA)
    storage.set_estado(chave, str((atual + config.ALTENAR_MAX_DETALHES) % janela))
    return atual % janela


def alvos_da_fila(storage: Storage) -> list[dict]:
    """`{"evento", "inicio_evento"}` do que a avaliação precisa precificar.

    A fila (`Storage.pendentes_de_avaliacao`, o MESMO SELECT que o alertador
    usa depois no ciclo) filtrada pelas ofertas com pelo menos uma perna sem
    cobertura Pinnacle — são as únicas que dependem do consenso, e portanto
    as únicas em que sondar a casa muda alguma coisa.

    Reduzido a evento distinto: uma oferta por perna sondaria o mesmo jogo
    várias vezes. A ordem da fila já vem por kickoff mais próximo, e
    `dict.fromkeys` preserva isso.
    """
    try:
        fila = storage.pendentes_de_avaliacao(config.MAX_AVALIACOES_POR_CICLO,
                                              config.REAVALIAR_APOS_MINUTOS)
    except Exception as exc:  # noqa: BLE001 — sonda direcionada é otimização
        log.warning("não deu pra ler a fila pra sonda direcionada: %s", exc)
        return []

    vistos: dict[tuple[str, str], dict] = {}
    for oferta in fila:
        evento, inicio = oferta.get("evento"), oferta.get("inicio_evento")
        if not evento or not inicio:
            continue
        legs = parse_mercado(oferta.get("mercado") or "")
        if not any(not leg.suportado for leg in legs):
            continue
        vistos.setdefault((evento, inicio),
                          {"evento": evento, "inicio_evento": inicio})
    return list(vistos.values())


async def coletar(
    scraper: BetanoScraper, casas: list[config.CasaAltenar],
    offsets: dict[str, int] | None = None,
    alvos: list[dict] | None = None,
) -> tuple[list, set[str], set[tuple[str, str]], list]:
    """Ofertas do ciclo + o que ele de fato olhou.

    Devolve também os dois escopos que o diff precisa para não expirar o que
    ninguém conferiu: as casas visitadas e os `(casa, evento)` cujo detalhe foi
    pedido — e os mercados completos vistos, matéria prima do consenso.

    Uma casa que quebra não derruba as outras: o ciclo só falha se TODAS
    falharem — mesma política que já valia entre as fontes da Betano. Por isso
    a casa só entra em "raspadas" se a captura dela deu certo: incluir uma casa
    que falhou faria o diff expirar as ofertas dela por engano.
    """
    t0 = time.monotonic()
    offers = await scraper.scrape()
    dt_betano = time.monotonic() - t0
    log.info("Betano: %d ofertas em %.1fs", len(offers), dt_betano)

    raspadas = {"Betano"}
    escopo: set[tuple[str, str]] = set()
    # A Betano é a única fonte de preço fora do feed Altenar: as 11 casas
    # Altenar são medidamente um feed só (90,3% de preços idênticos), então
    # isto é o que dá independência real ao consenso.
    mercados: list = list(scraper.mercados_vistos)

    if config.ENABLE_CASADEAPOSTA:
        tc = time.monotonic()
        try:
            async with CasaDeApostaScraper() as sc:
                casa_offers = await sc.scrape()
            dt_casa = time.monotonic() - tc
            log.info("CasaDeAposta: %d ofertas em %.1fs", len(casa_offers), dt_casa)
            offers += casa_offers
            raspadas.add("CasaDeAposta")
        except Exception as exc:  # noqa: BLE001 — mesma política das outras casas
            dt_casa = time.monotonic() - tc
            log.warning("CasaDeAposta falhou neste ciclo (%.1fs): %s", dt_casa, exc)

    if config.ENABLE_NOVIBET:
        tc = time.monotonic()
        try:
            async with NovibetScraper() as sc:
                casa_offers = await sc.scrape()
            dt_casa = time.monotonic() - tc
            log.info("Novibet: %d ofertas em %.1fs", len(casa_offers), dt_casa)
            offers += casa_offers
            raspadas.add("Novibet")
        except Exception as exc:  # noqa: BLE001 — mesma política das outras casas
            dt_casa = time.monotonic() - tc
            log.warning("Novibet falhou neste ciclo (%.1fs): %s", dt_casa, exc)

    if config.ENABLE_SPORTINGTECH:
        for casa_st in CASAS_SPORTINGTECH:
            tc = time.monotonic()
            try:
                async with SportingTechScraper(casa_st) as sc:
                    casa_offers = await sc.scrape()
                dt_casa = time.monotonic() - tc
                log.info("%s: %d ofertas em %.1fs", casa_st.nome, len(casa_offers), dt_casa)
                offers += casa_offers
                raspadas.add(casa_st.nome)
            except Exception as exc:  # noqa: BLE001 — mesma política das outras casas
                dt_casa = time.monotonic() - tc
                log.warning("%s falhou neste ciclo (%.1fs): %s", casa_st.nome, dt_casa, exc)

    for casa in casas:
        tc = time.monotonic()
        try:
            async with EsportivaScraper(
                casa, offset=(offsets or {}).get(casa.slug, 0), alvos=alvos
            ) as sc:
                casa_offers = await sc.scrape()
                escopo |= {(casa.nome, ev) for ev in sc.eventos_sondados}
                mercados += sc.mercados_vistos
            dt_casa = time.monotonic() - tc
            log.info("%s: %d ofertas em %.1fs (%d eventos sondados)",
                     casa.nome, len(casa_offers), dt_casa, len(sc.eventos_sondados))
            offers += casa_offers
            raspadas.add(casa.nome)
        except Exception as exc:  # noqa: BLE001 — as outras casas não caem junto
            dt_casa = time.monotonic() - tc
            log.warning("%s falhou neste ciclo (%.1fs): %s", casa.nome, dt_casa, exc)

    return offers, raspadas, escopo, mercados


async def run_cycle(scraper: BetanoScraper, storage: Storage,
                    alertador: Alertador | None = None) -> DiffResult | None:
    """Um ciclo completo. Devolve None se a captura falhou."""
    t_ciclo = time.monotonic()
    log.info("── início do ciclo ──")

    casas = casas_do_ciclo(storage) if config.ENABLE_ALTENAR else []
    offsets = {c.slug: avancar_offset(storage, c) for c in casas}
    if casas:
        log.info("casas Altenar neste ciclo: %s",
                 ", ".join(f"{c.nome}@{offsets[c.slug]}" for c in casas))

    # Lido ANTES da coleta: é a fila que o alertador vai avaliar no fim deste
    # mesmo ciclo, e os mercados sondados agora já entram no pool a tempo de
    # servir de referência pra ela (`salvar_mercados_casa` roda logo abaixo,
    # antes da avaliação, pelo mesmo motivo).
    alvos = alvos_da_fila(storage) if casas else []
    if alvos:
        log.info("sonda direcionada: %d evento(s) da fila com perna sem "
                 "cobertura, %d vaga(s) por casa", len(alvos),
                 config.ALTENAR_DETALHES_FILA)
    try:
        offers, raspadas, escopo, mercados = await coletar(scraper, casas, offsets,
                                                           alvos=alvos)
    except ScraperError as exc:
        log.error("captura falhou: %s", exc)
        storage.record_run(ofertas=0, erro=str(exc))
        check_breakage(storage, alertador)
        return None
    except Exception as exc:  # noqa: BLE001 — o loop não pode morrer por erro pontual
        log.exception("erro inesperado na captura: %s", exc)
        storage.record_run(ofertas=0, erro=repr(exc))
        check_breakage(storage, alertador)
        return None

    dt_coleta = time.monotonic() - t_ciclo
    log.info("coleta total: %d ofertas de %d fontes em %.1fs",
             len(offers), len(raspadas), dt_coleta)

    # Antes do diff: a avaliação que roda no fim deste ciclo já consulta esta
    # tabela pra formar consenso, então gravar depois atrasaria a referência
    # num ciclo inteiro.
    if mercados:
        n = storage.salvar_mercados_casa(mercados)
        podados = storage.limpar_mercados_casa(vconfig.CONSENSO_RETENCAO_HORAS)
        log.info("mercados p/ consenso: %d gravados, %d podados", n, podados)
        # O cache da ponte fuzzy (Storage._pool_matchups/_pool_cache) fica
        # obsoleto assim que o pool muda — invalidar aqui é o único ponto
        # onde isso acontece no ciclo.
        storage.invalidar_cache_pool()

    previous = storage.active_offers()
    result = diff_offers(previous, offers, casas_raspadas=raspadas,
                         escopo_detalhe=escopo)
    report(result)

    storage.apply_diff(result)
    storage.record_run(
        ofertas=len(offers),
        novas=len(result.novas),
        alteradas=len(result.alteradas),
        expiradas=len(result.expiradas),
    )
    check_breakage(storage, alertador)

    if alertador is not None:
        t_eval = time.monotonic()
        ciclo = await alertador.processar_ciclo()
        dt_eval = time.monotonic() - t_eval
        # ⚠️ A ORDEM IMPORTA e não é intercambiável. `acumular` zera o balde
        # quando vira o dia do relógio; `talvez_resumo_diario` pode estar
        # fechando o dia ANTERIOR (alvo 23:59 + tolerância cai em 00:0x, ver
        # config.RESUMO_TOLERANCIA_MINUTOS). Acumular antes apagaria
        # avaliadas/values/melhor_edge do dia que o resumo está justamente
        # reportando, e o relatório sairia zerado sem nenhum erro no log.
        # O custo de reportar primeiro é o resumo não incluir ESTE ciclo —
        # um de ~300 no dia.
        alertador.talvez_resumo_diario()
        alertador.acumular(ciclo)
        # Por último de propósito: fala com o SofaScore, e nada que faça rede
        # pode ficar entre o fim da avaliação e o envio do heartbeat.
        await alertador.liquidar_pendentes()
        if ciclo.get("avaliadas", 0) > 0:
            log.info("avaliação: %d avaliada(s), %d value(s), melhor edge %s — %.1fs",
                     ciclo["avaliadas"], ciclo["values"],
                     f"{ciclo['melhor_edge']:+.1f}%" if ciclo.get("melhor_edge") is not None else "n/a",
                     dt_eval)

    dt_total = time.monotonic() - t_ciclo
    log.info("── ciclo completo em %.1fs ──", dt_total)

    return result


async def main_async(once: bool, interval: int, avaliar: bool) -> int:
    with Storage() as storage, TelegramNotifier() as notifier:
        log.info("banco: %s", storage.db_path)
        log.info(
            "esportes: %s | fontes Betano: %s",
            ", ".join(s.name for s in config.SPORTS),
            ", ".join(
                n for n, on in (("MR12", config.ENABLE_MR12),
                                ("smart-picks", config.ENABLE_SMART_PICKS)) if on
            ),
        )
        if config.ENABLE_ALTENAR and config.CASAS_ALTENAR_ATIVAS:
            ativas = config.CASAS_ALTENAR_ATIVAS
            por_ciclo = config.CASAS_POR_CICLO
            if por_ciclo <= 0 or por_ciclo >= len(ativas):
                ritmo = "todas por ciclo"
            else:
                voltas = -(-len(ativas) // por_ciclo)   # teto da divisão
                ritmo = (f"{por_ciclo} por ciclo, rodízio — cada casa a cada "
                         f"{voltas} ciclos (~{voltas * interval // 60} min)")
            log.info("casas Altenar (%d): %s", len(ativas),
                     ", ".join(c.nome for c in ativas))
            log.info("ritmo: %s | teto de %d detalhes por casa",
                     ritmo, config.ALTENAR_MAX_DETALHES)
        else:
            log.info("casas Altenar: desligadas")
        log.info("CasaDeAposta: %s", "ligada" if config.ENABLE_CASADEAPOSTA else "desligada")
        log.info("Novibet: %s", "ligada" if config.ENABLE_NOVIBET else "desligada")
        log.info("SportingTech (%s): %s",
                 ", ".join(c.nome for c in CASAS_SPORTINGTECH),
                 "ligada" if config.ENABLE_SPORTINGTECH else "desligada")

        alertador: Alertador | None = None
        if avaliar:
            alertador = Alertador(storage, notifier, revalidar_oferta)
            if notifier.ativo:
                log.info("telegram: ativo (resumo diário às %02d:%02d)",
                         config.RESUMO_DIARIO_HORA, config.RESUMO_DIARIO_MINUTO)
            else:
                log.warning(
                    "telegram: INATIVO — defina TELEGRAM_BOT_TOKEN e TELEGRAM_CHAT_ID. "
                    "As value bets vão só para o console."
                )
        else:
            log.info("avaliação de value desligada (--sem-value)")

        async with BetanoScraper() as scraper:
            if once:
                result = await run_cycle(scraper, storage, alertador)
                log.info("estado do banco: %s", storage.stats())
                return 0 if result is not None else 1

            log.info("polling a cada %ds — Ctrl+C para parar", interval)
            while True:
                await run_cycle(scraper, storage, alertador)
                try:
                    await asyncio.sleep(interval)
                except asyncio.CancelledError:
                    raise


def testar_telegram() -> int:
    """Valida token e chat_id antes de deixar o loop rodando por horas."""
    with TelegramNotifier() as notifier:
        if not notifier.ativo:
            log.error("TELEGRAM_BOT_TOKEN e/ou TELEGRAM_CHAT_ID não definidos.")
            log.error("  token: pegue com o @BotFather")
            log.error("  chat id: mande qualquer coisa pro @userinfobot")
            return 1
        ok = notifier.enviar(
            "✅ <b>Betano Super Odds</b>\n\nNotificação configurada. "
            "Você vai receber alerta quando aparecer value bet, "
            f"e um resumo diário às {config.RESUMO_DIARIO_HORA:02d}:"
            f"{config.RESUMO_DIARIO_MINUTO:02d}."
        )
        log.info("mensagem enviada" if ok else "falhou — veja o erro acima")
        return 0 if ok else 1


async def liquidar_agora(max_partidas: int) -> int:
    """Liquida em lote e imprime o P&L, sem esperar o resumo das 23:59.

    Existe pra dar pra conferir um veredito à mão contra o placar no site do
    SofaScore — a única validação que pega match errado, que é o modo de falha
    caro aqui (P&L errado reportado com confiança total).
    """
    with Storage() as storage, TelegramNotifier() as notifier:
        alertador = Alertador(storage, notifier)
        total = 0
        # Uma rodada por lote até drenar ou bater o teto pedido.
        while total < max_partidas:
            resultado = await alertador.liquidar_pendentes()
            if not resultado.get("tentadas"):
                break
            total += resultado["tentadas"]

        apostas = storage.apostas_liquidadas_24h()
        if not apostas:
            print("nenhuma aposta liquidada nas últimas 24h.")
            return 0

        saldo = 0.0
        for a in apostas:
            unidades = _unidades_cli(a)
            saldo += unidades or 0.0
            marca = {"green": "GREEN", "red": "RED  ", "void": "VOID ",
                     "desconhecido": "?    "}.get(a["resultado"], "?    ")
            extra = f"{unidades:+.2f}un" if unidades is not None else "  --  "
            print(f"  {marca} {extra:>9}  {(a['evento'] or '')[:34]:34} | "
                  f"{(a['mercado'] or '')[:52]}")
            if a["resultado"] == "desconhecido" and a.get("motivo"):
                print(f"          motivo: {a['motivo'][:88]}")
        print(f"\n{len(apostas)} aposta(s) · saldo {saldo:+.2f}un")
        return 0


def _unidades_cli(aposta: dict) -> float | None:
    from .notifier import _unidades
    return _unidades(aposta)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scanner de Super Odds da Betano")
    parser.add_argument("--once", action="store_true", help="roda um ciclo só e sai")
    parser.add_argument("--interval", type=int, default=config.POLL_INTERVAL_SECONDS,
                        help=f"segundos entre ciclos (padrão: {config.POLL_INTERVAL_SECONDS})")
    parser.add_argument("-v", "--verbose", action="store_true", help="log de debug")
    parser.add_argument("--sem-value", action="store_true",
                        help="só raspa e salva, sem avaliar value nem notificar")
    parser.add_argument("--testar-telegram", action="store_true",
                        help="manda uma mensagem de teste e sai")
    parser.add_argument("--liquidar-agora", type=int, nargs="?", const=10,
                        metavar="PARTIDAS",
                        help="liquida apostas de jogos já encerrados e mostra o "
                             "P&L, sem esperar o resumo das 23:59")
    args = parser.parse_args()

    setup_logging(args.verbose)

    if args.testar_telegram:
        return testar_telegram()
    if args.liquidar_agora is not None:
        return asyncio.run(liquidar_agora(args.liquidar_agora))

    try:
        return asyncio.run(main_async(args.once, args.interval, not args.sem_value))
    except KeyboardInterrupt:
        log.info("encerrado pelo usuário")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
