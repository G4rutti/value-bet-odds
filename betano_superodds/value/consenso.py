"""Odd justa a partir do CONSENSO entre as casas, quando a Pinnacle não cobre.

Por que isto existe
-------------------
A Pinnacle é a referência do projeto porque é sharp. Só que ela publica apenas
mercados de gol no futebol — 1X2, handicap, totais, totais por equipe e um
punhado de specials. Conferido na API ao vivo: **nenhum prop de jogador**. Cada
linha de `market_parser.SEM_COBERTURA` é uma afirmação verdadeira.

Isso deixava sem referência ~25% das apostas que o usuário de fato faz (chutes
ao gol, cartões, artilheiro). A oferta era capturada e destruída em
`pipeline.avaliar_oferta`.

Quando não existe fonte sharp, a saída possível é o consenso das casas moles:
de-vigar o mesmo mercado em várias casas e tomar a MEDIANA das probabilidades.

⚠️ Isto é referência mais fraca que a Pinnacle, e a diferença não é de grau
-------------------------------------------------------------------------
A Pinnacle é referência porque tem dinheiro sharp corrigindo o preço. Um
consenso de casas moles pode estar errado *junto* — todas copiam a mesma
origem, e nesse caso o "edge" mede desvio do rebanho, não vantagem real.

Por isso o tratamento aqui é o mesmo que o projeto já dá ao modelo de placar:
fonte própria (`consenso`), threshold próprio e bem mais alto
(`EDGE_MIN_CONSENSO`), desconto de stake próprio e marcação obrigatória no
alerta. O número serve pra dizer "esta casa está fora da linha das outras",
que é uma afirmação bem mais modesta do que "isto é value".

Decisões de desenho
-------------------
- **Mediana, não média.** Uma casa com preço maluco não pode arrastar a
  referência. Com 3 casas, a mediana ignora a extrema.
- **De-vig por casa, antes de agregar.** Cada casa tem margem própria; agregar
  preço bruto misturaria margem com probabilidade.
- **Janela de tempo.** `CASAS_POR_CICLO=3` faz o rodízio raspar poucas casas
  por vez. Exigir as casas no mesmo ciclo faria o consenso nunca formar.
- **A casa avaliada sai da conta.** Comparar a oferta turbinada contra um
  consenso que a inclui puxa a referência na direção da própria oferta e
  encolhe o edge — a casa não pode ser sua própria referência.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from statistics import median

from . import config
from .fair_odds import ProbJusta, remover_vig
from .market_parser import ChaveConsenso, chave_consenso
from .models import Market

log = logging.getLogger(__name__)


# Cartão, chute ao gol, artilheiro (gol de jogador) e handicap pedem um
# mínimo de casas mais alto (`CONSENSO_MIN_CASAS_PROP`) que o consenso
# genérico — são a classe mais fraca da tabela de confiança (skill
# `value-bet-methodology`). `calcular` só recebe o texto do mercado (não o
# `Leg` estruturado — quem tem o `Leg` é `fair_odds.py`, que não é deste
# agente), então a classificação aqui é por palavra-chave no `market_nome`,
# a mesma ideia de `value_calc.classe_mercado_da_perna` mas rodando sobre
# outro dado de entrada. As duas funções concordam nas mesmas quatro
# categorias por desenho; ver o relatório do agente para a decisão de manter
# a classificação duplicada em vez de fazer `fair_odds.py` (que não é deste
# agente) repassar a classe já resolvida.
_PROP_PALAVRAS = re.compile(
    r"cart(õ|o)es|cart(ã|a)o"
    r"|chutes?\s+(no|a|ao)\s+gol"
    r"|marcar\s+(em\s+qualquer\s+momento|a\s+qualquer)"
    r"|artilheiro|marcador"
    r"|\bhandicap\b",
    re.IGNORECASE)


def _eh_mercado_prop(market_nome: str) -> bool:
    return bool(_PROP_PALAVRAS.search(market_nome))


# Mesmas quatro famílias que `_PROP_PALAVRAS` cobre por regex, só que pelo
# campo estruturado — usado quando o casamento veio por `ChaveConsenso`
# (fallback canônico) em vez de igualdade de string do `market_nome`. Não
# inclui `tie_break`/`duplas_faltas`/`aces` de propósito: o regex antigo
# também não casava essas palavras, então o mínimo genérico é o comportamento
# equivalente, não uma mudança de critério.
_FAMILIAS_PROP = frozenset({"cartoes", "chutes_gol", "artilheiro"})


def _normalizar(texto: str) -> str:
    """Nome de mercado/seleção comparável entre casas.

    As casas Altenar compartilham o catálogo, mas não a grafia: acento, caixa e
    pontuação variam. Não usa `matcher.normalizar` porque aquele é afinado pra
    NOME DE TIME (remove sufixo de clube, sigla de UF) e aqui isso destruiria
    rótulos como "Mais de 2.5".
    """
    t = str(texto).lower().strip()
    for de, para in (("á", "a"), ("à", "a"), ("ã", "a"), ("â", "a"), ("é", "e"),
                     ("ê", "e"), ("í", "i"), ("ó", "o"), ("õ", "o"), ("ô", "o"),
                     ("ú", "u"), ("ü", "u"), ("ç", "c")):
        t = t.replace(de, para)
    t = t.replace(",", ".")
    t = re.sub(r"[^a-z0-9. ]+", " ", t)
    # Colapsa espaço: a pontuação virou espaço logo acima, e sem isto
    # "AC Milan - Total de Gols" (Esportiva) e "AC Milan Total de gols"
    # (EstrelaBet) — o MESMO mercado — não casariam por causa do hífen.
    return re.sub(r"\s+", " ", t).strip()


@dataclass
class ResultadoConsenso:
    prob: ProbJusta
    n_casas: int
    casas: list[str]
    # Quantos preços DISTINTOS as casas deram. É o número que diz se houve
    # consenso de verdade: medido no banco, 90.3% das seleções cotadas por 2+
    # casas Altenar têm preço idêntico — elas são skins do mesmo feed. Três
    # casas com um preço só é uma fonte contada três vezes, e o alerta precisa
    # dizer isso em vez de exibir "3 casas" como se fossem independentes.
    n_precos: int = 0

    @property
    def independente(self) -> bool:
        """Houve mais de uma fonte de preço de verdade?"""
        return self.n_precos > 1


# "Mais de 2.5" / "Menos de 10.5" / "Over 1.75" — o lado e a linha.
_LADO_LINHA = re.compile(
    r"^(mais de|menos de|acima de|abaixo de|over|under)\s+(\d+(?:\.\d+)?)$")


def _par_da_linha(selecao_norm: str) -> tuple[str, str] | None:
    """("mais de 2.5") -> ("mais de", "2.5"). None quando não é over/under."""
    m = _LADO_LINHA.match(selecao_norm)
    return (m.group(1), m.group(2)) if m else None


def _mercados_por_casa(linhas: list[dict], market_nome: str, selecao: str,
                       excluir_casa: str | None) -> dict[str, Market]:
    """Monta, por casa, o `Market` que serve de unidade de de-vig.

    ⚠️ A unidade NÃO é o `market_id`. Conferido no payload real: um único
    "Total de gols" carrega TODAS as linhas de uma vez — "Mais de 1", "Mais de
    1.5", ... "Menos de 6.5", umas 40 seleções. De-vigar esse bloco somaria
    ~20 probabilidades, a margem daria centenas por cento, `remover_vig`
    recusaria e o consenso morreria calado — o pior modo de falhar que existe.

    Então:

    - seleção over/under ("Mais de 2.5"): a unidade é o PAR da mesma linha
      dentro do mesmo `market_id` — {Mais de 2.5, Menos de 2.5};
    - seleção sem linha (1X2, BTTS, artilheiro): a unidade é o `market_id`
      inteiro, que aí é mesmo uma partição do espaço amostral.
    """
    alvo_mkt = _normalizar(market_nome)
    alvo_sel = _normalizar(selecao)
    par_alvo = _par_da_linha(alvo_sel)

    por_mercado: dict[tuple[str, str], dict[str, float]] = {}
    for linha in linhas:
        if excluir_casa and linha["casa"] == excluir_casa:
            continue
        if _normalizar(linha["market_nome"]) != alvo_mkt:
            continue
        preco = float(linha["preco"])
        if preco <= 1.0:
            continue
        sel = _normalizar(linha["selecao"])
        if par_alvo is not None:
            # Só as duas pontas da linha pedida entram.
            par = _par_da_linha(sel)
            if par is None or par[1] != par_alvo[1]:
                continue
        chave = (linha["casa"], str(linha.get("market_id", "")))
        por_mercado.setdefault(chave, {})[sel] = preco

    melhor: dict[str, dict[str, float]] = {}
    for (casa, _mid), outcomes in por_mercado.items():
        if alvo_sel not in outcomes or len(outcomes) < 2:
            continue
        # Menos lados = mercado mais específico, não um agregado que contém a
        # seleção por acidente.
        atual = melhor.get(casa)
        if atual is None or len(outcomes) < len(atual):
            melhor[casa] = outcomes

    return {
        casa: Market(key="consenso", label=market_nome, outcomes=outcomes)
        for casa, outcomes in melhor.items()
    }


def _chave_da_linha(linha: dict) -> ChaveConsenso | None:
    """`ChaveConsenso` de uma linha do pool, montada de `market_nome` +
    `selecao` concatenados — o mesmo par que o rótulo `"Mercado: Seleção"`
    representa no caminho literal."""
    texto = f"{linha.get('market_nome', '')} {linha.get('selecao', '')}".strip()
    return chave_consenso(texto)


def _mercados_por_casa_canonico(
        linhas: list[dict], chave_alvo: ChaveConsenso, excluir_casa: str | None,
) -> dict[str, tuple[Market, str]]:
    """Mesma unidade de de-vig de `_mercados_por_casa` — o PAR da mesma linha
    (ou o bloco inteiro do `market_id`, quando a seleção não tem linha) —,
    só que casando por `ChaveConsenso` em vez de igualdade de string do nome
    do mercado.

    ⚠️ Restrição inegociável (ver `_mercados_por_casa`): a canonicalização
    muda só a BUSCA da linha certa. A unidade de de-vig continua restrita ao
    `(casa, market_id)` e à linha exata de `chave_alvo` — nunca "tudo com a
    mesma família", que somaria dezenas de seleções e faria `remover_vig`
    recusar tudo silenciosamente.

    Guarda de escopo: só entram linhas cuja `ChaveConsenso` tem a MESMA
    família, o MESMO escopo (um `time:`/`jogador:` nunca casa com `jogo`, e
    vice-versa — é o análogo da guarda que `fair_odds.prob_da_perna` já faz
    pro caminho Pinnacle) e a MESMA linha.
    """
    por_bloco: dict[tuple[str, str], dict[str, tuple[float, str | None]]] = {}
    for linha in linhas:
        if excluir_casa and linha["casa"] == excluir_casa:
            continue
        chave_linha = _chave_da_linha(linha)
        if chave_linha is None:
            continue
        if (chave_linha.familia != chave_alvo.familia
                or chave_linha.escopo != chave_alvo.escopo
                or chave_linha.linha != chave_alvo.linha):
            continue
        preco = float(linha["preco"])
        if preco <= 1.0:
            continue
        sel = _normalizar(linha["selecao"])
        bloco = (linha["casa"], str(linha.get("market_id", "")))
        por_bloco.setdefault(bloco, {})[sel] = (preco, chave_linha.lado)

    melhor: dict[str, dict[str, float]] = {}
    alvo_por_casa: dict[str, str] = {}
    for (casa, _mid), outcomes in por_bloco.items():
        alvo_sel = next((sel for sel, (_preco, lado) in outcomes.items()
                         if lado == chave_alvo.lado), None)
        precos = {sel: preco for sel, (preco, _lado) in outcomes.items()}
        if alvo_sel is None or len(precos) < 2:
            continue
        # Menos lados = mercado mais específico, mesma lógica de
        # `_mercados_por_casa`.
        atual = melhor.get(casa)
        if atual is None or len(precos) < len(atual):
            melhor[casa] = precos
            alvo_por_casa[casa] = alvo_sel

    return {
        casa: (Market(key="consenso", label=chave_alvo.familia, outcomes=outcomes),
               alvo_por_casa[casa])
        for casa, outcomes in melhor.items()
    }


def calcular(linhas: list[dict], market_nome: str, selecao: str,
             excluir_casa: str | None = None,
             chave_alvo: ChaveConsenso | None = None) -> ResultadoConsenso | None:
    """Probabilidade de-vigada de `selecao`, por consenso entre casas.

    `linhas` vem de `Storage.mercados_para_consenso`. Devolve None quando não há
    casas suficientes — nunca devolve um número com ressalva, porque quem chama
    trataria como preço observado.

    Dois modos de casamento, em fallback (nunca substituição): primeiro tenta
    o literal de sempre (`market_nome`/`selecao` por igualdade de string). Só
    quando ele não forma nenhum mercado é que `chave_alvo` (se veio) é usada
    pro casamento canônico — regressão zero por construção no caminho que já
    funcionava.
    """
    mercados: dict[str, Market] = {}
    alvo_por_casa: dict[str, str] = {}
    modo_canonico = False

    if market_nome:
        mercados = _mercados_por_casa(linhas, market_nome, selecao, excluir_casa)
        if mercados:
            alvo_norm = _normalizar(selecao)
            alvo_por_casa = {casa: alvo_norm for casa in mercados}

    if not mercados and chave_alvo is not None:
        canonico = _mercados_por_casa_canonico(linhas, chave_alvo, excluir_casa)
        if canonico:
            mercados = {casa: mkt for casa, (mkt, _alvo) in canonico.items()}
            alvo_por_casa = {casa: alvo for casa, (_mkt, alvo) in canonico.items()}
            modo_canonico = True

    if not mercados:
        return None

    probs: list[float] = []
    casas: list[str] = []
    for casa, market in sorted(mercados.items()):
        # `remover_vig` já recusa mercado incompleto e margem implausível — é a
        # mesma guarda usada na Pinnacle, e é ela que tira do consenso a casa
        # com uma perna suspensa ou preço corrompido.
        justas = remover_vig(market)
        alvo = alvo_por_casa[casa]
        if not justas or alvo not in justas:
            continue
        probs.append(justas[alvo].probabilidade)
        casas.append(casa)

    # Cartão/chute ao gol/artilheiro/handicap pedem mais casas que o consenso
    # genérico — são a classe mais fraca da tabela de confiança, sem a
    # Pinnacle corrigindo o preço por trás (handicap normalmente TEM Pinnacle;
    # só chega até aqui quando a linha específica não está publicada). No
    # caminho canônico o mínimo vem da `familia` da chave, não do regex de
    # `market_nome` — o regex nunca viu a grafia do pool.
    if modo_canonico:
        minimo = (config.CONSENSO_MIN_CASAS_PROP
                 if chave_alvo.familia in _FAMILIAS_PROP
                 else config.CONSENSO_MIN_CASAS)
    else:
        minimo = (config.CONSENSO_MIN_CASAS_PROP if _eh_mercado_prop(market_nome)
                 else config.CONSENSO_MIN_CASAS)
    if len(probs) < minimo:
        log.debug("consenso insuficiente para %s / %s: %d casa(s), mínimo %d",
                  market_nome or chave_alvo, selecao, len(probs), minimo)
        return None

    p = median(probs)
    if not 0 < p < 1:
        return None

    # Preços distintos, não casas distintas: é o que separa consenso real de
    # uma fonte só replicada. Arredonda antes de contar — diferença na quarta
    # casa decimal é ruído de arredondamento da API, não opinião diferente.
    n_precos = len({round(x, 4) for x in probs})

    rotulo_mercado = (market_nome if not modo_canonico
                      else f"{chave_alvo.familia}:{chave_alvo.escopo}")
    sufixo = " (via chave canônica)" if modo_canonico else ""
    return ResultadoConsenso(
        prob=ProbJusta(
            probabilidade=p,
            odd_justa=round(1.0 / p, 4),
            # A margem individual já foi removida casa a casa; o que sobra aqui
            # é dispersão entre casas, não overround. Zero é a resposta honesta.
            overround=0.0,
            mercado=f"{rotulo_mercado} ({len(probs)} casas, {n_precos} preço(s)){sufixo}",
            consenso=True,
        ),
        n_casas=len(probs),
        casas=casas,
        n_precos=n_precos,
    )


class ProvedorConsenso:
    """Ponte entre o pipeline e a tabela `mercados_casa`.

    Existe como objeto (e não função solta) para carregar o `evento_id` e a
    casa da oferta em avaliação, e para poder ser injetado nos testes sem banco.
    """

    def __init__(self, storage, evento_id: str, casa: str | None = None) -> None:
        self.storage = storage
        self.evento_id = str(evento_id)
        self.casa = casa
        self._cache: list[dict] | None = None

    def _linhas(self) -> list[dict]:
        if self._cache is None:
            self._cache = self.storage.mercados_para_consenso(
                self.evento_id, config.CONSENSO_JANELA_HORAS)
        return self._cache

    def prob_para(self, texto_perna: str) -> ResultadoConsenso | None:
        """Consenso para uma perna crua.

        Formato "Mercado: Seleção" é o rótulo que os parsers das casas montam
        (`esportiva._ofertas_do_detalhe`) — comportamento idêntico a sempre:
        `partition(":")` e casamento literal por igualdade de string.

        Sem ":" (achado real: "Total de Cartões Mais de 4.5"), o casamento
        literal não tem como funcionar — não há como separar mercado de
        seleção por string. Tenta a chave canônica (`chave_consenso`) em vez
        de recusar de cara: ela é o mesmo espaço que casa a grafia do pool
        ("Total cartões: Mais de 4.5", COM ":", mas de um jeito diferente do
        rótulo da oferta). Perna sem chave reconhecida (motivo fora de
        `_FAMILIA_POR_MOTIVO`) morre aqui, sem adivinhar.
        """
        linhas = self._linhas()
        if not linhas:
            return None
        if ":" in texto_perna:
            market_nome, _, selecao = texto_perna.partition(":")
            return calcular(linhas, market_nome.strip(), selecao.strip(),
                            excluir_casa=self.casa)
        chave = chave_consenso(texto_perna)
        if chave is None:
            return None
        return calcular(linhas, "", "", excluir_casa=self.casa, chave_alvo=chave)
