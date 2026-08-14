"""Pipeline fim a fim: oferta do scraper -> oferta enriquecida com value.

    oferta -> match do evento na Pinnacle -> parse do mercado ->
    de-vig -> odd justa -> edge -> is_value
"""

from __future__ import annotations

import logging
import os
from collections import Counter
from typing import Any, Iterable

from . import config, curadoria, stake
from .consenso import ProvedorConsenso
from .fair_odds import calcular_odd_justa
from .market_parser import parse_mercado, tipo_mercado
from .matcher import encontrar_evento
from .models import Matchup
from .pinnacle import PinnacleScraper
from .stats_check import checar_stats
from .value_calc import avaliar_value, classe_mercado_da_oferta

log = logging.getLogger(__name__)

# Confiança mínima pra valer a pena gastar request no SofaScore — abaixo disto
# a oferta já "morreu" no filtro de edge/confiança e checar o histórico seria
# desperdício puro (dono: `sofascore-stats`; ver a tabela de confiança na
# skill `value-bet-methodology`, dono `market-confidence`).
_CONFIANCA_ELEGIVEL_STATS = frozenset({"alta", "média-alta", "média"})

# Um degrau abaixo, na mesma escala de `value_calc.classificar_confianca`.
# Vive aqui (não em value_calc.py, que não é meu) porque é só usado por este
# rebaixamento pós-hoc — a stack normal de confiança nunca desce por si só.
_UM_DEGRAU_ABAIXO = {"alta": "média-alta", "média-alta": "média", "média": "baixa"}

# Nasce DESLIGADA, mesmo padrão de `MODELO_ALERTA_ATIVO` (value/config.py):
# camada nova, sem calibração de campo ainda (o `LIMIAR_DIVERGENCIA` de
# `stats_check.py` é um chute razoável, não um número medido contra histórico
# de alertas real). Ligada, cada `avaliar_oferta` com confiança elegível bate
# na rede — inclusive quando chamada direto de teste (`test_value.py`,
# `test_alertas.py`, donos de `market-confidence`), que não injeta um cliente
# fake pro SofaScore. Desligada por padrão, nenhum desses testes é afetado.
STATS_SOFASCORE_ATIVO = os.getenv("STATS_SOFASCORE_ATIVO", "0") not in ("0", "false", "False")


def avaliar_oferta(oferta: dict, matchups: list[Matchup],
                   scraper: PinnacleScraper | None = None,
                   storage=None) -> dict:
    """Enriquece uma oferta com odd justa, edge e is_value.

    Nunca levanta exceção por dado ausente: quando não dá pra avaliar, devolve
    `status` explicando o motivo e deixa os campos de value como None. Chutar
    um número aqui seria pior do que admitir que não deu.
    """
    saida = {
        **oferta,
        "odd_justa": None,
        "edge_pct": None,
        "is_value": None,
        "tipo_mercado": None,
        "fonte_odd": None,
        "status": None,
        "match_score": None,
        "evento_pinnacle": None,
    }

    legs = parse_mercado(oferta.get("mercado", ""))
    saida["tipo_mercado"] = tipo_mercado(legs)

    # Consenso entre casas: a única referência possível pros props que a
    # Pinnacle não publica (chutes ao gol, cartões, artilheiro). Precisa do
    # `evento_id` e da casa — a casa sai da própria conta, senão a oferta
    # ajudaria a formar a referência contra a qual é medida.
    consenso = None
    if storage is not None and oferta.get("evento_id"):
        consenso = ProvedorConsenso(storage, str(oferta["evento_id"]),
                                    oferta.get("casa"),
                                    evento=oferta.get("evento"),
                                    inicio_evento=oferta.get("inicio_evento"))

    # 0. perna sem referência mata a oferta independente de casar o evento —
    # e sai antes de qualquer request. Metade das super odds da Betano tem
    # perna exótica (cartões, aces), e reavaliá-las de hora em hora pagando
    # match + carga de mercados seria desperdício puro de request.
    #
    # Com consenso disponível a perna exótica deixa de ser sentença: ela ainda
    # não tem preço na Pinnacle, mas pode ter nas outras casas. O corte seco só
    # vale quando não há nem essa saída.
    sem_cobertura = [l for l in legs if not l.suportado]
    if sem_cobertura and consenso is None:
        motivos = ", ".join(sorted({l.motivo or "?" for l in sem_cobertura}))
        saida["fonte_odd"] = "pinnacle"
        saida["status"] = "sem_odd_justa"
        saida["motivo"] = f"sem cobertura: {motivos}"
        saida["motivo_classe"] = "sem consenso disponível"
        return saida

    # `inicio_evento` na frente: a janela de ±18h do matcher compara com o
    # `commence_time` da Pinnacle, que é o kickoff. `valido_ate` podia ser
    # o fim da promoção, e fica de fallback só pelas ofertas gravadas
    # antes de a coluna existir. É a mesma referência de data usada pra
    # casar o jogo de CADA perna, abaixo — o payload não expõe kickoff por
    # perna (ver `novibet.py`), só o da oferta inteira.
    data_ref = (oferta.get("inicio_evento") or oferta.get("valido_ate")
                or oferta.get("capturado_em"))

    # 1. casar o evento "principal" da oferta — o único jogo, ou o do combo
    # numa oferta de jogo só. Continua sendo o que aparece no cabeçalho do
    # card e no `evento_pinnacle` de topo.
    match = encontrar_evento(oferta.get("evento", ""), data_ref, matchups)
    if match is None:
        saida["status"] = "sem_match_evento"
        return saida

    saida["match_score"] = round(match.score, 1)
    saida["evento_pinnacle"] = match.matchup.display_name
    matchup = match.matchup

    # 1b. combo multi-jogo: cada perna com `evento_texto` (ver
    # `novibet.py` + `market_parser.parse_mercado`) precisa casar com o SEU
    # jogo — não com o da oferta. Sem isto, todas as pernas de um combo
    # tipo "Festival de Gols" (Novibet, 2026-08-07: 3 pernas "mais de 2,5"
    # em 3 jogos diferentes) seriam precificadas contra o mesmo matchup, e a
    # probabilidade de um jogo só entraria multiplicada 3x — o edge fantasma
    # medido naquele caso. Se QUALQUER jogo do combo não casar, a oferta
    # inteira cai: combo meio-casado é exatamente o cenário que fabrica edge
    # falso, e alertar com 2 de 3 pernas precificadas certo esconderia isso.
    jogos_distintos = sorted({l.evento_texto for l in legs if l.evento_texto})
    matchups_por_jogo: dict[str, Matchup] = {}
    if jogos_distintos:
        matches_por_jogo = []
        for jogo_texto in jogos_distintos:
            m = encontrar_evento(jogo_texto, data_ref, matchups)
            if m is None:
                saida["status"] = "sem_match_evento"
                saida["motivo"] = f"perna de jogo não casado: {jogo_texto}"
                return saida
            matchups_por_jogo[jogo_texto] = m.matchup
            matches_por_jogo.append({
                "jogo": jogo_texto,
                "evento_pinnacle": m.matchup.display_name,
                "match_score": round(m.score, 1),
            })
        saida["matches_por_jogo"] = matches_por_jogo

    # 2. carregar os mercados de cada jogo que casou — o da oferta e os das
    # pernas multi-jogo, sem duplicar request pro mesmo matchup (dedup por
    # `id` da Pinnacle, já que `Matchup` não é hashable).
    vistos: dict[int, Matchup] = {matchup.id: matchup}
    for mu in matchups_por_jogo.values():
        vistos.setdefault(mu.id, mu)
    for mu in vistos.values():
        if not mu.markets and scraper is not None:
            scraper.carregar_mercados(mu)

    # 3. odd justa
    fair = calcular_odd_justa(legs, matchup, consenso=consenso,
                              matchups_por_jogo=matchups_por_jogo or None)
    saida["fonte_odd"] = fair.fonte_odd
    if not fair.ok:
        saida["status"] = "sem_odd_justa"
        saida["motivo"] = fair.motivo_falha
        # `motivo` já diz QUAL perna morreu; `motivo_classe` diz ONDE, que é o
        # que se agrega no log do ciclo. Perna que a Pinnacle deveria cobrir e
        # não cobriu é um problema diferente de perna exótica sem consenso, e
        # o balde único "N sem cobertura" tratava as duas como a mesma coisa.
        motivo = fair.motivo_falha or ""
        if motivo.startswith("sem odd na Pinnacle"):
            saida["motivo_classe"] = "sem mercado na Pinnacle"
        else:
            saida["motivo_classe"] = (
                (consenso.diagnostico() if consenso is not None else None)
                or "sem consenso disponível")
        return saida

    # 4. edge — o threshold depende de a justa ser preço observado ou
    # derivada, e (dentro do consenso) da classe de mercado: cartão, chute ao
    # gol, artilheiro (gol de jogador) e handicap pedem mais edge e menos
    # confiança que o consenso genérico (skill `value-bet-methodology`). A
    # classe é da OFERTA inteira — a perna pior manda, igual à fonte.
    odd_boost = float(oferta["odd_boost"])
    classe_mercado = classe_mercado_da_oferta(legs)
    value = avaliar_value(odd_boost, fair.odd_justa, fair.tipo_mercado,
                          fonte_odd=fair.fonte_odd,
                          classe_mercado=classe_mercado,
                          n_casas_consenso=fair.n_casas_consenso,
                          n_precos_consenso=fair.n_precos_consenso)

    # 5. quanto apostar. `confianca`/`flag` decidem a fração de Kelly (ou
    # zeram o stake automático, sem sugestão nenhuma) — ver value_calc.py.
    aposta = stake.calcular(odd_boost, value.odd_justa,
                            tipo_mercado=value.tipo_mercado,
                            interpolada=fair.interpolada,
                            derivada=fair.derivada,
                            consenso=fair.por_consenso,
                            confianca=value.confianca,
                            flag=value.flag)

    is_value = value.is_value

    # Zebra demais não vira alerta. O corte é na JUSTA, não na ofertada: odd 7.00
    # com justa 4.80 passa, justa 8.29 com ofertada 11.00 não. Ver value/config.py.
    if (is_value and value.tipo_mercado == "simples"
            and config.ODD_JUSTA_MAX > 0
            and value.odd_justa > config.ODD_JUSTA_MAX):
        log.info("value fora da faixa (justa %.2f > ODD_JUSTA_MAX %.2f, "
                 "não alerta): %s — %s — edge %+.1f%%",
                 value.odd_justa, config.ODD_JUSTA_MAX,
                 oferta.get("evento"), oferta.get("mercado"), value.edge_pct)
        is_value = False

    if is_value and fair.derivada and not config.MODELO_ALERTA_ATIVO:
        # A trava: com o modelo ainda não calibrado, a oferta é avaliada,
        # registrada e logada — mas não vira alerta. Ver value/config.py.
        log.info("value só de modelo (não alerta, MODELO_ALERTA_ATIVO=0): "
                 "%s — %s — edge %+.1f%%",
                 oferta.get("evento"), oferta.get("mercado"), value.edge_pct)
        is_value = False

    if value.flag:
        # Log separado (WARNING, não INFO) pra revisão manual: edge acima do
        # teto de sanidade quase sempre é mercado casado errado, não achado —
        # ver EDGE_TETO_SANIDADE em config.py. Nunca entra no Kelly automático
        # (stake.calcular já zera sozinho quando recebe `flag`).
        log.warning("SUSPEITO (%s): %s — %s — edge %+.1f%% (confiança %s)",
                   value.flag, oferta.get("evento"), oferta.get("mercado"),
                   value.edge_pct, value.confianca)

    saida.update({
        "odd_justa": value.odd_justa,
        "edge_pct": value.edge_pct,
        "is_value": is_value,
        "tipo_mercado": value.tipo_mercado,
        "threshold_usado": value.threshold_usado,
        "confianca": value.confianca,
        "flag": value.flag,
        "classe_mercado": classe_mercado,
        "interpolada": fair.interpolada,
        "derivada": fair.derivada,
        "por_consenso": fair.por_consenso,
        "n_casas_consenso": fair.n_casas_consenso,
        "n_precos_consenso": fair.n_precos_consenso,
        "stake_unidades": aposta.unidades,
        "stake_descricao": aposta.descrever(),
        "stake_kelly_cheio": aposta.kelly_cheio,
        "status": "avaliada",
    })

    # 6. segunda camada: histórico do SofaScore, só pra quem passou do filtro
    # de confiança (edge de odds já decidiu que vale a pena olhar). Isto NÃO
    # refaz o edge nem o stake — é sinal de cima, e as duas camadas ficam
    # visíveis separadas no log (dono: `sofascore-stats`).
    if STATS_SOFASCORE_ATIVO and saida["confianca"] in _CONFIANCA_ELEGIVEL_STATS:
        try:
            stats = checar_stats(oferta, [l.texto for l in legs],
                                 eventos_por_perna=[l.evento_texto for l in legs])
        except Exception as exc:  # a camada de stats nunca derruba a avaliação
            log.warning("sofascore: checar_stats falhou pra %s: %s",
                       oferta.get("evento"), exc)
            stats = None
        if stats is not None:
            saida["stats_sofascore"] = stats
            rebaixar = (stats.get("flag_noticia_fresca") is True
                       or stats.get("diverge_da_estimativa_independente") is True)
            if rebaixar:
                nova = _UM_DEGRAU_ABAIXO.get(saida["confianca"])
                if nova:
                    # ⚠️ O stake acima já foi calculado com a confiança
                    # ORIGINAL (`stake.py` não é meu — não recalculo aqui).
                    # `confianca_original` fica gravado pra quem ler o alerta
                    # entender que o `stake_unidades` não reflete este
                    # rebaixamento.
                    log.info("sofascore: confiança rebaixada de %s pra %s — %s — "
                             "%s: %s", saida["confianca"], nova, oferta.get("evento"),
                             ("notícia fresca" if stats.get("flag_noticia_fresca")
                              else "combo diverge do histórico"), stats)
                    saida["confianca_original"] = saida["confianca"]
                    saida["confianca"] = nova

    # 7. curadoria estruturada (`curadoria.py`) — roda ALÉM do rebaixamento
    # legado acima, não em vez dele, enquanto os dois convivem (ver
    # CURADORIA_ATIVA/CURADORIA_ENFORCE em value/config.py). Usa
    # `value.confianca` (o tier ORIGINAL, antes do bloco 6 acima) como entrada
    # de propósito — assim os dois blocos nunca compõem um sobre o outro
    # nem competem por quem escreve `saida["confianca"]` por último; se os
    # dois estiverem em enforce ao mesmo tempo (não é o uso pretendido, mas
    # o código não impede), este bloco roda por último e prevalece.
    if config.CURADORIA_ATIVA and value.confianca in curadoria.ELEGIVEL:
        try:
            veredito = curadoria.avaliar(
                oferta, [l.texto for l in legs], value.confianca,
                eventos_por_perna=[l.evento_texto for l in legs])
        except Exception as exc:  # a curadoria nunca derruba a avaliação
            log.warning("curadoria: avaliar falhou pra %s: %s",
                       oferta.get("evento"), exc)
            veredito = None
        if veredito is not None:
            saida["veredito_curadoria"] = veredito
            if config.CURADORIA_ENFORCE:
                if veredito.decisao == "vetado":
                    log.info("curadoria: VETO (%s) — %s — %s — edge %+.1f%%",
                             veredito.motivo, oferta.get("evento"),
                             oferta.get("mercado"), value.edge_pct)
                    saida["is_value"] = False
                    saida["status_curadoria"] = "vetado"
                elif veredito.decisao == "degrau":
                    log.info("curadoria: confiança rebaixada de %s pra %s "
                             "(%s) — %s — %s", veredito.confianca_original,
                             veredito.confianca_final, veredito.motivo,
                             oferta.get("evento"), oferta.get("mercado"))
                    saida["confianca"] = veredito.confianca_final

    return saida


def avaliar_ofertas(ofertas: Iterable[dict], storage=None) -> list[dict]:
    """Avalia um lote, reaproveitando a lista de jogos da Pinnacle.

    `storage` habilita a referência de consenso entre casas. Sem ele o
    avaliador funciona igual a antes, só que sem cobrir prop nenhum.
    """
    ofertas = list(ofertas)
    with PinnacleScraper() as scraper:
        matchups = scraper.jogos_normalizados()
        por_esporte = Counter(m.sport for m in matchups)
        log.info("pinnacle: %d jogos carregados (%s)", len(matchups),
                 ", ".join(f"{n} {e}" for e, n in sorted(por_esporte.items())))
        resultados = [avaliar_oferta(o, matchups, scraper, storage=storage)
                      for o in ofertas]

    # Breakdown detalhado dos resultados
    statuses = Counter(r.get("status") for r in resultados)
    edges = [r["edge_pct"] for r in resultados
             if r.get("status") == "avaliada" and r.get("edge_pct") is not None]
    values = [r for r in resultados if r.get("is_value")]

    partes = []
    if statuses.get("avaliada"):
        partes.append(f"{statuses['avaliada']} avaliada(s)")
    if statuses.get("sem_match_evento"):
        partes.append(f"{statuses['sem_match_evento']} sem match")
    if statuses.get("sem_odd_justa"):
        partes.append(f"{statuses['sem_odd_justa']} sem cobertura")
    log.info("resultado: %s", ", ".join(partes) if partes else "nenhum")

    # Quebra do balde "sem cobertura" por ONDE morreu. Sem isto o log dava um
    # número só, e as quatro causas (evento fora do pool, perna sem chave,
    # consenso não formou, mercado ausente na Pinnacle) pedem consertos
    # completamente diferentes — a de longe maior é a primeira, e é a única
    # que não se conserta mexendo em código de casamento.
    classes = Counter(r.get("motivo_classe") for r in resultados
                      if r.get("status") == "sem_odd_justa")
    if classes:
        log.info("sem cobertura por causa: %s",
                 ", ".join(f"{n} {classe or '?'}"
                           for classe, n in classes.most_common()))

    if edges:
        log.info("edges: min %+.1f%% / max %+.1f%% / %d value(s)",
                 min(edges), max(edges), len(values))

    return resultados


def formatar(resultado: dict[str, Any]) -> str:
    """Linha de log pra validação manual."""
    cabecalho = f"{resultado.get('evento')} — {resultado.get('mercado')}"
    status = resultado.get("status")

    if status == "sem_match_evento":
        return f"{cabecalho}\n  SEM MATCH (evento não encontrado na Pinnacle, pulado)"
    if status == "sem_odd_justa":
        return f"{cabecalho}\n  SEM MATCH ({resultado.get('motivo')}, pulado)"

    marca = "🔥 VALUE" if resultado.get("is_value") else "sem edge suficiente"
    if resultado.get("is_value") and resultado.get("stake_descricao"):
        marca += f" | apostar {resultado['stake_descricao']}"
    tipo = resultado.get("tipo_mercado")
    # "interpolada" avisa que alguma linha foi estimada entre duas publicadas —
    # o número é uma aproximação, não o preço que a Pinnacle mostra.
    qualificadores = [tipo, "de-vig", resultado["fonte_odd"]]
    if resultado.get("interpolada"):
        qualificadores.append("linha interpolada")
    return (
        f"{cabecalho}\n"
        f"  odd boost: {resultado['odd_boost']:.2f} | "
        f"odd justa: {resultado['odd_justa']:.2f} ({', '.join(qualificadores)}) | "
        f"edge: {resultado['edge_pct']:+.1f}% | {marca}"
    )
