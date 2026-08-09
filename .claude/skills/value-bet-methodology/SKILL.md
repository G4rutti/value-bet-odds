---
name: value-bet-methodology
description: Metodologia de odd justa deste bot — de-vig proporcional, tabela de fonte/threshold/confiança por mercado, e as armadilhas conhecidas (mercado restrito casado com referência ampla, consenso fraco, correlação em combo, viés de zebra). Carregue ANTES de tocar em fair_odds.py, value_calc.py, market_parser.py, pinnacle.py, consenso.py ou stake.py — inclusive pra mudanças que parecem triviais.
---

# Metodologia de value bet

## 1. De-vig proporcional

`remover_vig` (`betano_superodds/value/fair_odds.py`):

```python
implicitas = {nome: 1/odd for nome, odd in market.outcomes.items()}
overround  = sum(implicitas.values())
prob       = imp / overround * esperado
odd_justa  = 1 / prob
```

Algebricamente isso é `odd_justa = odd_crua × overround / esperado`.

**Invariante que nunca pode quebrar:** com `esperado = 1` e `overround ≥ 1`, a
odd justa é sempre **maior ou igual** à odd crua daquele lado. De-vig tira
margem; tirar margem só aumenta a odd. Coberto por
`tests/test_value.py::TestDeVig::test_odd_justa_e_sempre_maior_que_a_crua`.

Se você observar uma justa **abaixo** da crua, o de-vig não é o culpado —
provavelmente a odd crua que você está olhando veio de **outro mercado** que
não o que foi de-vigado. Vá direto pra seção 3.

Exceção legítima: `MARGEM_MIN = -0.01` deixa passar overround até 0.99
(arbitragem real, raríssimo). Aí a justa fica até 1% abaixo da crua.

### `SOMA_ESPERADA` — nem todo mercado soma 1

Quase todo mercado é uma partição do espaço amostral e soma 1. A **chance
dupla** não: cada desfecho cobre dois dos três resultados, então a soma honesta
é 2. Normalizar pra 1 daria margem de ~109%, o mercado seria descartado como
corrompido, e a chance dupla nunca teria referência.

⚠️ A tabela indexa por `market.key` com match exato. Chave parametrizada
(`totals:2.5`, `team_total:home:1.5`) nunca vai casar. Se algum dia um mercado
não-partição ganhar chave parametrizada, ele normaliza silenciosamente pra 1.

### Partição incompleta

`"{Time} Goals"` da Pinnacle vem com buckets `0..4`, **sem terminal `N+`**. A
soma verdadeira é <1, e normalizar pra 1 infla cada probabilidade um pouco
(deflaciona a justa). Pra gols de um time só a cauda é ~0.5% — tolerável, mas
saiba que está lá. `"Exact Total Goals"` tem `4+` e é completo.

### Viés de zebra

De-vig proporcional **subestima sistematicamente a justa de azarão**. Medido no
histórico de alertas: edge médio de +111% na faixa de odd 8+, contra +11% na
faixa até 5. Não há Shin nem power de-vig no código. O `ODD_JUSTA_MAX = 5.0`
(`config.py`) existe pra mascarar exatamente essa faixa — é um curativo, não uma
correção. Qualquer edge alto em odd alta merece desconfiança extra.

## 2. Fonte, threshold e confiança

**Fonte vence tipo de mercado.** Se qualquer perna veio de consenso ou de
modelo, é o erro dela que domina, combo ou não.

| Tipo de mercado | Fonte prioritária | Mín. preços p/ consenso | Threshold de edge | Confiança |
|---|---|---|---|---|
| Simples, com Pinnacle | Pinnacle (de-vig) | — | 5% | alta |
| Simples, sem Pinnacle | Consenso (de-vig) | 5 casas | 8% | média |
| Combo, Pinnacle em todas as pernas | Pinnacle por perna | — | 18% | média-alta |
| Combo, sem Pinnacle em alguma perna | Consenso (de-vig) | 5 casas por perna | 30% | baixa |
| Cartão / gol de jogador / chute / handicap | Consenso (de-vig) | 6 casas | 30% | baixa |

Regras que acompanham a tabela:

- Consenso abaixo do mínimo → `confianca: insuficiente`, **não calcula edge** e
  não entra no Kelly.
- **Teto de sanidade:** edge > 50% em qualquer mercado → `flag:
  possivel_erro_matching`, log separado, nunca entra no Kelly automático.
- Kelly ¼ só pra `alta` e `média-alta`. `média`/`baixa` → ⅛ Kelly ou nenhum
  stake automático, só log pra decisão manual.

### ⚠️ Casas Altenar são um feed só

`n_casas` **não** mede independência. ~90% das seleções nas casas Altenar têm
preço idêntico — 5 casas podem ser 1 preço. `consenso.py` já calcula
`n_precos` (preços distintos arredondados) justamente por isso. Contagem alta
de casas com `n_precos` baixo é rebanho, não consenso: aí o edge mede desvio do
rebanho, não value.

Hoje `n_precos` é **diagnóstico visível no log**, não gate. Decisão consciente
do dono, tomada com o risco na mesa.

## 3. A armadilha principal: mercado restrito × referência ampla

**Regra:** o mercado usado como referência tem que descrever *exatamente* o
mesmo evento que o rótulo da oferta. Se o rótulo nomeia um time, a chave tem
que ter componente de time (`:home` / `:away`).

Caso real (2026-08, corrigido):

```
oferta:  "1º tempo - Internacional gols exatos: 1"      (Inter faz 1 no 1ºT)
chave errada: exact_goals_1t = "Exact Total Goals 1st Half"  (o JOGO tem 1 gol no 1ºT)
```

Causa: `re.search(r"gols\s+exatos\s*:\s*(\d+)\s*$", ...)` não é ancorado à
esquerda, então o nome do time na frente passava batido. O evento amplo é mais
provável → justa 2.79 contra crua 3.82 → edge fantasma de +23.6%.

Sintomas desta classe de bug:
- justa **abaixo** da crua (impossível se o de-vig recebeu o mercado certo);
- edge alto num mercado exótico;
- rótulo com nome de time, chave sem componente de time.

Defesas no código: a guarda em `prob_da_perna` que recusa perna com
`time_nome` preenchido quando a chave não tem `:home`/`:away`; e o modo
`FAIR_ODDS_TRACE=1`, que imprime **chave resolvida + label da Pinnacle + odds
cruas** por perna — a chave e o label são o que revela o erro, não os números.

**Ao adicionar qualquer regex nova em `market_parser.py`**: teste contra um
rótulo com nome de time na frente e contra um qualificador de período na
frente. Use `_PREFIXO_PERIODO` pra tirar o período e `_NAO_E_TIME` pra recusar
tokens que não são time — os dois já existem no arquivo.

### Períodos

A Pinnacle publica só período 0 (jogo inteiro) e 1 (1º tempo). `_sufixo()`
devolve `""` pra qualquer período desconhecido — ou seja, **2º tempo viraria
jogo inteiro**. Por isso existem a guarda explícita de 2º tempo em `parse_leg`
e o `_sufixo_especial()`, que devolve `None` (recusa) em vez de `""`. Mesma
armadilha, outra dimensão.

## 4. Combo e correlação

A justa de combo é o **produto das pernas**, que assume independência. Isso a
deixa otimista, e edge inflado vira stake inflada na mesma proporção (Kelly é
linear no edge). Daí `EDGE_MIN_COMBO` alto e `STAKE_DESCONTO_COMBO`.

`_conjunta_correlacionada` (`fair_odds.py`) já substitui o produto por uma
conjunta de matriz de placar **para o subconjunto de pernas que são função do
placar final**. Ela é indexada posicionalmente contra a lista de pernas — só é
segura porque `probs` é preenchida 1:1 na ordem das pernas. Se mexer na ordem
de uma, mexa na da outra.

## 5. Modelo de placar

Poisson/Dixon-Coles (`modelo_gols.py`), usado só como último recurso quando a
Pinnacle não publica o mercado. **Nasce desligado pra alerta**: o p90 do erro é
32%, o que é conclusão de calibração e não pendência. `EDGE_MIN_MODELO = 35%` e
`STAKE_DESCONTO_MODELO = 0.4` refletem isso. Preço observado sempre ganha de
modelo.

## 6. Antes de fechar qualquer mudança aqui

```bash
python -m pytest tests/ -q
FAIR_ODDS_TRACE=1 python avaliar.py --limit 40 --todas
```

No trace, confira **chave e label** de cada perna, não só o número. Uma justa
abaixo da crua é bug estrutural até prova em contrário.
