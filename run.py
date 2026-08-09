"""Ponto de entrada do scanner — é por aqui que se roda no dia a dia.

    python run.py --setup     # configura o Telegram (guiado) e grava o .env
    python run.py             # loop contínuo com alerta
    python run.py --once      # um ciclo e sai
    python run.py --test      # manda uma mensagem de teste e sai

`--setup` descobre o `chat_id` sozinho: você manda qualquer coisa pro seu bot e
ele lê de `getUpdates`. Sem ida ao @userinfobot, sem copiar número na mão.
"""

from __future__ import annotations

import argparse
import asyncio
import time

from betano_superodds import config, dotenv
from betano_superodds.main import main_async, setup_logging
from betano_superodds.notifier import TelegramNotifier

# Quanto tempo esperar você mandar a primeira mensagem pro bot, no --setup.
ESPERA_CHAT_ID_SEGUNDOS = 180
INTERVALO_POLL = 3.0


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _perguntar(rotulo: str) -> str:
    try:
        return input(rotulo).strip()
    except (EOFError, KeyboardInterrupt):
        _p()
        return ""


def setup() -> int:
    """Configura o Telegram de ponta a ponta e grava no .env."""
    _p("=" * 62)
    _p("  Configuração do Telegram")
    _p("=" * 62)
    _p()

    # --- token ---------------------------------------------------------
    token = config.TELEGRAM_BOT_TOKEN
    if token:
        _p(f"token: já configurado ({token[:10]}…)")
    else:
        _p("1) Abra o Telegram e fale com o @BotFather")
        _p("2) Mande /newbot e siga as instruções")
        _p("3) Ele devolve um token tipo 123456:ABC-DEF...")
        _p()
        token = _perguntar("cole o token aqui: ")
        if not token:
            _p("\nsem token não dá pra continuar.")
            return 1

    notifier = TelegramNotifier(token=token, chat_id="")
    bot = notifier.identidade()
    if bot is None:
        _p("\n✗ token recusado pelo Telegram. Confira se copiou inteiro.")
        return 1
    usuario = bot.get("username", "?")
    _p(f"\n✓ bot reconhecido: @{usuario}")

    # --- chat id -------------------------------------------------------
    chat_id = config.TELEGRAM_CHAT_ID
    if chat_id:
        _p(f"✓ chat id já configurado: {chat_id}")
    else:
        _p()
        _p(f"Agora abra https://t.me/{usuario} e mande qualquer mensagem "
           f"(um 'oi' serve).")
        _p("Estou esperando…")

        limite = time.monotonic() + ESPERA_CHAT_ID_SEGUNDOS
        chat_id = None
        while time.monotonic() < limite:
            chat_id = notifier.descobrir_chat_id()
            if chat_id:
                break
            time.sleep(INTERVALO_POLL)

        if not chat_id:
            _p("\n✗ nenhuma mensagem chegou.")
            _p("  Se o bot for novo, mande /start pra ele e rode de novo.")
            _p("  O Telegram só entrega updates de conversas já iniciadas.")
            return 1
        _p(f"\n✓ chat id encontrado: {chat_id}")

    # --- grava e confirma ----------------------------------------------
    caminho = dotenv.escrever({
        "TELEGRAM_BOT_TOKEN": token,
        "TELEGRAM_CHAT_ID": chat_id,
    })
    _p(f"✓ gravado em {caminho}")

    notifier.chat_id = chat_id
    enviado = notifier.enviar(
        "✅ <b>Betano Super Odds</b>\n\n"
        "Configurado. Você recebe alerta quando aparecer value bet "
        f"e um resumo diário às {config.RESUMO_DIARIO_HORA:02d}:"
        f"{config.RESUMO_DIARIO_MINUTO:02d}.\n\n"
        "<i>Aviso honesto: value bet é raro. Na validação foram 0 em 241 "
        "ofertas. Bot quieto é o esperado, não defeito.</i>"
    )
    if not enviado:
        _p("\n✗ gravei o .env mas a mensagem de teste não saiu.")
        return 1

    _p("✓ mensagem de teste enviada — confere no chat")
    _p()
    _p("Pronto. Agora é só:  python run.py")
    return 0


def testar() -> int:
    with TelegramNotifier() as notifier:
        if not notifier.ativo:
            _p("Telegram não configurado. Rode:  python run.py --setup")
            return 1
        bot = notifier.identidade()
        if bot is None:
            _p("✗ token inválido ou sem rede.")
            return 1
        ok = notifier.enviar(
            "🔔 Teste do <b>Betano Super Odds</b> — está tudo ligado.")
        _p(f"✓ enviado para o chat {notifier.chat_id} (@{bot.get('username')})"
           if ok else "✗ falhou ao enviar — rode com -v pra ver o erro")
        return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        description="Scanner de Super Odds da Betano com alerta no Telegram",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--setup", action="store_true",
                   help="configura o Telegram de forma guiada e grava o .env")
    p.add_argument("--test", action="store_true",
                   help="manda uma mensagem de teste e sai")
    p.add_argument("--once", action="store_true", help="roda um ciclo só e sai")
    p.add_argument("--interval", type=int, default=config.POLL_INTERVAL_SECONDS,
                   help=f"segundos entre ciclos (padrão: {config.POLL_INTERVAL_SECONDS})")
    p.add_argument("--sem-value", action="store_true",
                   help="só raspa e salva, sem avaliar value nem notificar")
    p.add_argument("-v", "--verbose", action="store_true", help="log de debug")
    args = p.parse_args()

    setup_logging(args.verbose)

    if args.setup:
        return setup()
    if args.test:
        return testar()

    if not config.TELEGRAM_BOT_TOKEN and not args.sem_value:
        _p("⚠ Telegram não configurado — os alertas vão só pro console.")
        _p("  Para receber no celular:  python run.py --setup")
        _p()

    try:
        return asyncio.run(main_async(args.once, args.interval, not args.sem_value))
    except KeyboardInterrupt:
        _p("\nencerrado.")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
