# Casas mapeadas — API pública confirmada sem login

**11 de 36 casas** têm API JSON acessível sem login, verificada ao vivo em
**2026-08-03**. Duas já rodam em produção; **9 são novas e prontas para entrar**.

Metodologia e por que as outras 25 ficaram de fora:
[LEVANTAMENTO-CASAS.md](LEVANTAMENTO-CASAS.md) · [CASAS-PENDENTES.md](CASAS-PENDENTES.md)

---

## Em produção hoje

| casa | plataforma | endpoint | status |
|---|---|---|---|
| **Betano BR** | Kaizen Gaming | `www.betano.bet.br/api/` | ✅ rodando |
| **Esportiva Bet** | Altenar | `sb2frontend-altenar2.biahosted.com/api/widget` | ✅ rodando |

## Novas — Altenar, mesmo parser, só falta ligar

Todas respondem no **mesmo endpoint** da Esportiva. Muda só `integration`:

```
GET https://sb2frontend-altenar2.biahosted.com/api/widget/GetEvents
    ?integration=<slug>&culture=pt-BR&countryCode=BR&deviceType=1
    &numFormat=en-GB&timezoneOffset=-180&langId=1&sportId=66&champIds=&period=0
```

Sem login, sem token, sem cookie de sessão, sem quota. Verificado: **HTTP 200 em
todas**, nos três esportes (futebol 66, basquete 67, tênis 68).

| casa | slug | domínio | eventos¹ | mercado turbinado² | ofertas reais³ |
|---|---|---|---|---|---|
| **BateuBet** | `bateu` | bateu.bet.br | 998 | ✅ Vencedor do encontro - Odds Aumentadas | **112** |
| **BetGorillas** | `betgorillas` | betgorillas.bet.br | 979 | — só no detalhe | **47** |
| **EstrelaBet** | `estrelabet` | estrelabet.bet.br | 1167 | ✅ Vencedor do encontro - Super Odds | **34** |
| *(EsportivaBet)* | `esportiva` | esportiva.bet.br | 1104 | ✅ 1x2 - Odds Aumentadas | **31** |
| **4Play** | `4play` | 4play.bet.br | 1214 | — só no detalhe | **22** |
| **vupi** | `vupi` | vupi.bet.br | 1154 | ✅ Vencedor do encontro - Super Odds | **22** |
| **VaiDeBet** | `vaidebet` | vaidebet.bet.br | 877 | — só no detalhe, **a partir do 20º jogo** | **129**⁴ |
| **MultiBet** | `multibet` | multi.bet.br | 1116 | — | 0 |
| **BetPix365** | `betpix365` | betpix365.bet.br | 950 | — | 0 |
| **JogoDeOuro** | `jogodeouro` | jogodeouro.bet.br | 977 | — | 0 |

¹ soma de futebol + basquete + tênis no momento do teste.
² nome do mercado turbinado na listagem. "só no detalhe" = a casa **não publica
mercado turbinado nenhum** na listagem, mas tem combos no array `boosts`.
³ ofertas efetivamente capturadas pelo scraper — não é mais estimativa.
⁴ medido isolando a VaiDeBet por 4 visitas seguidas, tempo de varrer a janela
inteira. No rodízio normal ela chega lá em ~16 ciclos.

**MultiBet, BetPix365 e JogoDeOuro** foram conferidas a fundo: 30 eventos com
detalhe pedido, **0 combos**, nenhum mercado turbinado. Não é bug nosso — não
tinham promoção no ar. A API delas responde normalmente, então ficam ligadas.

### Três armadilhas que só apareceram na implementação

O levantamento inicial estimou combos amostrando eventos, e isso escondeu três
problemas que só o scraper real expôs. Nenhum deles gera erro no log — "zero
oferta" é um resultado legítimo, então a única forma de perceber é olhar
`query.py --casas` e desconfiar do zero.

1. **Cada casa batiza o mercado turbinado do seu jeito.** O filtro original
   casava só `"aumentad"` — EstrelaBet e vupi usam **"Super Odds"** e devolviam
   zero. Hoje `MARCAS_TURBINADO` é uma lista.
2. **4Play e BetGorillas não publicam mercado turbinado nenhum na listagem.**
   O boost delas existe *só* no array `boosts` do detalhe. Como o scraper só
   pedia detalhe de quem "mostrou boost na listagem", essas duas também
   devolviam zero. Hoje quem tem mercado turbinado tem prioridade no orçamento
   e o resto sonda os demais eventos.
3. **O boost não fica no topo da listagem.** Medido nos 40 primeiros jogos:
   EstrelaBet tem combos nos índices 0–6, 4Play em 0–11, mas a **VaiDeBet só a
   partir do 20** — e o orçamento de 12 nunca chegava lá. Ela tinha **77
   combos** e devolvia zero. Hoje cada visita sonda uma fatia diferente da
   listagem, dando a volta (`ALTENAR_JANELA_SONDA`).

Sem as três correções, **5 das 9 casas novas contribuiriam nada**.

### E uma regressão que a correção 3 criou

Girar a janela fez as ofertas da fatia anterior sumirem do snapshot — e o diff
começou a marcá-las como expiradas, para renascerem na visita seguinte. Isso
polui o histórico e fura a dedup: a oferta volta como "nova" e realerta.

A correção foi levar o escopo um nível abaixo do rodízio de casas: `diff_offers`
recebe também `escopo_detalhe`, o conjunto de `(casa, evento)` cujo detalhe foi
pedido. Combo que não foi sondado não expira. Como rede de segurança, oferta com
`valido_ate` no passado expira de qualquer jeito — senão um combo não sondado
ficaria ativo depois do jogo começar.

### As duas vias de boost — idênticas às que o scraper já lê

| via | onde | traz a odd original? |
|---|---|---|
| mercado `*- Odds Aumentadas` / `*- Super Odds` | listagem `GetEvents` | não — só no detalhe |
| array `boosts` | `GetEventDetails` | **sim**, `price` + `boostInfo.price` |

É exatamente o que `esportiva.py` já parseia. Nenhum código novo de parsing.

---

## Status: implementado

As 10 estão em `config.CASAS_ALTENAR` e rodando. O que mudou no código:

| arquivo | mudança |
|---|---|
| `config.py` | tabela `CASAS_ALTENAR` + `CASAS_POR_CICLO` + `ALTENAR_MAX_DETALHES` |
| `esportiva.py` | recebe uma `CasaAltenar` em vez das constantes fixas; filtro de mercado turbinado virou lista; orçamento de detalhes sonda além da listagem |
| `diff.py` | `casas_raspadas` + `fora_do_ciclo`, pro rodízio não expirar quem não foi visitado |
| `main.py` | `casas_do_ciclo()` com cursor persistido no banco |
| `query.py` | `--casa` e `--casas` |

`Offer`, `matcher`, de-vig, Kelly e alertas **não mudaram** — a casa já fazia
parte da identidade da oferta (`models.py:58`), então as 11 convivem no mesmo
banco sem colidir.

### O orçamento de request

Era **1 casa × 25 detalhes** por ciclo. Dez casas no mesmo teto seriam **250
requests por ciclo de 180s** — o caminho rápido pro `403` que o
[AVISOS.md](AVISOS.md#6) descreve.

A saída foi **rodízio**: 3 casas por ciclo (`CASAS_POR_CICLO`), girando, com teto
de 12 detalhes por casa. Cada casa é visitada a cada 4 ciclos (~12 min), dentro
do TTL de reavaliação de 60 min. Preferido a simplesmente baixar o teto porque
mantém a cobertura *dentro* de cada casa — baixar para 2-3 detalhes por casa
caberia no orçamento mas nunca olharia além dos primeiros eventos da listagem.

Medido em produção: **~100s por ciclo** com 3 casas, dentro dos 180s padrão.
Subir `CASAS_POR_CICLO` ou `ALTENAR_MAX_DETALHES` pede subir o intervalo junto.

## Observações que afetam a modelagem

**vupi é white-label da EstrelaBet.** Mesmo backend (`app.estrelabet.com`,
`assets.estrelabet.bet.br` carregados na home da vupi), mas os feeds voltaram
diferentes — 1154 vs 1167 eventos, 16 vs 28 combos. **Raspar as duas**: os
boosts não são os mesmos.

**BetPix365 e VaiDeBet devolveram feed idêntico** — 842 eventos e 4955 mercados
no futebol, número a número. Mesma configuração de Altenar. Aqui a sobreposição
é real; a dedup por `offer_id` resolve porque a casa está na identidade, mas vale
lembrar que raspar as duas custa o dobro pelo mesmo conteúdo enquanto isso durar.

**PixBet ≠ BetPix365.** São casas diferentes, domínios diferentes
(`pix.bet.br` vs `betpix365.bet.br`) e plataformas diferentes — só a BetPix365
está nesta lista; a PixBet está nas pendentes.

**Ultrabet, Maxima e Suprema** são a mesma plataforma (aparecem lado a lado no
bundle da Ultrabet) — se uma for resolvida no futuro, as três entram juntas.
Nenhuma está nesta lista.
