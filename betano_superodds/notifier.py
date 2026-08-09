"""Notificação por Telegram.

Push puro, via a API HTTP do bot. Não usa `python-telegram-bot`: o projeto já
carrega `curl_cffi`, e o que precisamos daqui é um POST em `sendMessage`.

    export TELEGRAM_BOT_TOKEN="123456:ABC-DEF..."   # do @BotFather
    export TELEGRAM_CHAT_ID="987654321"             # do @userinfobot

Sem as duas variáveis o notificador fica inerte: `ativo` é False, `enviar()`
vira no-op e o loop de scraping segue logando no console. É de propósito —
notificação é acessório, e falta de credencial não pode derrubar a captura.
"""

from __future__ import annotations

import html
import logging
import re
from datetime import datetime

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

from . import config
from .models import minutos_ate_inicio
from .value import config as vconfig

log = logging.getLogger(__name__)

# O Telegram corta mensagem em 4096 caracteres.
LIMITE_TELEGRAM = 4000


# Teto do texto de mercado num bloco de aposta. Combo real chega a ~80 chars;
# este corte só existe pro caso patológico de um rótulo gigante estourar o
# limite do Telegram sozinho.
MERCADO_MAX_CHARS = 160

# Espaço reservado pro cabeçalho "(2/3)" e o separador durante o empacotamento.
# O contador só é conhecido depois de fatiar, então o espaço é reservado antes.
RESERVA_CABECALHO = 90


def _esc(valor: object) -> str:
    """Escapa pro parse_mode=HTML — nome de time pode conter & ou <."""
    return html.escape(str(valor), quote=False)


def _encurtar(texto: str, limite: int) -> str:
    """Corta o DADO, antes de escapar e montar HTML.

    Truncar dado é sempre seguro; truncar HTML montado pode partir uma tag e
    fazer o Telegram recusar a mensagem inteira.
    """
    texto = str(texto)
    return texto if len(texto) <= limite else texto[:limite - 1].rstrip() + "…"


def _unidades(aposta: dict) -> float | None:
    """Unidades ganhas/perdidas, ou `None` quando a aposta fica fora do saldo.

    Fora do saldo, e de propósito: veredito não resolvido (não houve ganho nem
    perda), alerta sem stake gravada (anterior à migração — inventar a stake
    contradiria "o relatório registra o que foi recomendado") e stake abaixo do
    mínimo, que o alerta marcou como "não vale a pena".
    """
    resultado = aposta.get("resultado")
    if resultado not in ("green", "red"):
        return None
    stake, odd = aposta.get("stake_unidades"), aposta.get("odd_boost")
    if stake is None or odd is None:
        return None
    if aposta.get("stake_apostavel") == 0:
        return 0.0
    return stake * (odd - 1.0) if resultado == "green" else -float(stake)


def _veredito_visual(aposta: dict) -> tuple[str, str]:
    """(ícone, texto do P&L) de uma aposta."""
    resultado = aposta.get("resultado")
    if resultado == "void":
        return "➖", "<i>anulada</i>"
    if resultado not in ("green", "red"):
        return "❔", "<i>não apurada</i>"
    unidades = _unidades(aposta)
    icone = "✅" if resultado == "green" else "❌"
    if unidades is None:
        return icone, "<i>sem stake registrada</i>"
    if aposta.get("stake_apostavel") == 0:
        return icone, "<i>não recomendada (0un)</i>"
    return icone, f"<b>{unidades:+.2f}un</b>"


def _amostras_apostas_do_dia(fmt) -> list[tuple[str, str]]:
    """Amostras da seção de apostas, uma por PARTE da mensagem.

    Dimensionada em 45 apostas de propósito — é o volume real medido (37-46/dia)
    e força o fatiamento em 2+ partes. Com isso, a asserção de tamanho que a
    suíte já faz sobre cada amostra vira teste de regressão do empacotador de
    graça: se alguém trocar o corte por bloco por um corte por caractere, uma
    das partes estoura ou sai com tag partida, e o teste quebra.
    """
    modelos = [
        {"evento": "Velez Sarsfield - CA Independiente",
         "mercado": "Total de Gols Mais de 1.5 + Escanteios Mais de 8.5",
         "odd_boost": 7.26, "stake_unidades": 0.25, "stake_apostavel": 1,
         "resultado": "green"},
        {"evento": "Corinthians - Internacional",
         "mercado": "1º tempo - Internacional gols exatos: 1",
         "odd_boost": 3.45, "stake_unidades": 1.0, "stake_apostavel": 1,
         "resultado": "red"},
        {"evento": "PAOK Salônica - RSC Anderlecht",
         "mercado": "PAOK Salônica total: Mais de 1.5 + Cartões 1x2: RSC Anderlecht",
         "odd_boost": 3.70, "stake_unidades": 0.5, "stake_apostavel": 1,
         "resultado": "desconhecido"},
        # & e < provam o escape; sem ele o Telegram recusa a mensagem toda.
        {"evento": "Brøndby & <Co> - Preußen Münster",
         "mercado": "Resultado Final: Brøndby & <Co>", "odd_boost": 2.10,
         "stake_unidades": 2.0, "stake_apostavel": 1, "resultado": "green"},
        # Alerta anterior à migração: sem stake gravada, fica fora do saldo.
        {"evento": "Portland Timbers - Club Puebla",
         "mercado": "Resultado Final: Empate", "odd_boost": 5.30,
         "stake_unidades": None, "stake_apostavel": None, "resultado": "red"},
        # Jogo adiado: anulada, nunca derrota.
        {"evento": "Lech Poznan - KI Klaksvik",
         "mercado": "Total de Escanteios: Mais de 9,5", "odd_boost": 1.95,
         "stake_unidades": 1.5, "stake_apostavel": 1, "resultado": "void"},
    ]
    muitas = [dict(modelos[i % len(modelos)]) for i in range(45)]
    partes = fmt.formatar_apostas_do_dia(muitas)
    amostras = [(f"apostas-do-dia-{i}de{len(partes)}", t)
                for i, t in enumerate(partes, start=1)]
    amostras.append(("apostas-do-dia-uma-so",
                     fmt.formatar_apostas_do_dia(modelos[:1])[0]))
    return amostras


def _empacotar(blocos: list[str], *, cabecalho: str, rodape: str,
               limite: int) -> list[str]:
    """Empacota blocos em mensagens, sem NUNCA partir um bloco.

    É esta propriedade que garante HTML balanceado: cada bloco é balanceado
    sozinho, e todo corte cai entre blocos.
    """
    if not blocos:
        return []
    util = limite - RESERVA_CABECALHO - len(rodape)
    paginas: list[list[str]] = [[]]
    atual = 0
    for bloco in blocos:
        # Bloco patológico sozinho maior que a página: vai numa página só dele.
        # O corte de `MERCADO_MAX_CHARS` já torna isso quase impossível.
        candidato = atual + len(bloco) + 2
        if paginas[-1] and candidato > util:
            paginas.append([bloco])
            atual = len(bloco)
        else:
            paginas[-1].append(bloco)
            atual = candidato
    total = len(paginas)
    saida = []
    for i, pagina in enumerate(paginas, start=1):
        titulo = cabecalho if total == 1 else f"{cabecalho} ({i}/{total})"
        # Blocos separados por linha em branco (os 2 chars já reservados no
        # orçamento acima). Sem isso a lista vira um paredão ilegível no chat.
        corpo = [titulo, "━━━━━━━━━━━━━━━━━━━━━━", "", "\n\n".join(pagina)]
        if i == total:
            corpo += ["", "━━━━━━━━━━━━━━━━━━━━━━", rodape]
        saida.append("\n".join(corpo))
    return saida


def _duracao(minutos: float) -> str:
    """"7 min", "2h14". Sem segundos: a precisão aqui é ilusória de qualquer
    jeito (a captura pode ter minutos de idade)."""
    # Arredonda em vez de truncar: 179,9 min é "3h00" pra quem lê, não "2h59".
    total = round(minutos)
    if total < 60:
        return f"{total} min"
    return f"{total // 60}h{total % 60:02d}"


def _linha_inicio(resultado: dict) -> str | None:
    """"🕐 Começa em 2h14 (18:30)" — a informação que faltava no card.

    Sem ela não dava pra saber, olhando o alerta, se dava tempo de abrir o site
    e conferir o teto de aposta antes da bola rolar.
    """
    faltam = minutos_ate_inicio(resultado)
    if faltam is None or faltam <= 0:
        return None
    bruto = resultado.get("inicio_evento") or resultado.get("valido_ate")
    try:
        hora = datetime.fromisoformat(
            str(bruto).replace("Z", "+00:00")).astimezone().strftime("%H:%M")
    except ValueError:
        return f"🕐 Começa em <b>{_duracao(faltam)}</b>"
    return f"🕐 Começa em <b>{_duracao(faltam)}</b> ({hora})"


# Mesmo prefixo que `market_parser._PREFIXO_JOGO` reconhece na entrada — aqui
# é só pra exibição, quebrar "[Jogo] Mercado: Seleção" em jogo + resto.
_PREFIXO_JOGO_RE = re.compile(r"^\[([^\]]+)\]\s*")


def _formatar_the_bet(resultado: dict) -> list[str]:
    """Linhas do bloco THE BET.

    Combo de jogo único (o caso comum): mercado cru, igual a sempre. Combo
    multi-jogo (`matches_por_jogo` presente — ver `pipeline.py`): uma linha
    por perna, com o jogo dela em itálico antes do mercado. Sem isto, 3
    pernas "Total de Gols: Mais de 2,5" de 3 jogos diferentes apareciam
    idênticas no card, sem nada dizendo que eram jogos distintos (o
    "Festival de Gols" da Novibet, 2026-08-07).
    """
    mercado = resultado.get("mercado") or ""
    if not resultado.get("matches_por_jogo"):
        return [_esc(mercado)]

    linhas = []
    for perna in mercado.split(" + "):
        perna = perna.strip()
        m = _PREFIXO_JOGO_RE.match(perna)
        if m:
            jogo, resto = m.group(1).strip(), perna[m.end():].strip()
            linhas.append(f"<i>{_esc(jogo)}</i>\n{_esc(resto)}")
        else:
            linhas.append(_esc(perna))
    return linhas


def _linha_sofascore(resultado: dict) -> str | None:
    """Aviso do rebaixamento de confiança pelo SofaScore, se houve algum.

    `pipeline.py` grava `confianca_original` só quando `stats_sofascore`
    rebaixou a confiança um degrau — até 2026-08-07 isso nunca chegava ao
    Telegram (`stats_sofascore` inteiro ficava só no log), então quem lia o
    alerta não tinha como saber que o stake abaixo foi calculado com a
    confiança ANTIGA, não com a que o card mostra.
    """
    original = resultado.get("confianca_original")
    if not original:
        return None
    stats = resultado.get("stats_sofascore") or {}
    if stats.get("flag_noticia_fresca"):
        desfalques = stats.get("desfalque_recente") or []
        motivo = ("desfalque: " + "; ".join(desfalques)) if desfalques else "notícia fresca"
    else:
        motivo = "combo diverge do histórico"
    return (f"⚠️ <i>sofascore rebaixou a confiança de {_esc(original)} pra "
            f"{_esc(resultado.get('confianca'))} ({_esc(motivo)}) — a stake acima "
            f"foi calculada com a confiança ANTERIOR</i>")


def _avisos_de_suspeita(resultado: dict) -> list[str]:
    """Motivos para desconfiar do número, em linguagem de quem lê o alerta.

    A política é marcar, não suprimir: um alerta que some não dá chance de
    julgar. Cada aviso diz o que fazer, não só que algo está estranho.
    """
    avisos: list[str] = []

    if (resultado.get("fonte_odd") or "") == "consenso":
        avisos.append(
            "⚠️ sem preço da Pinnacle neste mercado — a justa é a mediana das "
            "outras casas, que podem estar erradas juntas"
        )
        if (resultado.get("n_precos_consenso") or 0) <= 1:
            # O caso mais comum e o mais enganoso: várias casas Altenar
            # publicando o MESMO preço. Não é concordância, é uma fonte só.
            avisos.append(
                "⚠️ todas as casas cotaram o mesmo preço — é um feed só, não "
                "opiniões independentes; trate como referência fraca"
            )

    edge = resultado.get("edge_pct")
    if edge is not None and edge >= vconfig.EDGE_SUSPEITO_PCT:
        avisos.append(
            f"⚠️ edge de {edge:+.0f}% é alto demais pra ser real — quase sempre "
            "é mercado casado errado; confira o mercado na casa antes"
        )

    return avisos


def truncar(texto: str, limite: int = LIMITE_TELEGRAM) -> str:
    """Corta no limite do Telegram, avisando que cortou.

    Função separada (e não inline no `enviar`) pra poder ser testada sem rede:
    passar do limite faz a API devolver 400 e o alerta some.
    """
    if len(texto) <= limite:
        return texto
    marca = "\n[…truncado]"
    return texto[: limite - len(marca)] + marca


class TelegramNotifier:
    """Envia mensagens pro chat configurado. Nunca levanta exceção."""

    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = (token if token is not None else config.TELEGRAM_BOT_TOKEN).strip()
        self.chat_id = (chat_id if chat_id is not None else config.TELEGRAM_CHAT_ID).strip()
        self._session: curl_requests.Session | None = None

    @property
    def ativo(self) -> bool:
        return bool(self.token and self.chat_id)

    def __enter__(self) -> "TelegramNotifier":
        if self.ativo:
            self._session = curl_requests.Session(timeout=config.REQUEST_TIMEOUT_SECONDS)
        return self

    def __exit__(self, *exc: object) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    # ------------------------------------------------------------------
    # Setup — só precisam do token, o chat_id ainda não existe aqui
    # ------------------------------------------------------------------

    def _chamar(self, metodo: str, **params: object) -> dict | None:
        """Chama um método da API do bot. None em qualquer falha."""
        if not self.token:
            return None
        sessao = self._session or curl_requests.Session(
            timeout=config.REQUEST_TIMEOUT_SECONDS)
        try:
            r = sessao.get(f"{config.TELEGRAM_API}/bot{self.token}/{metodo}",
                           params=params or None)
            dados = r.json()
            if not isinstance(dados, dict) or not dados.get("ok"):
                log.debug("%s falhou: %s", metodo, str(dados)[:200])
                return None
            return dados
        except (RequestException, ValueError) as exc:
            log.debug("%s falhou: %s", metodo, exc)
            return None
        finally:
            if self._session is None:
                sessao.close()

    def identidade(self) -> dict | None:
        """`getMe` — valida o token e devolve os dados do bot."""
        dados = self._chamar("getMe")
        return dados.get("result") if dados else None

    def descobrir_chat_id(self) -> str | None:
        """Extrai o chat_id da última mensagem recebida pelo bot.

        Evita a ida ao @userinfobot: basta o dono mandar qualquer coisa pro
        próprio bot. Pega a mensagem mais recente porque `getUpdates` devolve
        em ordem cronológica e o interessante é quem acabou de falar.
        """
        dados = self._chamar("getUpdates", limit=100, timeout=0)
        if not dados:
            return None
        for update in reversed(dados.get("result") or []):
            for chave in ("message", "edited_message", "channel_post"):
                chat = (update.get(chave) or {}).get("chat") or {}
                if chat.get("id") is not None:
                    return str(chat["id"])
        return None

    # ------------------------------------------------------------------

    def enviar(self, texto: str) -> bool:
        """Manda uma mensagem. Devolve se saiu — falha vira log, não exceção.

        Uma indisponibilidade do Telegram não pode interromper o scraping: o
        dado capturado vale mais que o aviso.
        """
        if not self.ativo:
            log.debug("telegram não configurado — mensagem descartada")
            return False

        texto = truncar(texto)

        sessao = self._session or curl_requests.Session(
            timeout=config.REQUEST_TIMEOUT_SECONDS)
        try:
            r = sessao.post(
                f"{config.TELEGRAM_API}/bot{self.token}/sendMessage",
                json={
                    "chat_id": self.chat_id,
                    "text": texto,
                    "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                },
            )
            if r.status_code != 200:
                # O corpo do erro do Telegram diz o motivo (chat_id errado,
                # token revogado); sem ele o diagnóstico fica no escuro.
                log.warning("telegram respondeu HTTP %s: %s", r.status_code, r.text[:200])
                return False
            return True
        except RequestException as exc:
            log.warning("falha de rede ao notificar: %s", exc)
            return False
        finally:
            if self._session is None:
                sessao.close()

    # ------------------------------------------------------------------
    # Formatação das mensagens
    # ------------------------------------------------------------------

    @staticmethod
    def formatar_value(resultado: dict) -> str:
        """Alerta de uma value bet — estilo card visual com seções e separadores."""
        sep = "━━━━━━━━━━━━━━━━━━━━━━"

        # --- Cabeçalho: esporte + evento ---
        evento = _esc(resultado.get("evento"))

        linhas = []
        # Banner no topo, antes do evento: o alerta apertado tem que se
        # anunciar na notificação do celular, não na terceira linha do card.
        faltam = minutos_ate_inicio(resultado)
        if (config.MIN_ANTECEDENCIA_ALERTA > 0 and faltam is not None
                and 0 < faltam < config.MIN_ANTECEDENCIA_ALERTA):
            linhas.append(f"⚡ <b>EM CIMA DA HORA — começa em {_duracao(faltam)}</b>")

        linhas += [
            f"⚽ <b>{evento}</b>",
            sep,
        ]
        inicio = _linha_inicio(resultado)
        if inicio:
            linhas += [inicio, ""]

        # --- THE BET: o mercado ---
        linhas += [
            "⚡ <b>THE BET</b>",
            *_formatar_the_bet(resultado),
            "",
        ]

        # --- BOOK: odds ---
        # A casa vem da oferta: com mais de uma casa vigiada, fixar o nome aqui
        # faria o alerta apontar a casa errada.
        casa = _esc(resultado.get("casa") or "Betano")
        linhas += [
            f"🏦 <b>BOOK: {casa}</b>",
            f"Odds: <b>{resultado['odd_boost']:.2f}</b> (justa: {resultado['odd_justa']:.2f})",
            "",
        ]

        # --- Edge ---
        edge = resultado["edge_pct"]
        barra_cheia = int(min(edge, 30) / 30 * 10)
        barra = "▓" * barra_cheia + "░" * (10 - barra_cheia)
        linhas += [
            f"📊 Edge: <b>{edge:+.1f}%</b>  {barra}",
            f"<i>mín. {resultado.get('threshold_usado', '?')}%</i>",
        ]

        # --- Stake: aposta sugerida ---
        if resultado.get("stake_descricao"):
            unidades = resultado.get("stake_unidades") or 0
            icone = "💰" if unidades >= 1 else "🪙"
            linhas.append(f"{icone} Apostar: <b>{_esc(resultado['stake_descricao'])}</b>")
            # Boost muito alto costuma vir com teto de aposta baixo. Sem este
            # aviso, "apostar 5un" seria uma recomendação inexecutável.
            limite = resultado.get("limite_aposta")
            # `boost_pct` só é preenchido pelas fontes de combo; nas demais o
            # ganho sai das próprias odds. Sem esse fallback o aviso valeria
            # só pra parte das ofertas.
            ganho = resultado.get("boost_pct")
            if ganho is None:
                orig, boost = resultado.get("odd_original"), resultado.get("odd_boost")
                if orig and boost:
                    ganho = (boost / orig - 1) * 100
            if limite:
                linhas.append(f"⚠️ <i>a casa limita esta aposta a {limite:,.2f}</i>"
                              .replace(",", "@").replace(".", ",").replace("@", "."))
            elif ganho and ganho >= config.BOOST_ALTO_AVISO_PCT:
                # A API não publica o teto, então o aviso sai do tamanho do
                # boost: ninguém turbina +800% sem limitar a aposta.
                linhas.append(f"⚠️ <i>boost de {ganho:+.0f}% — confira o limite "
                              f"de aposta no site antes</i>")

        linhas.append(sep)

        # --- Detalhes técnicos ---
        # A fonte é lida do resultado, não fixada: "pinnacle" fixo aqui fazia
        # alerta de consenso se anunciar como preço sharp, que é exatamente a
        # confusão que o resto do pipeline trabalha pra evitar.
        fonte = resultado.get("fonte_odd") or "pinnacle"
        if fonte == "consenso":
            n = resultado.get("n_casas_consenso") or 0
            precos = resultado.get("n_precos_consenso") or 0
            # Mostra os preços distintos junto das casas: as casas Altenar são
            # skins do mesmo feed (90% das seleções com preço idêntico), e
            # "3 casas" sozinho passaria por três opiniões independentes.
            fonte = (f"consenso de {n} casas, {precos} preço(s)" if n
                     else "consenso de casas")
        detalhes = [resultado.get("tipo_mercado") or "", f"de-vig · {fonte}"]
        if resultado.get("interpolada"):
            detalhes.append("⚠️ interpolada")
        linhas.append(f"<i>{_esc(' · '.join(d for d in detalhes if d))}</i>")

        # --- Selo de número suspeito ---
        # Não suprime: manda marcado. Edge dessa ordem quase sempre é erro de
        # casamento de mercado, mas quem decide é quem lê — e alerta que some
        # não dá chance de decidir nada.
        for aviso in _avisos_de_suspeita(resultado):
            linhas.append(f"<i>{_esc(aviso)}</i>")

        linha_sofascore = _linha_sofascore(resultado)
        if linha_sofascore:
            linhas.append(linha_sofascore)

        matches_por_jogo = resultado.get("matches_por_jogo")
        if matches_por_jogo:
            # Multi-jogo: um match por jogo, não só o da oferta — senão o
            # score de 100.0 do primeiro jogo passaria a impressão de que
            # TODAS as pernas casaram, quando é só a primeira.
            for m in matches_por_jogo:
                linhas.append(f"<i>🔗 {_esc(m['jogo'])} → {_esc(m['evento_pinnacle'])} "
                              f"({m['match_score']})</i>")
        elif resultado.get("evento_pinnacle"):
            linhas.append(f"<i>🔗 {_esc(resultado['evento_pinnacle'])} "
                          f"({resultado.get('match_score')})</i>")

        if resultado.get("url"):
            # O nome da casa vem da oferta, como no BOOK acima. Fixar "Betano"
            # aqui fazia o link de 10 das 11 casas dizer a casa errada.
            linhas.append(f"\n🌐 <a href=\"{_esc(resultado['url'])}\">Abrir na {casa}</a>")

        return "\n".join(linhas)

    @staticmethod
    def formatar_resumo(resumo: dict) -> str:
        """Heartbeat diário: prova que o bot está vivo mesmo sem value bet."""
        sep = "━━━━━━━━━━━━━━━━━━━━━━"
        values = resumo.get("values", 0)
        icone_values = "✅" if values > 0 else "⛔"

        linhas = [
            "📊 <b>RESUMO DO DIA</b>",
            sep,
            "",
            f"📋 Ofertas ativas: <b>{resumo.get('ativas', 0)}</b>",
            f"🔄 Ciclos (24h): <b>{resumo.get('ciclos', 0)}</b>"
            f"  ({resumo.get('ciclos_com_erro', 0)} com erro)",
            "",
            f"🆕 Novas: {resumo.get('novas', 0)}  ·  "
            f"✏️ Alteradas: {resumo.get('alteradas', 0)}  ·  "
            f"🗑 Expiradas: {resumo.get('expiradas', 0)}",
            "",
            sep,
            "",
            f"🔬 Avaliadas (Pinnacle): <b>{resumo.get('avaliadas', 0)}</b>",
            f"{icone_values} Value bets: <b>{values}</b>",
        ]
        if resumo.get("melhor_edge") is not None:
            edge = resumo["melhor_edge"]
            barra_cheia = int(max(min(edge, 30), 0) / 30 * 10)
            barra = "▓" * barra_cheia + "░" * (10 - barra_cheia)
            linhas.append(f"📈 Melhor edge: <b>{edge:+.1f}%</b>  {barra}")
        if resumo.get("sem_cobertura"):
            linhas.append(f"\n<i>⚠️ Sem ref. Pinnacle: "
                          f"{_esc(resumo['sem_cobertura'])}</i>")
        return "\n".join(linhas)

    # ------------------------------------------------------------------
    # Apostas do dia (liquidadas)
    # ------------------------------------------------------------------

    @staticmethod
    def formatar_apostas_do_dia(apostas: list[dict], *,
                                limite: int = LIMITE_TELEGRAM) -> list[str]:
        """As apostas cujo jogo fechou nas últimas 24h, com green/red e saldo.

        Devolve uma LISTA de mensagens, não uma string. Medido em dado real:
        ~40 apostas por dia × ~130 caracteres ≈ 5.200, contra o teto de 4.096 do
        Telegram. E `truncar()` não serve aqui — ele corta no caractere e pode
        partir uma tag `<b>` ao meio, o que faz a API recusar a mensagem
        INTEIRA com 400 (vira uma linha de log e o relatório some).

        O empacotamento é guloso por BLOCO inteiro, nunca por caractere: como
        todo corte cai entre blocos e cada bloco é HTML balanceado, toda
        mensagem sai balanceada por construção — não por sorte.

        Lista vazia quando não houve aposta liquidada (dia sem jogo encerrado,
        ou tudo ainda pendente): melhor não mandar nada que mandar uma seção
        vazia todo dia.
        """
        if not apostas:
            return []

        blocos = [TelegramNotifier._bloco_aposta(a) for a in apostas]
        rodape = TelegramNotifier._rodape_apostas(apostas)
        return _empacotar(blocos, cabecalho="📒 <b>APOSTAS DO DIA</b>",
                          rodape=rodape, limite=limite)

    @staticmethod
    def _bloco_aposta(aposta: dict) -> str:
        """Um bloco de 2 linhas, HTML já balanceado."""
        icone, texto_pl = _veredito_visual(aposta)
        mercado = _esc(_encurtar(aposta.get("mercado") or "?", MERCADO_MAX_CHARS))
        odd = aposta.get("odd_boost")
        odd_txt = f"odd <b>{odd:.2f}</b>" if isinstance(odd, (int, float)) else "odd ?"
        return (f"⚽ <b>{_esc(aposta.get('evento') or '?')}</b>\n"
                f"↳ {mercado} · {odd_txt} · {icone} {texto_pl}")

    @staticmethod
    def _rodape_apostas(apostas: list[dict]) -> str:
        contagem = {"green": 0, "red": 0, "desconhecido": 0, "void": 0}
        saldo = 0.0
        sem_stake: list[dict] = []
        for a in apostas:
            contagem[a.get("resultado", "desconhecido")] = contagem.get(
                a.get("resultado", "desconhecido"), 0) + 1
            unidades = _unidades(a)
            if unidades is None:
                if a.get("resultado") in ("green", "red"):
                    sem_stake.append(a)
            else:
                saldo += unidades
        linhas = [
            f"✅ {contagem['green']}  ❌ {contagem['red']}  "
            f"❔ {contagem['desconhecido']}  ➖ {contagem['void']}",
            f"💰 Saldo: <b>{saldo:+.2f}un</b>",
        ]
        if sem_stake:
            # Sem stake gravada não dá pra somar — mas listar por nome poupa
            # quem lê de rolar a mensagem toda procurando qual bloco é qual.
            linhas.append(f"<i>{len(sem_stake)} aposta(s) sem stake registrada "
                          f"— fora do saldo:</i>")
            for a in sem_stake:
                icone = "✅" if a.get("resultado") == "green" else "❌"
                linhas.append(
                    f"<i>{icone} {_esc(a.get('evento') or '?')} — "
                    f"{_esc(_encurtar(a.get('mercado') or '?', MERCADO_MAX_CHARS))}</i>")
        return "\n".join(linhas)

    @staticmethod
    def amostras_exemplo() -> list[tuple[str, str]]:
        """(nome, texto) de cada tipo de mensagem, com dados de exemplo.

        Mora aqui, e não num script solto, porque é o contrato com a API do
        Telegram: HTML desbalanceado faz a mensagem inteira ser recusada com
        400, e isso precisa quebrar nos testes, não em produção.
        """
        from .value import stake

        base = {
            "casa": "Betano", "evento": "Celtic - Dundee FC",
            "mercado": "Resultado Final: Celtic", "odd_boost": 1.21,
            "odd_justa": 1.12, "edge_pct": 8.04, "tipo_mercado": "simples",
            "fonte_odd": "pinnacle", "threshold_usado": 5.0,
            "evento_pinnacle": "Celtic vs Dundee FC", "match_score": 100.0,
            "url": "https://www.betano.bet.br/live/celtic-dundee-fc/",
        }

        def com_stake(o: dict) -> dict:
            s = stake.calcular(o["odd_boost"], o["odd_justa"],
                               tipo_mercado=o.get("tipo_mercado", "simples"),
                               interpolada=bool(o.get("interpolada")))
            return {**o, "stake_unidades": s.unidades, "stake_descricao": s.descrever()}

        fmt = TelegramNotifier
        return [
            ("value-simples", fmt.formatar_value(com_stake(base))),
            ("value-combo-interpolada", fmt.formatar_value(com_stake({
                **base, "evento": "Velez Sarsfield - CA Independiente",
                "mercado": "Total de Gols Mais de 1.5 + Escanteios Mais de 8.5",
                "odd_boost": 7.26, "odd_justa": 5.90, "edge_pct": 23.05,
                "tipo_mercado": "combo", "threshold_usado": 18.0, "interpolada": True,
            }))),
            # & e < provam o escape: sem ele o Telegram recusa a mensagem toda.
            ("value-com-caractere-especial", fmt.formatar_value(com_stake({
                **base, "evento": "Brøndby & <Co> - Preußen Münster",
                "mercado": "Resultado Final: Brøndby & <Co>",
            }))),
            ("value-outra-casa-com-limite", fmt.formatar_value(com_stake({
                **base, "casa": "Esportiva Bet",
                "evento": "Athletico-PR - Vitória",
                "mercado": "Vencedor do encontro: Vitória",
                "odd_boost": 50.0, "odd_justa": 12.0, "edge_pct": 316.7,
                "limite_aposta": 3330.0,
                "url": "https://esportiva.bet.br/sports/event/16630676",
            }))),
            ("resumo-diario", fmt.formatar_resumo({
                "ativas": 198, "ciclos": 27, "ciclos_com_erro": 0, "novas": 41,
                "alteradas": 63, "expiradas": 38, "avaliadas": 74, "values": 1,
                "melhor_edge": 9.09, "sem_cobertura": 96,
            })),
            ("alerta-de-quebra", fmt.formatar_quebra(3)),
            *_amostras_apostas_do_dia(fmt),
        ]

    @staticmethod
    def formatar_quebra(ciclos: int) -> str:
        sep = "━━━━━━━━━━━━━━━━━━━━━━"
        return (
            f"🚨 <b>POSSÍVEL QUEBRA DO SCRAPER</b>\n"
            f"{sep}\n\n"
            f"⚠️ <b>{ciclos}</b> ciclos seguidos sem capturar nada.\n\n"
            f"A Betano provavelmente mudou a estrutura da API.\n"
            f"Revise <code>scraper.py</code> e reinicie.\n\n"
            f"{sep}\n"
            f"<i>🔧 Ação necessária</i>"
        )
