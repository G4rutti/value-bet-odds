"""Ponte entre o ciclo de scraping e o Telegram.

Fluxo de um ciclo:

    diff (novas + alteradas) -> avaliar contra a Pinnacle -> alertar as value

Só as ofertas **novas ou com odd alterada** são avaliadas. Reavaliar as ~195
ativas a cada 3 minutos gastaria centenas de requests na Pinnacle pra recalcular
o mesmo edge — e o WAF dela responde a volume, igual ao da Betano.

O resumo diário existe porque o alerta de value é raro por natureza (0 em 241
ofertas na validação). Sem heartbeat não dá pra distinguir "nada bom apareceu"
de "o processo morreu de madrugada".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime, timedelta, timezone

from typing import Awaitable, Callable

from . import config
from .models import calcular_alert_hash, minutos_ate_inicio
from .notifier import TelegramNotifier
from .revalidacao import Revalidacao
from .storage import Storage
from .value import config as vconfig
from .value import liquidacao
from .value.pipeline import avaliar_ofertas
from .value.stats_check import SofaScoreClient, SofaScoreError

log = logging.getLogger("betano.alerts")

CHAVE_RESUMO = "resumo_diario_enviado_em"
CHAVE_ACUMULADO = "acumulado_do_dia"


class Alertador:
    """Avalia o que mudou no ciclo e decide o que vai pro Telegram."""

    def __init__(self, storage: Storage, notifier: TelegramNotifier,
                 revalidador: Callable[[dict], Awaitable[Revalidacao]] | None = None) -> None:
        self.storage = storage
        self.notifier = notifier
        # Injetado pra que o teste não precise de rede. `None` desliga a
        # revalidação — o alerta sai com o dado do banco, como antes.
        self.revalidador = revalidador

    # ------------------------------------------------------------------

    async def processar_ciclo(self) -> dict:
        """Avalia o próximo lote da fila e dispara os alertas.

        Não recebe o diff: quem define o trabalho é a tabela `avaliacoes`.
        Oferta nova e oferta com odd reajustada entram sozinhas na fila (o
        `content_hash` delas ainda não foi avaliado), e o que não coube no teto
        do ciclo anterior continua lá. Uma rota só, sem backlog órfão.

        Só a avaliação (síncrona, faz rede na Pinnacle) sai pra uma thread. O
        acesso ao SQLite fica todo aqui: a conexão é presa à thread que a criou,
        e usá-la de fora levanta `ProgrammingError`.
        """
        vazio = {"avaliadas": 0, "values": 0, "melhor_edge": None, "sem_cobertura": 0}

        ttl = config.REAVALIAR_APOS_MINUTOS
        lote = self.storage.pendentes_de_avaliacao(config.MAX_AVALIACOES_POR_CICLO, ttl)
        if not lote:
            return vazio

        total = self.storage.contar_pendentes(ttl)
        if total > len(lote):
            log.info(
                "fila de avaliação: %d pendentes, avaliando %d neste ciclo "
                "(as de jogo mais próximo); as outras %d entram nos próximos",
                total, len(lote), total - len(lote),
            )
        else:
            log.info("fila de avaliação: avaliando %d oferta(s)", len(lote))

        try:
            resultados = await asyncio.to_thread(avaliar_ofertas, lote,
                                                 self.storage)
        except Exception as exc:  # noqa: BLE001 — avaliação não pode matar o loop
            log.exception("avaliação de value falhou neste ciclo: %s", exc)
            return vazio

        # Marca ANTES de alertar: se o envio falhar, a oferta não volta pra fila
        # eternamente — a dedup de alerta já cuida de não perder a notificação.
        for r in resultados:
            if r.get("offer_id") and r.get("content_hash"):
                self.storage.registrar_avaliacao(
                    r["offer_id"], r["content_hash"], r.get("status"))
                # Grava o veredito da curadoria estruturada AQUI, não só na
                # hora de alertar: em modo sombra (CURADORIA_ENFORCE=0) a
                # oferta segue seu caminho normal e pode nem virar alerta —
                # mas é justo essa amostra (vetado/degrau que FOI enviado
                # mesmo assim) que `query.py --curadoria` precisa pra
                # comparar contra `liquidacoes` e decidir se o veto presta.
                veredito = r.get("veredito_curadoria")
                if veredito is not None:
                    self.storage.registrar_veredito_curadoria(
                        r["offer_id"], r["content_hash"], veredito)

        return await self._consolidar(resultados, len(lote))

    async def _consolidar(self, resultados: list[dict], n_candidatas: int) -> dict:
        """Apura os números, dispara os alertas e devolve o resumo do lote."""
        avaliadas = [r for r in resultados if r.get("status") == "avaliada"]
        values = [r for r in avaliadas if r.get("is_value")]
        edges = [r["edge_pct"] for r in avaliadas if r.get("edge_pct") is not None]

        log.info(
            "value: %d avaliada(s) de %d candidata(s), %d value bet(s)",
            len(avaliadas), n_candidatas, len(values),
        )

        enviadas = await self._alertar_values(values)

        return {
            "avaliadas": len(avaliadas),
            # Alertas efetivamente enviados, não avaliações que bateram o
            # threshold: a mesma oferta reavaliada a cada oscilação de odd
            # inflava `values` muito além do que o usuário via no Telegram
            # (dedup, MAX_ALERTAS_POR_CICLO e revalidação cortam depois).
            "values": enviadas,
            "melhor_edge": max(edges) if edges else None,
            "sem_cobertura": sum(1 for r in resultados
                                 if r.get("status") == "sem_odd_justa"),
        }

    @staticmethod
    def _velha_demais(resultado: dict) -> bool:
        """Captura antiga demais pra virar alerta sem confirmação.

        Só entra em cena quando a revalidação foi INDETERMINADA — confirmada
        passa por mais velha que seja, e bloqueada já saiu antes. É a rede pro
        caso "casa sem revalidação + rede caiu".
        """
        limite = config.IDADE_MAX_PARA_ALERTA
        if limite <= 0:
            return False
        visto = resultado.get("last_seen") or resultado.get("capturado_em")
        if not visto:
            return False
        try:
            quando = datetime.fromisoformat(str(visto))
        except ValueError:
            return False
        if quando.tzinfo is None:
            quando = quando.astimezone()
        idade = datetime.now(timezone.utc) - quando.astimezone(timezone.utc)
        return idade > timedelta(minutes=limite)

    @staticmethod
    def _selecionar_diversificado(values: list[dict]) -> list[dict]:
        """Top edge, mas sem deixar uma família de mercado engolir o ciclo.

        Percorre `values` já ordenado por edge decrescente e vai aceitando
        até `MAX_ALERTAS_POR_CICLO`, pulando (não descartando pra sempre,
        só não escolhendo agora) qualquer item cuja família já bateu
        `MAX_ALERTAS_POR_FAMILIA_POR_CICLO` ENTRE OS JÁ SELECIONADOS. Sem
        isto, vários handicaps (ou qualquer família só) de edge alto no
        mesmo ciclo enchiam o teto sozinhos e outra família de edge menor
        nunca aparecia, mesmo em jogos sem nenhuma correlação entre si.
        """
        selecionados: list[dict] = []
        por_familia: dict[str, int] = {}
        teto_familia = config.MAX_ALERTAS_POR_FAMILIA_POR_CICLO
        for resultado in sorted(values, key=lambda r: r["edge_pct"], reverse=True):
            if len(selecionados) >= config.MAX_ALERTAS_POR_CICLO:
                break
            familia = resultado.get("familia_mercado") or "outros"
            if teto_familia > 0 and por_familia.get(familia, 0) >= teto_familia:
                continue
            selecionados.append(resultado)
            por_familia[familia] = por_familia.get(familia, 0) + 1
        return selecionados

    async def _alertar_values(self, values: list[dict]) -> int:
        """Manda os alertas que sobrarem do funil de filtros. Devolve quantos saíram."""
        enviados = 0
        for resultado in self._selecionar_diversificado(values):
            offer_id = resultado.get("offer_id")
            content_hash = resultado.get("content_hash")
            if not offer_id or not content_hash:
                continue
            # A dedup é pelo que o usuário LÊ, não pelo que mudou na oferta —
            # `odd_original` oscilava sozinha e reenviava alerta idêntico.
            odd_boost = resultado.get("odd_boost")
            edge_pct = resultado.get("edge_pct")
            alert_hash = calcular_alert_hash(offer_id)
            # `_candidatas` já filtra, mas a garantia de não repetir mensagem
            # tem que morar aqui: é o único ponto por onde todo envio passa.
            if self.storage.ja_alertou(offer_id, edge_pct,
                                       config.INTERVALO_MIN_REALERTA,
                                       config.REALERTA_MELHORA_MIN_PP,
                                       config.MAX_REALERTAS_POR_OFERTA):
                continue

            # Jogo em andamento não vira alerta, ponto. O diff e a fila já
            # barram antes, mas entre a avaliação e este envio houve rede
            # (Pinnacle + revalidação) e o apito pode ter saído no meio — que é
            # exatamente como saíram alertas com o jogo 15 min adiantado.
            faltam = minutos_ate_inicio(resultado)
            if faltam is not None and faltam <= 0:
                log.info("alerta descartado (jogo já começou há %.0f min): %s — %s",
                         -faltam, resultado.get("evento"), resultado.get("mercado"))
                continue

            # Último passo antes de mandar: a odd ainda existe? O rodízio de
            # casas deixa a captura envelhecer até ~1h, e alertar preço morto
            # é pior que não alertar. Ver `revalidacao`.
            if self.revalidador is not None:
                rev = await self.revalidador(resultado)
                if rev.bloqueia:
                    log.info("alerta descartado (%s): %s — %s [%s]",
                             rev.status, resultado.get("evento"),
                             resultado.get("mercado"), rev.motivo)
                    # Não devolve pra fila de propósito. `avaliacoes` é chaveada
                    # por `content_hash`: quando a casa for reraspada e a odd
                    # mudar, a oferta volta a ficar pendente sozinha. Forçar o
                    # retorno agora só repetiria avaliação + revalidação a cada
                    # ciclo, contra a MESMA odd velha, até a casa girar (~1h).
                    continue
                if rev.status == "indeterminada" and self._velha_demais(resultado):
                    # Não deu pra confirmar E a captura já está velha: é
                    # exatamente a combinação que produziu o alerta falso.
                    log.info("alerta descartado (não confirmado, captura de %s): %s",
                             resultado.get("last_seen"), resultado.get("evento"))
                    continue

            texto = self.notifier.formatar_value(resultado)
            enviado = self.notifier.enviar(texto)
            # Registra mesmo sem Telegram configurado: o console já mostrou o
            # alerta, e sem isso a mesma oferta reapareceria todo ciclo no log.
            if enviado or not self.notifier.ativo:
                # A stake vai gravada porque o P&L do resumo diário usa o que
                # FOI recomendado, nunca um valor recalculado depois: Kelly,
                # thresholds e a tabela de confiança mudam, e o relatório é
                # registro do que o bot mandou fazer naquele momento.
                stake = resultado.get("stake_unidades")
                self.storage.registrar_alerta(
                    offer_id, content_hash, alert_hash,
                    resultado.get("edge_pct"), odd_boost,
                    stake_unidades=stake,
                    stake_apostavel=(None if stake is None
                                     else stake >= vconfig.STAKE_MIN_UNIDADES),
                    odd_justa=resultado.get("odd_justa"),
                    confianca=resultado.get("confianca"),
                )
                enviados += 1
            log.info("🔥 VALUE %+.1f%%: %s — %s", resultado["edge_pct"],
                     resultado.get("evento"), resultado.get("mercado"))
        return enviados

    # ------------------------------------------------------------------
    # Resumo diário
    # ------------------------------------------------------------------

    def acumular(self, ciclo: dict) -> None:
        """Soma os números do ciclo no acumulado do dia (sobrevive a restart)."""
        hoje = date.today().isoformat()
        bruto = self.storage.get_estado(CHAVE_ACUMULADO)
        acc = {}
        if bruto:
            try:
                acc = json.loads(bruto)
            except ValueError:
                acc = {}
        if acc.get("dia") != hoje:
            acc = {"dia": hoje, "avaliadas": 0, "values": 0, "melhor_edge": None}

        acc["avaliadas"] += ciclo.get("avaliadas", 0)
        acc["values"] += ciclo.get("values", 0)
        edge = ciclo.get("melhor_edge")
        if edge is not None and (acc["melhor_edge"] is None or edge > acc["melhor_edge"]):
            acc["melhor_edge"] = edge
        self.storage.set_estado(CHAVE_ACUMULADO, json.dumps(acc))

    def _dia_de_referencia(self, agora: datetime) -> str | None:
        """Qual dia este instante fecha — ou None se não é hora de resumo.

        O relatório é atribuído ao dia que ele FECHA, não ao dia do relógio.
        A distinção só importa quando o alvo está perto da meia-noite (ver
        `config.RESUMO_TOLERANCIA_MINUTOS`): às 00:03 com alvo 23:59, o resumo
        pendente é o de ONTEM, e gravá-lo como "hoje" faria o relatório de hoje
        ser pulado à noite — um dia sim, um dia não.
        """
        alvo_hoje = agora.replace(hour=config.RESUMO_DIARIO_HORA,
                                  minute=config.RESUMO_DIARIO_MINUTO,
                                  second=0, microsecond=0)
        if agora >= alvo_hoje:
            return agora.date().isoformat()

        # Ainda não deu a hora hoje. Pode ser o resumo de ontem atrasado —
        # mas só dentro da tolerância, senão qualquer start no meio do dia
        # dispararia o resumo do dia anterior.
        alvo_ontem = alvo_hoje - timedelta(days=1)
        if agora - alvo_ontem <= timedelta(minutes=config.RESUMO_TOLERANCIA_MINUTOS):
            return alvo_ontem.date().isoformat()
        return None

    def talvez_resumo_diario(self, agora: datetime | None = None) -> bool:
        """Manda o heartbeat se já passou da hora e o dia ainda não foi coberto."""
        agora = agora or datetime.now()
        dia = self._dia_de_referencia(agora)
        if dia is None:
            return False
        if self.storage.get_estado(CHAVE_RESUMO) == dia:
            return False

        resumo = dict(self.storage.resumo_24h())
        bruto = self.storage.get_estado(CHAVE_ACUMULADO)
        if bruto:
            try:
                acc = json.loads(bruto)
                # Compara com o dia de REFERÊNCIA: o acumulado foi somado
                # durante o dia que fechou, então às 00:03 ele traz "ontem" —
                # comparar com a data do relógio perderia avaliadas/values.
                if acc.get("dia") == dia:
                    resumo.update({k: acc[k] for k in
                                   ("avaliadas", "values", "melhor_edge") if k in acc})
            except ValueError:
                pass

        self.notifier.enviar(self.notifier.formatar_resumo(resumo))

        # As apostas do dia vão DEPOIS e dentro de try: o heartbeat é o sinal
        # de "estou vivo" e não pode depender de uma seção acessória. Nenhuma
        # rede acontece aqui — `apostas_liquidadas_24h` só lê o banco, e quem
        # falou com o SofaScore foi `liquidar_pendentes`, ciclos atrás.
        try:
            apostas = self.storage.apostas_liquidadas_24h(agora)
            for parte in self.notifier.formatar_apostas_do_dia(apostas):
                self.notifier.enviar(parte)
            if apostas:
                log.info("apostas do dia: %d liquidada(s) reportada(s)", len(apostas))
        except Exception:  # noqa: BLE001 — o heartbeat já saiu, não derruba o ciclo
            log.exception("apostas do dia falharam — o resumo já foi enviado")

        # Marca como enviado mesmo se o Telegram falhou: reenviar em loop a cada
        # ciclo até a rede voltar seria pior que perder um resumo.
        self.storage.set_estado(CHAVE_RESUMO, dia)
        log.info("resumo diário (%s): %s", dia, resumo)
        return True

    # ------------------------------------------------------------------
    # Liquidação
    # ------------------------------------------------------------------

    async def liquidar_pendentes(self) -> dict:
        """Resolve green/red de apostas cujo jogo já acabou.

        Roda um punhado por ciclo (`LIQUIDACAO_MAX_POR_CICLO`) e persiste cada
        veredito na hora. É o que permite o relatório das 23:59 ser uma leitura
        pura de banco: um lote de rede naquele horário seria ~300s de bloqueio
        no exato momento em que falhar é irrecuperável, porque `CHAVE_RESUMO` é
        gravado mesmo quando o envio falha.

        Nunca levanta: uma falha aqui atrasa uma linha do relatório, não
        derruba o ciclo.
        """
        vazio = {"liquidadas": 0, "tentadas": 0}
        try:
            pendentes = self.storage.pendentes_de_liquidacao(
                config.LIQUIDACAO_MAX_POR_CICLO * 4, config.LIQUIDACAO_ATRASO_MIN)
        except Exception:  # noqa: BLE001
            log.exception("liquidação: não consegui listar pendentes")
            return vazio
        if not pendentes:
            return vazio

        try:
            em_backoff = self.storage.eventos_em_backoff()
        except Exception:  # noqa: BLE001
            em_backoff = set()

        # Agrupa por PARTIDA: as casas Altenar publicam o mesmo jogo, e uma
        # busca por casa gastaria o orçamento N vezes pelo mesmo resultado.
        por_partida: dict[str, list[dict]] = {}
        for row in pendentes:
            chave = liquidacao.chave_partida(row["evento"], row["inicio_evento"])
            if chave is None or chave in em_backoff:
                continue
            por_partida.setdefault(chave, []).append(row)

        alvos = list(por_partida.items())[:config.LIQUIDACAO_MAX_POR_CICLO]
        if not alvos:
            return vazio

        return await asyncio.to_thread(self._liquidar_lote, alvos)

    def _liquidar_lote(self, alvos: list[tuple[str, list[dict]]]) -> dict:
        """Parte síncrona: fala com o SofaScore e grava. Roda fora do loop."""
        liquidadas = tentadas = 0
        with SofaScoreClient() as cli:
            for chave, ofertas in alvos:
                tentadas += 1
                referencia = ofertas[0]
                try:
                    liquidadas += self._liquidar_partida(cli, chave, referencia,
                                                         ofertas)
                except SofaScoreError as exc:
                    log.warning("liquidação: %s falhou: %s", referencia["evento"], exc)
                except Exception:  # noqa: BLE001
                    log.exception("liquidação: erro inesperado em %s",
                                  referencia["evento"])
        if tentadas:
            log.info("liquidação: %d partida(s) consultada(s), %d aposta(s) resolvida(s)",
                     tentadas, liquidadas)
        return {"liquidadas": liquidadas, "tentadas": tentadas}

    def _liquidar_partida(self, cli: SofaScoreClient, chave: str,
                          referencia: dict, ofertas: list[dict]) -> int:
        evento, inicio = referencia["evento"], referencia["inicio_evento"]
        fim = liquidacao.fim_estimado(inicio)
        if fim is None:
            return 0

        estado = self.storage.evento_liquidacao(chave) or {}
        sofascore_id = estado.get("sofascore_id")
        if sofascore_id is None:
            sofascore_id, score = liquidacao.encontrar_partida(cli, evento, inicio)
            if sofascore_id is None:
                tentativas = self.storage.registrar_tentativa_evento(
                    chave, evento, inicio_evento=inicio,
                    motivo="sem match no SofaScore",
                    backoff_minutos=self._backoff(estado))
                if tentativas >= config.LIQUIDACAO_MAX_TENTATIVAS:
                    log.info("liquidação: desisti de %r após %d tentativas",
                             evento, tentativas)
                return 0
            self.storage.registrar_tentativa_evento(
                chave, evento, inicio_evento=inicio, sofascore_id=sofascore_id,
                match_score=score, motivo=None, backoff_minutos=0)

        partida = liquidacao.carregar_resultado(cli, sofascore_id)
        if partida is None:
            self.storage.registrar_tentativa_evento(
                chave, evento, inicio_evento=inicio, sofascore_id=sofascore_id,
                motivo="resultado indisponível",
                backoff_minutos=self._backoff(estado))
            return 0

        if not partida.encerrada:
            # Ainda rolando ou adiada. Não é derrota — só não é hora.
            self.storage.registrar_tentativa_evento(
                chave, evento, inicio_evento=inicio, sofascore_id=sofascore_id,
                motivo=f"status {partida.status}",
                backoff_minutos=self._backoff(estado))
            return 0

        self.storage.registrar_partida(
            sofascore_id, status=partida.status, home_nome=partida.home_nome,
            away_nome=partida.away_nome,
            gols=partida.placar(liquidacao.PERIODO_JOGO) or (None, None),
            ht=partida.placar(liquidacao.PERIODO_1T) or (None, None),
            estatisticas_json=liquidacao.serializar_estatisticas(partida),
            fim_partida=fim)

        n = 0
        for oferta in ofertas:
            veredito = liquidacao.liquidar_aposta(
                oferta["mercado"], partida,
                red_com_perna_indefinida=config.LIQUIDACAO_RED_COM_PERNA_INDEFINIDA)
            self.storage.registrar_liquidacao(
                oferta["offer_id"], resultado=veredito.resultado,
                motivo=veredito.motivo, fim_partida=fim,
                sofascore_id=sofascore_id)
            if veredito.resolvida:
                n += 1
        return n

    @staticmethod
    def _backoff(estado: dict) -> int:
        """Espera exponencial: 30min, 1h, 2h, 4h… com teto de 8h.

        Sem isso, uma partida que nunca casa (amistoso obscuro, nome que o
        matcher não resolve) volta pra fila a cada ciclo pra sempre.
        """
        tentativas = int(estado.get("tentativas") or 0)
        return min(30 * (2 ** tentativas), 480)

    def alertar_quebra(self, ciclos: int) -> None:
        if config.ALERTAR_QUEBRA:
            self.notifier.enviar(self.notifier.formatar_quebra(ciclos))
