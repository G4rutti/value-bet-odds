"""Veredito estruturado sobre os sinais da segunda camada (SofaScore) —
decide se uma oferta value é aprovada, rebaixada um degrau, ou VETADA.

Isto é uma camada NOVA por cima da que já existe em `pipeline.py`
(`_UM_DEGRAU_ABAIXO`), que só sabe rebaixar confiança e nunca bloqueia o
envio sozinha. Este módulo não troca `stats_check.checar_stats` — continua
sendo o único dono da COLETA de sinais (H2H aproximado, forma recente,
notícia fresca). `curadoria.py` só julga o que já foi coletado.

Convive com o código legado, não o substitui: enquanto `CURADORIA_ENFORCE`
estiver desligada (padrão), o veredito é calculado e devolvido pra ser
gravado no storage — `pipeline.py` não aplica nada, a oferta segue seu
caminho normal de `is_value`/confiança/stake exatamente como se este módulo
não existisse. Isso é DE PROPÓSITO: é o único jeito de acumular veredito
real pra comparar depois contra `liquidacoes` (`query.py --curadoria`) antes
de deixar o veto afetar dinheiro de verdade. Ver `value/config.py`,
seção "Curadoria".
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .stats_check import SofaScoreClient, checar_stats

# Confiança elegível pra rodar a checagem — inclui "baixa" de propósito
# (pedido do dono): é o tier mais frágil e hoje é o único que NUNCA passa
# por nenhuma segunda camada, porque `pipeline._CONFIANCA_ELEGIVEL_STATS`
# (o gate do código LEGADO) não inclui "baixa" e continua sem incluir —
# aquele gate é do bloco antigo, este é deste módulo.
ELEGIVEL = frozenset({"alta", "média-alta", "média", "baixa"})

# Mesma escala de `value_calc.classificar_confianca`. "baixa" não desce mais
# (não existe degrau abaixo dela na tabela de confiança) — um veredito
# "degrau" numa oferta já "baixa" vira só um aviso no motivo, sem mudar a
# confiança final.
_UM_DEGRAU_ABAIXO = {
    "alta": "média-alta",
    "média-alta": "média",
    "média": "baixa",
}

# Rótulo pra log/relatório de cada sinal grave possível. A chave é o campo
# que `stats_check.checar_stats` devolve.
_SINAIS_GRAVES = {
    "flag_noticia_fresca": "notícia fresca",
    "diverge_da_estimativa_independente": "combo diverge do histórico",
    "forma_desfavoravel": "forma recente ruim",
}


@dataclass
class Veredito:
    decisao: str                     # "aprovado" | "degrau" | "vetado"
    motivo: str | None
    sinais: dict = field(default_factory=dict)
    confianca_original: str = ""
    confianca_final: str = ""
    modo: str = "sombra"              # "sombra" | "enforce" — o que ESTE veredito usou


def _julgar(sinais: dict, confianca_atual: str) -> tuple[str, str | None]:
    """Regra explícita, não uma soma de score oculta: cada sinal grave que
    concorda entra no `motivo`, pra quem lê o log/relatório entender
    exatamente por que vetou.

    Vetar exige `config.CURADORIA_MIN_SINAIS_VETO` (default 2) sinais graves
    concordando ao mesmo tempo — nenhum sinal isolado derruba a oferta
    sozinho, a mesma garantia que a checagem legada já dava (ela também só
    reage quando pelo menos 1 sinal acende, mas nunca veta). A diferença:
    aqui 2+ sinais concordando pesam mais do que um simples rebaixamento de
    1 degrau — porque é justamente esse caso ("dois sinais independentes
    apontando problema ao mesmo tempo") que o dono pediu pra tratar como
    "não vale a pena mandar", não só "confiar um pouco menos".
    """
    graves = [rotulo for chave, rotulo in _SINAIS_GRAVES.items()
             if sinais.get(chave) is True]

    if len(graves) >= config.CURADORIA_MIN_SINAIS_VETO:
        return "vetado", " + ".join(graves)
    if graves:  # 1+ sinal grave, mas abaixo do piso de veto
        return "degrau", " + ".join(graves)
    return "aprovado", None


def avaliar(oferta: dict, mercado_pernas: list[str], confianca: str, *,
           eventos_por_perna: list[str | None] | None = None,
           cliente: SofaScoreClient | None = None) -> Veredito | None:
    """Coleta os sinais (via `stats_check.checar_stats`) e julga.

    Devolve `None` quando a oferta não é elegível (`confianca` fora de
    `ELEGIVEL`) — quem chama nem gasta request nesse caso. Nunca levanta
    exceção: `checar_stats` já garante isso pra coleta (qualquer falha de
    rede vira campo `None` + log, não exceção); o julgamento em si é puro,
    sem I/O.
    """
    if confianca not in ELEGIVEL:
        return None

    sinais = checar_stats(oferta, mercado_pernas, cliente=cliente,
                          eventos_por_perna=eventos_por_perna)
    decisao, motivo = _julgar(sinais, confianca)

    confianca_final = confianca
    if decisao == "degrau":
        confianca_final = _UM_DEGRAU_ABAIXO.get(confianca, confianca)
    elif decisao == "vetado":
        # Não faz sentido reportar uma "confiança final" pra uma oferta que
        # não vai ser enviada de qualquer forma — mas o campo continua
        # preenchido (= a original) pra quem ler o registro em modo sombra
        # não achar que o veto zerou a confiança da avaliação original.
        confianca_final = confianca

    return Veredito(
        decisao=decisao,
        motivo=motivo,
        sinais=sinais,
        confianca_original=confianca,
        confianca_final=confianca_final,
        modo="enforce" if config.CURADORIA_ENFORCE else "sombra",
    )
