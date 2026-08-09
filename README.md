# Super Odds Scanner — 11 casas

Raspa as ofertas turbinadas de **11 casas**, guarda em SQLite, loga novas /
alteradas / expiradas a cada ciclo, avalia o edge contra a Pinnacle e alerta no
Telegram.

## Casas monitoradas

| plataforma | casas | fontes de boost |
|---|---|---|
| **Kaizen Gaming** | Betano BR | `MR12` + `smart-picks` |
| **Altenar** | Esportiva Bet, EstrelaBet, 4Play, BetGorillas, vupi, BateuBet, MultiBet, BetPix365, JogoDeOuro, VaiDeBet | mercado turbinado na listagem + array `boosts` |

As 10 da Altenar rodam **o mesmo código**: a API é multi-tenant e quem seleciona
a marca é o parâmetro `integration`. Casa nova ali é uma linha em
`config.CASAS_ALTENAR`, não um parser novo.

### O critério de entrada

Não é "tem odd turbinada" — quase toda casa tem. É a conjunção de três coisas:

1. **API JSON pública, sem login e sem quota.** Nenhuma das duas pede cookie de
   sessão, token ou header `x-*`. Isso é o que dispensa Playwright: o custo por
   casa cai de "navegador headless por evento" para "um GET".
2. **A odd original vem junto, ou a um request de distância.** Sem o preço
   pré-boost não dá pra calcular `ganho_pct` nem confirmar que a oferta é
   turbinada de verdade.
3. **Estrutura que cabe no mesmo pipeline.** As duas entregam boost por
   exatamente duas vias — um 1X2 turbinado na listagem e um array de combos de
   bet-builder no detalhe do evento. A Esportiva entrou reusando `Offer`,
   `diff`, `matcher` e todo o cálculo de edge; só o parser é novo.

O que **não** é limitador: quantidade de casas com promoção. O que limita é
volume de request — os WAFs escalam bloqueio com tráfego (ver
[AVISOS.md](AVISOS.md#6)), então cada casa nova precisa pagar o próprio custo em
requisições.

> **Pinnacle não é uma terceira casa.** É a referência de odd justa (Parte 2).
> Ninguém aposta nela aqui.

### Expansão — levantamento de 2026-08-03

Foram testadas **36 casas** contra esse critério; 10 passaram e já estão ligadas.

| documento | o quê |
|---|---|
| [CASAS-MAPEADAS.md](CASAS-MAPEADAS.md) | as 11 acessíveis, com slug, domínio e volume medido |
| [CASAS-PENDENTES.md](CASAS-PENDENTES.md) | as 25 não resolvidas, com o motivo de cada uma |
| [LEVANTAMENTO-CASAS.md](LEVANTAMENTO-CASAS.md) | método, o que funcionou, o que deu errado e como reproduzir |

### Rodízio de casas — por que não raspar todas todo ciclo

Cada casa custa 1 listagem por esporte + `ALTENAR_MAX_DETALHES` requests de
detalhe. As 10 de uma vez seriam ~120 requests de detalhe por ciclo de 180s,
contra WAFs que escalam bloqueio por volume.

Então o ciclo visita `CASAS_POR_CICLO` (3) casas por vez, girando a lista. Com
10 casas, cada uma é visitada a cada 4 ciclos — ~12 min, bem dentro do TTL de
reavaliação de 60 min. O cursor fica no banco, então reiniciar não faz o rodízio
recomeçar sempre pelas mesmas casas.

> **A pegadinha do rodízio**: o que não foi olhado não pode expirar. As casas
> fora do ciclo não aparecem no snapshot, e um diff ingênuo marcaria **todas as
> ofertas delas como expiradas** — para "renascerem" no ciclo seguinte, poluindo
> histórico e furando a dedup (a oferta volta como "nova" e realerta).
>
> Isso vale em **dois níveis**, e `diff_offers` recebe um escopo para cada um:
>
> - `casas_raspadas` — as casas que o ciclo visitou. Uma casa que **falhou** na
>   captura também fica de fora: erro de rede não pode significar "acabou".
> - `escopo_detalhe` — os `(casa, evento)` cujo detalhe foi pedido. A listagem
>   vem inteira toda visita, então oferta de listagem que some, sumiu mesmo; já
>   o combo só existe no detalhe de um punhado de eventos por visita, e a
>   ausência dele não prova nada.
>
> Rede de segurança por cima dos dois: oferta com `valido_ate` no passado
> expira de qualquer jeito. Sem isso, um combo não sondado ficaria pendurado
> como ativo depois do jogo começar — e podia virar alerta de partida em
> andamento.

## Etapa 0 — Reconhecimento da Betano (resultado)

**Não precisou de Playwright.** A Betano roda na plataforma Kaizen Gaming, que
expõe uma API JSON pública espelhando cada rota do site em `/api/...`.

| Item | Achado |
|---|---|
| Endpoint base | `https://www.betano.bet.br/api/` |
| Método | `GET` |
| Autenticação | **Nenhuma** — sem login, sem cookie de sessão, sem token `x-*` |
| Resposta | `application/json` |
| Bloqueio | WAF com fingerprint de TLS (ver abaixo) |

### As Super Odds aparecem por duas vias distintas

**1. Mercado `MR12` — "Resultado Final SuperOdds"** (`typeId: 2850`)

É o 1X2 turbinado. Na listagem de jogos ele **substitui** o `MRES` normal, então
a listagem entrega só a odd turbinada — a original vem da página do evento, que
traz os dois mercados lado a lado.

```
Listagem:  GET /api/sport/{slug}/jogos-de-hoje/?req=la,s,stnf,c,mb
Evento:    GET /api/odds/{nome-do-evento}/{eventId}/?req=m,ms,s,c,stnf
```

A página do evento ainda confirma a promoção explicitamente:

```json
"marketOffersData": {
  "marketOffers": {
    "2887693171": [{ "offerTypeId": 1000, "text": "SuperOdds" }]
  }
}
```

Exemplo real capturado (Celtic – Dundee FC): `MRES` = 1.19 / 7.20 / 14.00,
`MR12` = 1.20 / 7.90 / 15.50.

**2. `/api/smart-picks` — combos de Criar Aposta turbinados**

Esses já trazem tudo no payload, sem request extra:

```
GET /api/smart-picks?sportId=1&includeTypes=0&includeTypes=1&includeTypes=2&includeTypes=4&includeTypes=5
```

```json
{ "type": 5, "data": { "percentageOffer": 20,
  "market": { "selections": [{ "originalPrice": 9.0, "boostedPrice": 10.8 }] } } }
```

`type` 4 = boost de 10%, `type` 5 = 20%. Os tipos 0/1/2 vêm sem `boostedPrice`
— são sugestões comuns, e o scraper descarta.

### Endpoints avaliados e descartados

| Endpoint | Por quê |
|---|---|
| `/api/offers/sportsbook/SuperOdds/27705/` | Só o texto de marketing e T&Cs da promoção — nenhuma odd |
| `/api/upcomingcoupon/?sid=FOOT&day=...` | Declara `marketTypes: ["MR12","MRES"]` no header, mas na prática nunca devolveu `MR12` |
| `/api/home/top-events-v2` | Traz `MR12`, mas só ~25 eventos em destaque e sem a odd original |

`jogos-de-hoje` cobre ~100 eventos de futebol numa requisição só, o que o torna
a varredura mais eficiente — bem melhor que percorrer as 186 ligas uma a uma.

### ⚠️ Fingerprint de TLS — por que não dá pra usar `httpx`

O WAF da Betano faz fingerprint do **handshake TLS**, não dos headers. A stack
TLS do Python leva `403` mesmo mandando bytes de header idênticos aos do curl:

| Cliente | Resultado |
|---|---|
| `curl` | 200 |
| `httpx` (HTTP/1.1 e HTTP/2, qualquer combinação de headers) | 403 |
| `urllib` stdlib | 200 no começo, **403 depois de algum volume** |
| `curl_cffi` com `impersonate="chrome"` | 200, estável |

Por isso o transporte é **`curl_cffi`**, que reproduz o handshake do Chrome.
A spec sugeria `httpx`; foi trocado porque simplesmente não passa.

**Não sobrescreva o `User-Agent`** — o `impersonate` já manda um coerente com o
fingerprint TLS, e um UA divergente do handshake é justamente o que entrega bot.

## Etapa 0b — Reconhecimento da Esportiva Bet (segunda casa)

A Esportiva roda o sportsbook da **Altenar**, cuja API de widget é pública —
mesmo perfil da Betano: sem login, sem token, sem quota.

| Item | Achado |
|---|---|
| Endpoint base | `https://sb2frontend-altenar2.biahosted.com/api/widget` |
| Integração | `integration=esportiva` (parâmetro fixo, veio do bundle do site) |
| Autenticação | **Nenhuma** |
| Esportes | `sportId` 66 Futebol, 67 Basquete, 68 Tênis (≠ dos ids da Betano) |

```
GET /api/widget/GetEvents        listagem por esporte (~2,3 MB, 889 eventos)
GET /api/widget/GetEventDetails  detalhe de um evento, com `boosts`
```

A resposta é **relacional**, não aninhada: vêm listas separadas de `events`,
`markets`, `odds`, `competitors` e `champs`, ligadas por id — por isso o parser
monta índices antes de qualquer coisa.

### As duas vias de boost espelham a Betano

| Betano | Altenar | traz a odd original? |
|---|---|---|
| mercado `MR12` | mercado turbinado na listagem | não — só no detalhe do evento |
| `/api/smart-picks` | array `boosts` no detalhe do evento | **sim**, as duas juntas |

⚠️ **Cada casa batiza o mercado turbinado do seu jeito** — "1x2 - Odds
Aumentadas" (Esportiva), "Vencedor do encontro - Odds Aumentadas" (BateuBet),
"Vencedor do encontro - Super Odds" (EstrelaBet, vupi). Por isso o filtro é uma
lista de marcas (`MARCAS_TURBINADO`), não uma string só. Casar apenas
`"aumentad"` fazia EstrelaBet e vupi devolverem **zero oferta**.

⚠️ **Em várias casas o boost só existe no detalhe.** 4Play e BetGorillas não
publicam nenhum mercado turbinado na listagem, mas têm dezenas de combos no
array `boosts`. Buscar detalhe só de quem "mostrou boost na listagem" zerava
essas casas também. Hoje quem tem mercado turbinado tem **prioridade** no
orçamento de detalhes, e o que sobra sonda os demais eventos.

⚠️ **E o boost não fica no topo da listagem.** Medido nos 40 primeiros jogos de
futebol:

| casa | índices com combo turbinado | o teto de 12 pega? |
|---|---|---|
| EstrelaBet | 0–6 | ✅ tudo |
| 4Play | 0–11 | ✅ tudo |
| **VaiDeBet** | **20–39** | ❌ **nada** |

A VaiDeBet tinha **77 combos** e devolvia zero oferta só por isso. Como cada
detalhe é caro, a saída foi varrer aos poucos: cada visita começa de um offset
diferente e avança dando a volta (`ALTENAR_JANELA_SONDA`). O log mostra o ponto
de partida — `VaiDeBet@24`.

O array `boosts` traz os dois preços no mesmo objeto:

```json
{"id": 15216009, "eventId": 16630676,
 "odds": [{"marketId": …, "selectionId": …}, …],
 "price": 1.3,
 "boostInfo": {"price": 1.5, "endDate": …, "betsLimit": 3330,
               "isWelcome": false}}
```

⚠️ `price` é a **original** e `boostInfo.price` a turbinada. O código não confia
nessa convenção: como o boost sempre *sobe* a odd, ele toma `min`/`max` dos dois
(`esportiva.py:214`). Inverter as duas faria todo edge sair invertido.

⚠️ `isWelcome: true` é **oferta de boas-vindas**, não boost recorrente — e o
array mistura as duas. É ela que produz os ganhos impossíveis: 1.71→50.00,
1.077→22.00, 1.95→19.00, sempre com `betsLimit` baixo. Medido em 2026-08-04:
todo ganho acima de +500% era `isWelcome`, nenhum boost normal passou de ~20%.
O parser descarta. Só serve pra conta nova (uma por CPF) e, como a fila de
avaliação ordena por `boost_pct`, deixá-la entrar faz a promoção inútil furar a
fila na frente da oferta real.

### Custo por ciclo

Cada detalhe de evento é **1 request**. O teto `ALTENAR_MAX_DETALHES` (12) é
**por casa**, e o rodízio limita quantas casas entram por ciclo — o custo é
`12 × CASAS_POR_CICLO`, não `12 × 10`. Medido: ~100s por ciclo com 3 casas,
dentro do intervalo padrão de 180s.

Vale o mesmo bloqueio por fingerprint de TLS, então o transporte é `curl_cffi`
com o mesmo `impersonate`.

### Isolamento de falha

Uma casa que quebra **não derruba as outras**: cada uma roda dentro de um `try`
que só emite `WARNING`. Dentro de cada casa, um esporte que falha não derruba os
outros dois. O ciclo só falha inteiro se todas as fontes falharem — mesma
política que já valia entre as duas fontes da Betano.

E, como dito acima, a casa que falhou **não entra em `casas_raspadas`**: sem
isso, uma queda de rede de 30 segundos expiraria o catálogo inteiro dela.

## Instalação

```bash
pip install -r requirements.txt
```

## Uso

```bash
python -m betano_superodds.main --once          # um ciclo e sai
python -m betano_superodds.main                 # loop, 180s (padrão)
python -m betano_superodds.main --interval 300  # loop, 5 min
python -m betano_superodds.main -v              # log de debug
python -m betano_superodds.main --sem-value     # só raspa, sem avaliar nem notificar
python -m betano_superodds.main --testar-telegram
```

Saída:

```
[13:27:03] NOVA: Celtic - Dundee FC — Resultado Final: Dundee FC — 14.00 → 15.50 (+10.7%)
[13:27:43] SEM MUDANÇA (195 ofertas ativas)
[13:32:44] EXPIRADA: Athletico-PR - Vitória — Resultado Final: Empate
```

Consultar o que foi capturado:

```bash
python query.py                     # ofertas ativas, maior ganho primeiro
python query.py --casas             # quantas ofertas cada casa tem
python query.py --casa estrela      # só de uma casa (aceita pedaço do nome)
python query.py --fonte mr12        # só as SuperOdds de Resultado Final
python query.py --min-ganho 15      # só boosts acima de 15%
python query.py --todas             # inclui as já expiradas
python query.py --historico         # últimas mudanças de odd registradas
python query.py --runs              # log dos ciclos de scraping
```

`--casas` é a visão mais útil com 11 casas no ar:

```
CASA                ATIVAS   TOTAL  MELHOR GANHO
--------------------------------------------------
Betano                 141     141        +20.0%
BateuBet               112     112        +20.0%
BetGorillas             47      47        +20.0%
EstrelaBet              34      34        +86.7%
Esportiva Bet           31      31        +47.1%
4Play                   22      22        +14.5%
vupi                    22      22        +86.7%
VaiDeBet                 9       9        +23.8%
```

Medido em 8 ciclos: **418 ofertas ativas**, contra 141 quando só a Betano rodava.
A VaiDeBet aparece baixa porque estava no meio da varredura — isolada, chega a
129. MultiBet, BetPix365 e JogoDeOuro não tinham promoção no ar (conferido: 30
eventos, 0 combos).

Testes:

```bash
python -m unittest discover -s tests -v
```

## Arquitetura

```
betano_superodds/
├── config.py     # endpoints, CASAS_ALTENAR, rodízio, impersonate, telegram
├── models.py     # dataclass Offer (casa, id estável, content_hash, ganho_pct)
├── scraper.py    # Betano: as duas fontes → lista de Offer normalizada
├── esportiva.py  # plataforma Altenar (10 casas) → mesma lista de Offer
├── diff.py       # snapshot novo vs. salvo → novas / alteradas / expiradas
├── storage.py    # SQLite: offers, offer_history, scrape_runs, alertas, estado
├── notifier.py   # push no Telegram (sem dependência nova)
├── alerts.py     # ponte diff → avaliação de value → alerta + resumo diário
├── dotenv.py     # leitor/escritor de .env, sem python-dotenv
└── main.py       # orquestra o loop

run.py            # porta da frente: --setup, --test, loop
query.py          # consulta o banco pela linha de comando
.env.example      # todas as variáveis, comentadas
```

### Identidade da oferta (desvio consciente da spec)

A spec propunha `id = hash(evento + mercado + odd_boost)`. Se a odd entrar no
id, um reajuste de boost vira **"oferta sumiu + oferta nova apareceu"** e o caso
"mudou de odd" — que a própria spec pede — nunca é detectável.

Então: `offer_id = sha1(casa + fonte + evento_id + mercado)` é a identidade
estável, e um `content_hash` separado (que inclui as odds) dispara o "alterada".

A **casa entra na identidade** justamente porque são duas: Betano e Esportiva
podem ter o mesmo jogo e o mesmo mercado, e são ofertas distintas, com odds
distintas e links distintos. Sem esse campo, uma sobrescreveria a outra no banco
e o diff acusaria "alterada" a cada ciclo, alternando entre as duas.

### Banco

- `offers` — estado atual, uma linha por oferta, com `active`, `first_seen`, `last_seen`
- `offer_history` — append-only: cada `nova` / `alterada` / `expirada` com as odds do momento
- `scrape_runs` — log de ciclos (contagens + erro), base do detector de quebra

## Detecção de quebra

Se `BREAKAGE_ALERT_AFTER_EMPTY_RUNS` (3) ciclos seguidos capturarem 0 ofertas ou
falharem, o log emite `ERROR` com "POSSÍVEL QUEBRA DO SCRAPER". A ideia é não
confundir "a Betano mudou a estrutura" com "não tem promoção no ar agora".

Uma fonte que quebra não derruba as outras — o ciclo só falha inteiro se *todas*
falharem.

## Validação feita

- Ciclo real: **195 ofertas** capturadas (12 `MR12` + 183 `smart-picks`), todas com `odd_original`
- Conferência independente contra a API (via `curl`, fora do código do scraper): **12/12 seleções `MR12` idênticas**, zero faltando, zero sobrando
- Segundo ciclo: `SEM MUDANÇA (195 ativas)` — sem duplicar linha nem inflar histórico
- 11 testes cobrindo novas / alteradas / expiradas / reaparecimento / detector de quebra

---

# Parte 2 — Avaliador de Value Bet

Pega uma oferta capturada e descobre se é value de verdade, comparando com a
odd justa de mercado — não só olhando se a odd está alta.

```bash
python avaliar.py                # ofertas ativas
python avaliar.py --fonte mr12   # só as SuperOdds de Resultado Final
python avaliar.py --todas        # inclui as já expiradas
```

```
Celtic - Dundee FC — Resultado Final: Celtic
  odd boost: 1.21 | odd justa: 1.22 (simples, de-vig, pinnacle) | edge: -0.8% | sem edge suficiente

Velez Sarsfield - CA Independiente — Total de Gols Mais de 1.5 + Escanteios Mais de 8.5 + Resultado Final 1
  odd boost: 7.26 | odd justa: 8.07 (combo, de-vig, pinnacle) | edge: -10.0% | sem edge suficiente

Athletico-PR - Vitória — Resultado Final: Vitória
  SEM MATCH (evento não encontrado na Pinnacle, pulado)
```

## Fonte da odd justa: Pinnacle raspada direto

Em vez de usar a The Odds API (quota de 500 req/mês), o scanner raspa a
Pinnacle direto — mesma abordagem da Betano. A API que alimenta o site dela é
pública:

| Item | Achado |
|---|---|
| Endpoint | `https://guest.api.arcadia.pinnacle.com/0.1/` |
| Auth | `x-api-key` estática, que vem no bundle JS do frontend — sem login |
| Formato das odds | **Americano** (`+256`, `-607`) — convertido pra decimal |
| Quota | Nenhuma |

```
/sports/{id}/matchups                    todos os jogos do esporte (1 request)
/matchups/{id}/related                   mercados derivados do jogo
/matchups/{id}/markets/related/straight   os preços
```

Esportes carregados (`value/config.py`): **29 futebol, 33 tênis, 4 basquete**.
Carregar só futebol descartava metade das pernas capturadas — as super odds da
Betano são tão de tênis quanto de futebol.

Ganho sobre a The Odds API, medido na recon:

| | The Odds API | Pinnacle direta |
|---|---|---|
| Ligas | ~45 | **161** |
| Copa do Brasil, Colômbia, Equador, Escócia | ✗ | ✓ |
| Mercados | h2h, totals, spreads, btts | + **escanteios, placar exato, HT/FT, dupla chance, e tudo de 1º tempo** |
| Quota | 500 req/mês | ilimitada |

O mesmo bloqueio por fingerprint TLS da Betano vale aqui — por isso `curl_cffi`.

### 1X2 de escanteios — derivado do handicap

A Pinnacle **não** publica moneyline de escanteios em jogo nenhum (conferido em
60). Publica o handicap, e dele o 1X2 sai por aritmética exata:

```
A = P(mandante cobre -0.5)   ->  P(mandante vence os escanteios) = A
B = P(mandante cobre +0.5)   ->  P(empate)  = B - A
                                 P(visitante) = 1 - B
```

Não é modelo — são dois preços observados e de-vigados, mesma natureza da
interpolação de linha. Sai marcado como estimada (herda o desconto de stake).

Só funciona quando o ladder passa pelo zero: em jogo desequilibrado a Pinnacle
publica de −3.0 pra baixo e não há ±0.5 pra ler. Escanteios aparecem em ~2 de 60
jogos, e metade desses não serve — então a cobertura real disso é ~1,5% dos
jogos.

**Validação (Fortaleza x Palmeiras, 2026-08-04):** a justa derivada deu 2.20
contra a odd **pré-boost da própria casa**, 1.94 — a diferença é a margem dela.
As três probabilidades somaram 1.0000 e P(empate) deu 10,4%. A odd turbinada
era 3.00, ou seja, o edge de +36% vem do boost de +54%, não da derivação.

## De-vig

Toda casa infla os preços pra que as probabilidades implícitas somem mais que
100%; a sobra é a margem. Sem remover isso, o edge sai sistematicamente menor
do que é de verdade. Exemplo real (Celtic x Dundee):

```
Pinnacle cru:  1.1647 / 8.52 / 13.94
implícitas:    0.8586 + 0.1174 + 0.0717 = 1.0477   -> margem 4.77%
normalizando:  0.8195 / 0.1120 / 0.0684 = 1.0000
odd justa:     1.22   / 8.93  / 14.60
```

Conferido à mão contra o pipeline: bate exatamente.

## Combo: por que o threshold é mais alto

Para combos, as probabilidades de-vigadas das pernas são multiplicadas. Isso
**assume que as pernas são independentes**, o que é falso no futebol — um jogo
com resultado mais definido tende a ter mais gols, mais escanteios e mais
chutes. As pernas são positivamente correlacionadas, então a probabilidade real
é **maior** que o produto, e a odd justa calculada sai **otimista** (baixa
demais), inflando o edge.

Por isso o threshold de combo é bem mais alto — e o output sempre marca
`combo`, pra deixar claro que é estimativa, não valor exato.

| | edge mínimo | teto da odd justa |
|---|---|---|
| simples | 5% (`EDGE_MIN_SIMPLES`) | 5.00 (`ODD_JUSTA_MAX`) |
| combo | 18% (`EDGE_MIN_COMBO`) | — |
| consenso de casas | 20% (`EDGE_MIN_CONSENSO`) | 5.00 |

O teto da justa é o que impede o feed de virar só zebra. Ele corta na odd
**justa**, não na ofertada: uma oferta de 7.00 com justa 4.80 continua
alertando, mas justa 8.29 com ofertada 11.00 não — desfecho com menos de 20% de
chance real fica de fora, por mais edge que apareça. Vale só no simples; no
combo a justa é o produto das pernas e sai alta por construção.

O teto também protege de um viés conhecido: o de-vig proporcional
(`remover_vig`) subestima a justa de zebra, e é por isso que a faixa de odd 8+
mostrava edge médio de +111% contra +11% na faixa até 5.

## Consenso entre casas — quando a Pinnacle não cobre

A Pinnacle é a referência do projeto porque é sharp. Só que, no futebol, **ela
publica apenas mercado de gol**: 1X2, handicap, totais, totais por equipe e um
punhado de specials. Conferido na API ao vivo — **nenhum prop de jogador**.

Isso deixava sem referência os mercados de chutes ao gol, cartões e artilheiro,
que são justamente onde mora boa parte das apostas reais. A oferta era
capturada e descartada.

Para esses mercados a referência passa a ser o **consenso das outras casas**:
de-vig do mesmo mercado casa a casa, e a **mediana** das probabilidades.

A matéria prima é a tabela `mercados_casa`, alimentada pelo mesmo
`GetEventDetails` que já era pedido — o payload sempre trouxe o evento inteiro
e o parser só lia as pernas turbinadas. Não custa request novo.

**Isto é referência mais fraca que a Pinnacle, e a diferença não é de grau.**
A Pinnacle vale porque tem dinheiro sharp corrigindo o preço; um consenso de
casas moles pode estar errado *junto*, e aí o edge mede desvio do rebanho, não
vantagem. Por isso o tratamento é o mesmo que o do modelo de placar: fonte
própria (`consenso`), threshold próprio (20%), desconto de stake próprio e
marcação obrigatória no alerta.

| decisão | por quê |
|---|---|
| mediana, não média | uma casa com preço maluco não pode arrastar a referência |
| de-vig por casa, antes de agregar | cada casa tem margem própria; agregar preço bruto misturaria margem com probabilidade |
| mínimo de 3 casas (`CONSENSO_MIN_CASAS`) | duas não são consenso, são desempate — e casas Altenar copiam catálogo |
| janela de 3h (`CONSENSO_JANELA_HORAS`) | o rodízio raspa 3 casas por ciclo; sem janela o consenso nunca formaria |
| a própria casa sai da conta | a casa avaliada não pode ser sua própria referência |

### ⚠️ As casas Altenar são um feed só

Medido em 20.669 seleções cotadas por 2+ casas: **90,3% têm preço idêntico.**

| par | seleções em comum | preço idêntico |
|---|---|---|
| Esportiva Bet × EstrelaBet | 18.385 | 89,9% |
| 4Play × EstrelaBet | 4.712 | 96,6% |
| 4Play × Esportiva Bet | 3.226 | 86,1% |

As dez casas Altenar são skins da mesma plataforma e publicam o mesmo preço.
**Três casas Altenar são uma fonte contada três vezes.** Por isso o alerta
mostra "consenso de N casas, M preço(s)" e avisa explicitamente quando M=1 —
sem isso, "3 casas" passaria por três opiniões independentes.

O cruzamento vira real quando entra casa de fora da Altenar (Betano,
CasaDeAposta). E esse é o caso que interessa: a Betano é quem cota chutes ao
gol, o mercado mais apostado do dono do projeto.

### Contraprova

O que decide se dá pra confiar no número: comparar as duas fontes num mercado
que **ambas** cobrem. Calibração de 2026-08-05 (11 pares de totais, 3 casas):

| | |
|---|---|
| viés médio | **+4.9%** (consenso pede odd maior que a Pinnacle) |
| erro absoluto mediano | 4.5% |
| p90 | 9.1% |

O viés positivo é a direção segura: justa alta demais **encolhe** o edge, então
o erro empurra pra não apostar. `EDGE_MIN_CONSENSO=20` fica bem acima do p90.
Amostra pequena e só de totais — reconferir quando mais casas cobrirem o mesmo
mercado.

### Número suspeito vai marcado, não some

Edge absurdo (acima de `EDGE_SUSPEITO_PCT`) e justa de consenso saem com selo
`⚠️` no alerta em vez de serem suprimidos: alerta que some não dá chance de
julgar. A guarda de `COMBINADOS` (`market_parser.py`) é a exceção e continua
recusando — ela não sinaliza número duvidoso, ela impede comparar a odd de um
mercado combinado contra a justa do 1X2 puro, que é medir a coisa errada.

## Matching de evento

Fuzzy nos dois times, depois de normalizar acento, sufixo de clube
("CR Flamengo" → `flamengo`) e sufixo de UF ("Athletico-PR" → `athletico`),
mais conferência de data.

O score de cada lado é `max(token_sort_ratio, token_set_ratio)`. O `sort`
sozinho pune token a mais como se fosse token errado, e era a **maior perda de
cobertura do sistema** — todos estes são o mesmo jogo, e eram rejeitados:

| oferta | Pinnacle | score antigo |
|---|---|---|
| SK Brann | Brann | 76,9 |
| Estudiantes LP | Estudiantes de La Plata | 75,7 |
| Vitória BA | Athletico Paranaense | 75,0 |
| Glasgow Rangers | Rangers | 63,6 |

Corrigido, o casamento subiu de 66% para 84% das ofertas que passam no parser,
**sem trocar nenhum match existente de jogo** (validado sobre 426 ofertas reais).

O score do par é o **mínimo** dos dois lados, não a média: "Flamengo vs
Palmeiras" contra "Flamengo vs Santos" tiraria ~75 na média e passaria no corte
de 80. Com o mínimo, o lado errado reprova o match inteiro. Abaixo do score
mínimo devolve `sem_match_evento` em vez de arriscar comparar jogo errado.

### Os dois vetos que aceitar subconjunto exige

Aceitar subconjunto e apagar a UF é exatamente o que apaga a diferença entre
homônimos. Sem estes dois, a mudança troca perda de cobertura por edge falso:

- **UF divergente** — Botafogo-SP e Botafogo-RJ viram o mesmo texto depois de
  normalizar, e podem jogar no mesmo dia (a janela de data não separa). Um lado
  sem UF não é conflito: a Pinnacle escreve "Vitoria" seco.
- **Gênero** — `RUIDO` come "women"/"feminino", então "Corinthians (F)" e
  "Corinthians" ficam idênticos. As casas marcam no **nome do time**; a Pinnacle
  marca no **nome da liga** ("WNBA", "…Women"). Por isso a comparação é
  assimétrica.

## ⚠️ Cobertura — leia antes de confiar no hit rate

Medido sobre as 621 pernas das 241 ofertas reais capturadas:

| | antes | agora |
|---|---|---|
| pernas mapeadas pra um mercado da Pinnacle | 124 = **20%** | 347 = **56%** |

O salto não veio de mercado novo na Pinnacle: veio de **parar de presumir**. A
versão anterior descartava por comentário quatro famílias que a API publica —
conferido bloco a bloco:

| descartado como "sem cobertura" | o que a API devolve |
|---|---|
| tênis (287 pernas) | sportId 33, `units: Games` e `Sets`, com total, handicap e vencedor por set |
| basquete (30 pernas) | sportId 4, `units: Points / Rebounds / Assists / Threes Made / Pts & Rebs & Asts`, com nome do jogador |
| handicap | `type: spread` — 22 blocos por jogo, o mais líquido da casa |
| total por equipe | `type: team_total`, com `side: home\|away` |

Só o filtro de mapeamento já jogava fora **31% dos blocos de preço recebidos**,
por não existir branch para `spread` nem `team_total`.

### O que continua sem referência — agora verificado, não presumido

Zero ocorrências em 3.372 matchups derivados de futebol; a única `category` de
special que a Pinnacle publica é `Team Props`:

**cartões, chutes no gol, faltas, impedimentos e artilheiro.** No tênis faltam
**aces**, **duplas faltas**, **tie-break**, **"resultado após N games"** e
**placar exato de set**; e o total de games do **set 2** não é publicado
pré-jogo (a API só traz período 0 e 1). No basquete falta a **cestinha do jogo**.

Quando uma perna não tem referência, o pipeline devolve `sem_odd_justa` em vez
de inventar número.

### O parser nasceu falando betanês

Em 2026-08-04 só **25,6%** das 857 ofertas ativas tinham todas as pernas
mapeadas. A causa maior não era falta de dado na Pinnacle — era **grafia**: o
parser foi escrito para a Betano e as 9 casas Altenar entraram depois
escrevendo diferente. `Total de Gols Mais de 2.5` passava;
`Total: Mais de 2.5`, o mesmo mercado, caía em "mercado não reconhecido".

| | antes | depois |
|---|---|---|
| ofertas com todas as pernas mapeadas | 219 = **25,6%** | 409 = **51,8%** |
| "mercado não reconhecido" | 456 | 139 |
| "mercado combinado num rótulo só" | 91 | 37 |

Três frentes, nenhuma delas mercado novo na Pinnacle:

1. **Grafia da Altenar** — `Total:`, `1 total:` / `2 total:` (mandante e
   visitante), `1x2: <time>`, `Chance dupla:`, `1º tempo - total:`. Também
   corrigiu um erro silencioso: `Vencedor do encontro: Atlético MG` virava
   `time_nome = "do encontro: Atlético MG"`, marcado como suportado e morrendo
   depois em "sem odd na Pinnacle".
2. **Especiais que a Pinnacle já publicava** — `Correct Score`,
   `Half-Time/Full-Time`, `Double Chance`, `Both Teams To Score/Winner`,
   `Exact Total Goals`. Estavam em `SPECIAL_KEYS` e eram carregados; faltava só
   o parser apontar pra lá. É o que tirou `1x2 e ambas equipes marcam` da lista
   de recusados: existe mercado próprio pra isso, com preço próprio.
3. **Recusas mais honestas** — `Qualq. Altura`, `Qualq. Momento`, `Primeiro` e
   `Último` são props de artilheiro (o `sv` da seleção traz `ls:player:NNN`), e
   `Mais de 0.5` sozinho é perna de bet-builder cujo nome de mercado não vem no
   payload. Não viram cobertura; param de ocupar a fila.

⚠️ A terceira frente é a mais importante e a menos óbvia. `Mais de 0.5 + Mais
de 0.5` é oferta real da Esportiva: **duas pernas de mercados diferentes com o
mesmo rótulo**. Lidas como "total de gols da partida acima de 0.5" dariam duas
probabilidades de ~0.95, justa ~1.1 contra uma paga de 5.0 — edge inventado.
Quando o rótulo não diz de que mercado é, a única saída correta é recusar.

### Chance dupla não é partição

Os três desfechos de `Double Chance` cobrem **dois** dos três resultados cada,
então as probabilidades implícitas somam ~2, não ~1. O de-vig normalizava pra 1,
via margem de ~109%, concluía "linha corrompida" e descartava o mercado — a
chance dupla nunca teria referência. `SOMA_ESPERADA` corrige. Confere contra o
1X2 do mesmo jogo: casa-ou-empate deu 1.4937 pelo mercado e 1.494 pelo 1X2.

### O modelo de placar — e por que ele nasce desligado

Os especiais acima resolvem uma minoria dos jogos: medido ao vivo, a Pinnacle
publica placar exato / HT/FT / gols exatos em **~1 de cada 12** jogos de
futebol, quase sempre liga grande. Nos outros ~90% ela publica 1X2 e over/under
em praticamente todos — e disso dá pra reconstruir a distribuição de placares
(`modelo_gols.py`: Poisson bivariado com correção de Dixon-Coles nos placares
baixos, dois λ ajustados por busca em grade contra o 1X2 e o over/under
de-vigados).

**Isso não é preço observado, é estimativa.** E dá pra medir o quanto ela erra
sem esperar jogo nenhum: naquele 1/12 de jogos os dois existem lado a lado.

```
python avaliar.py --calibrar-modelo
```

Calibração de 2026-08-04, 12 jogos, 198 desfechos — erro relativo da odd
derivada contra a real:

| mercado | mediana | p90 | máx |
|---|---|---|---|
| `correct_score` | 11,6% | 32,2% | 49,2% |
| `exact_goals` | 5,3% | 12,1% | 26,7% |
| `ht_ft` | 9,2% | 29,5% | 48,5% |

O recado é esse: **o threshold teria que ficar acima de ~32% só pra superar o
ruído do próprio modelo**. Por isso `EDGE_MIN_MODELO=35` e, mais importante,
`MODELO_ALERTA_ATIVO=0` — o modelo calcula, loga e grava, mas não alerta. Ele
serve hoje pra cobertura e estudo, não pra apostar.

As travas, todas em `value/config.py`:

| trava | papel |
|---|---|
| `fonte_odd = "modelo"` | basta **uma** perna derivada; odd de modelo nunca se mistura com preço observado |
| `EDGE_MIN_MODELO` (35%) | acima do p90 do erro medido |
| `STAKE_DESCONTO_MODELO` (0.4) | o desconto mais pesado da lista |
| `MODELO_ERRO_MAX` | se o ajuste não reproduz nem o 1X2 que o alimentou, não há modelo |
| `MODELO_ALERTA_ATIVO` (0) | a chave geral |

### Correlação: onde o modelo já paga pelo próprio custo

Combo era multiplicado assumindo pernas independentes — e no futebol elas não
são. Pior: **o erro é sempre na direção que infla o edge**. Com a matriz de
placares dá pra ler a conjunta de verdade. Dois casos reais de 2026-08-04:

| combo | justa por independência | justa corrigida | edge |
|---|---|---|---|
| `1x2: Empate + Total: Mais de 2.5` (favorito pesado) | 14,98 | **52,65** | +60,2% → −54,4% |
| `Total > 2.5 + Ambas não marcam + Escanteios > 9.5` | 8,39 | **24,25** | +90,7% → −34,0% |

Os dois eram EV negativo se passando por oportunidade. Empate com 3+ gols quer
dizer 2-2 ou 3-3; "muitos gols" com "alguém não marca" também se estorva.

Duas decisões de desenho valem registro:

- **Fica sempre a MENOR das duas.** Deixar o modelo *aumentar* a probabilidade
  seria trocar preço observado por estimativa em troca de um edge maior —
  exatamente o negócio errado.
- **A trava de resíduo aqui é mais folgada** (`MODELO_ERRO_MAX_CORRELACAO`), por
  assimetria de risco: criar um preço do nada exige rigor, mas *reduzir* uma
  conjunta que já se sabe superestimada é seguro mesmo com ajuste mediano.

A correção alcança o subconjunto de pernas que é função do placar (1X2, totais,
total por equipe, ambas marcam, gols exatos); escanteios e cartões seguem
multiplicados. Meia correção é melhor que nenhuma — no segundo caso acima, a
pior correlação estava justamente entre as duas pernas que a matriz cobre.

### Segunda camada: estatística do SofaScore (ligada em 2026-08-06)

Histórico de placar e desfalques **por cima** do edge de odds, nunca no lugar
dele — o mercado precifica o jogo melhor que qualquer estatística calculada por
fora. A camada roda só em oferta que já passou no filtro de confiança
(`alta`/`média-alta`/`média`), e o efeito é de **mão única**: rebaixa a
confiança um degrau, nunca sobe. Não recalcula edge nem stake, então não
consegue inventar value bet — no máximo desconfia de uma.

Dispara em dois sinais:

| sinal | o que é |
|---|---|
| `flag_noticia_fresca` | desfalque de titular (lesão ou suspensão) que o mercado pode ainda não ter precificado |
| `diverge_da_estimativa_independente` | a frequência conjunta REAL das pernas no histórico recente diverge ≥12pp do produto das marginais |

⚠️ **O stake sai calculado com a confiança ORIGINAL.** `stake.py` roda no passo
5 e o rebaixamento acontece no 6; por isso `confianca_original` fica gravado no
alerta. Quem lê precisa saber que o "apostar Xun" não reflete o rebaixamento.

**Duas limitações medidas ao ligar**, ambas de cobertura, não de correção:

- **Só futebol.** O filtro de candidatos exige `sport=football`, então tênis e
  basquete — a maior fatia da Betano — saem sempre com os campos em `None`.
- **A frequência conjunta quase nunca fecha.** Medido em 8 combos reais de
  futebol: **zero** calcularam. Quase todo combo do catálogo tem perna de
  escanteio, cartão ou prop de jogador, que estão fora do escopo de placar e
  derrubam a conjunta inteira (de propósito — meia amostra seria pior que
  nenhuma). Quem entrega valor hoje é o **desfalque**, que casou em 5 de 6
  eventos de futebol testados e achou lesão real de titular.

Ou seja: a metade "correlação de combo" da camada está ligada mas praticamente
inerte com o catálogo atual. Ampliá-la é trabalho de escopo (escanteios no
histórico), não de calibração.

Custo: **+7s num lote de 40 ofertas** com cache frio, porque só ~15% das
ofertas chegam ao passo que dispara a camada. Há cache em sqlite
(`sofascore_cache`), com TTL por tipo — id de time 7 dias, jogos 6h,
escalação 30min.

### Cobertura depende de quanto falta pro jogo

A Pinnacle só sobe os mercados derivados perto do início. Medido na listagem
de futebol (683 jogos raiz):

| falta pro jogo | com specials | com escanteios |
|---|---|---|
| 3–12h | 67% | 48% |
| 24–48h | 66% | 8% |
| 48h+ | **3%** | **0%** |

Como 83% da listagem está na última faixa, rodar o avaliador cedo demais faz
mercado existente parecer ausente. Não é quebra do scraper.

**Resultado da validação: 0 value bets em 241 ofertas.** Todos os edges deram
negativos (-0,8% a -28%). Faz sentido: o boost da Betano é aplicado em cima da
linha dela, que já tem margem — turbinar 1.19 → 1.20 não bate os 1.22 justos da
Pinnacle. E como a odd justa de combo é otimista, o edge real é ainda pior que o
mostrado.

## ⚠️ Mercado restrito × referência ampla — a família de bug mais cara

Duas vezes o mesmo erro de classe: casar um mercado **mais restrito** com a odd
justa de um mercado **mais amplo**. Sempre produz edge enorme e positivo, então
o alerta parece ótimo justamente quando está errado.

| caso | o que era casado | edge falso |
|---|---|---|
| `1x2 e ambas equipes marcam: Vélez e não` | odd justa do 1X2 puro | +130% |
| `Mirassol total de gols: Mais de 1.5` | odd justa do total da **partida** | **+141.8%** |

O segundo passou porque o total por equipe só era reconhecido **com hífen**
(`"Flamengo - Total de Gols"`, formato da Betano). A Altenar escreve
`"Mirassol total de gols: Mais de 1.5"`, sem hífen — e caía em `totals:1.5`, o
over 1.5 do jogo inteiro (justa 1.53) em vez do over 1.5 só do Mirassol
(justa 4.36). Odd de 3.70 contra 1.53 vira +141,8% e stake no teto de 5un.

Hoje as duas grafias são reconhecidas, com guarda para não confundir
qualificador de período com nome de equipe (`"1º tempo - total de gols"` é
total da partida no 1T, não de uma equipe chamada "1º tempo").

**Como pegar isso de novo:** compare a odd justa com a odd da casa. Casa tem
margem, então a justa é quase sempre **menor** que a paga. Uma justa muito
abaixo — 1.53 contra 3.70 — não é value, é mercado errado. Vale mais confiar
nesse cheiro do que no número do edge.

## Interpolação de linha

A Betano escolhe linhas que a Pinnacle nem sempre publica: ela oferece "Games no
Set Mais de 9.5" e a Pinnacle publica 8.5 e 10.5. Com as duas vizinhas em mãos,
`P(over)` na linha do meio sai por interpolação linear — é a maior causa isolada
de perna mapeada que mesmo assim fica sem odd.

Travas, porque isto é estimativa e não preço observado:

- só **entre** duas linhas publicadas — nunca extrapola pra fora do intervalo;
- intervalo máximo de `INTERPOLACAO_GAP_MAX` (2.0) entre elas;
- a linha exata, quando existe, sempre tem precedência;
- a saída marca `linha interpolada`, e `INTERPOLAR_LINHAS=0` desliga tudo.

## Arquitetura

```
betano_superodds/value/
├── config.py         # endpoints, thresholds, score mínimo de match
├── models.py         # Matchup, Market, conversão americano -> decimal
├── pinnacle.py       # scraper da Pinnacle
├── matcher.py        # fuzzy match de evento
├── market_parser.py  # texto do mercado -> pernas estruturadas
├── fair_odds.py      # de-vig + odd justa (simples/combo)
├── value_calc.py     # edge + is_value
└── pipeline.py       # avaliar_oferta()

avaliar.py            # roda o pipeline contra o banco
```

# Parte 3 — Rodando contínuo com alerta no Telegram

O loop agora faz o ciclo inteiro sozinho:

```
raspa → diff → salva → avalia value contra a Pinnacle → alerta no Telegram
```

## Configuração — `python run.py --setup`

```bash
pip install -r requirements.txt
python run.py --setup     # guiado: pede o token, acha o chat id, grava o .env
python run.py             # loop contínuo com alerta
```

### Windows — rodar em segundo plano (`bot.bat`)

Clique duas vezes no `bot.bat` para o menu, ou:

```
bot.bat iniciar     sobe em segundo plano, sem janela nenhuma
bot.bat parar       encerra (supervisor junto — só matar o python não adianta)
bot.bat status      diz se está rodando e mostra o fim do log
bot.bat log         acompanha o log ao vivo
bot.bat autostart   sobe sozinho no logon do Windows
bot.bat desativar   desliga o início automático
```

Não é um `python run.py` disfarçado: sobe um **supervisor** que reergue o bot se
o processo cair, com 30s de espera entre tentativas — seguro porque todo o
estado está no SQLite, e espaçado pra que um erro de configuração não vire um
loop de reinícios. Logs em `logs/bot-AAAA-MM-DD.log`, um por dia.

Dois detalhes que custaram teste: o Python da Microsoft Store roda como
**`python3.12.exe`**, não `python.exe` (por isso a detecção casa `python*` mais
o caminho do projeto), e o `_oculto.vbs` existe porque o Windows não tem jeito
nativo de rodar um `.bat` realmente invisível — `start /min` ainda deixa item na
barra de tarefas, e `pythonw.exe` esconderia a janela mas perderia o log.

O `--setup` faz tudo:

1. pede o token (o @BotFather te dá em `/newbot`);
2. valida no `getMe` e mostra o nome do bot;
3. pede pra você mandar qualquer mensagem pro bot e **descobre o `chat_id`
   sozinho** via `getUpdates` — sem ida ao @userinfobot, sem copiar número;
4. grava tudo no `.env` e manda uma mensagem de confirmação.

**Não há dependência nova** em nada disso: o notificador usa o `curl_cffi` que
já estava no projeto, e o leitor de `.env` é um módulo de ~80 linhas
(`dotenv.py`) em vez de `python-dotenv`.

### O `.env`

`.env.example` tem todas as chaves comentadas — `cp .env.example .env` se
preferir preencher na mão. O `.env` está no `.gitignore`.

Variável já exportada no ambiente **tem precedência** sobre o arquivo, então
Docker e systemd continuam mandando mais que o `.env` local.

Sem token o bot fica inerte e o loop segue normal, logando no console —
notificação é acessório e falta de credencial não pode derrubar a captura.

### Outros comandos

```bash
python run.py --test         # manda uma mensagem de teste e sai
python run.py --once         # um ciclo e sai
python run.py --sem-value    # só raspa, sem avaliar nem notificar
python run.py --interval 300 # loop de 5 min
python run.py -v             # log de debug

python testar_mensagens.py         # manda uma amostra de CADA tipo de mensagem
python testar_mensagens.py --seco  # só mostra no console, não envia
python testar_mensagens.py --so resumo   # filtra por nome da amostra
```

`testar_mensagens.py` chama as mesmas funções `TelegramNotifier.formatar_*` que
o loop usa — se alguém quebrar a formatação, ele quebra junto. Útil pra ver
como fica um alerta de combo ou o resumo diário sem esperar acontecer.

`python -m betano_superodds.main` continua funcionando com as mesmas flags —
`run.py` é a porta da frente, não uma substituição.

## O que chega no chat

**Alerta de value bet**, só quando o edge passa o threshold (5% simples / 18%
combo). Traz odd boost, odd justa, edge, **quanto apostar em unidades**, o jogo
que casou na Pinnacle e o link. Se alguma perna usou linha estimada, vem
marcado `⚠️ linha interpolada`.

```
🔥 VALUE BET
Carol Zhao - Magda Linette
Total de Games Mais de 17.5

odd boost: 1.95
odd justa: 1.79
edge: +9.1%  (mín. 5.0%)
💰 apostar: 2.25un (¼ Kelly)
```

**Resumo diário** às `RESUMO_DIARIO_HORA:RESUMO_DIARIO_MINUTO` (padrão 23:59,
pra fechar o dia inteiro). Existe porque value bet é raro por natureza — foram
**0 em 241 ofertas** na validação. Sem heartbeat não dá pra distinguir "nada bom
apareceu" de "o processo morreu de madrugada".

> **Por que existe `RESUMO_TOLERANCIA_MINUTOS` (30).** O disparo não é um
> agendador: `talvez_resumo_diario` só roda no fim de cada ciclo, e o ciclo leva
> ~100s + `BETANO_POLL_INTERVAL` de sono — **~4,6 min entre duas checagens**.
> Com alvo às 23:59 sobram 60s até o dia virar, então na maioria das noites a
> primeira checagem elegível cai já em **00:0x**. Simulado sobre 24h de
> checagens: os dois disparos caíram 00:03 e 00:02.
>
> Por isso o relatório é atribuído ao **dia que ele fecha**, não à data do
> relógio. Gravá-lo sob a data de hoje faria o relatório de hoje ser pulado à
> noite — um dia sim, um dia não. E a tolerância tem teto justamente pra que
> subir o bot ao meio-dia não dispare o resumo de ontem na hora.
>
> ⚠️ Em `main.py`, `talvez_resumo_diario()` roda **antes** de `acumular()`, e a
> ordem não é intercambiável: `acumular` zera o balde quando vira o dia do
> relógio, e acumular primeiro apagaria `avaliadas`/`values` do dia que o resumo
> está justamente reportando — o relatório sairia zerado, sem erro no log.

**Apostas do dia**, logo depois do resumo: as apostas cujo **jogo terminou** nas
últimas 24h, com green/red e o saldo em unidades.

```
📒 APOSTAS DO DIA (1/2)
━━━━━━━━━━━━━━━━━━━━━━

⚽ Fluminense - Vasco da Gama
↳ 1x2 e ambas equipes marcam: Fluminense e sim · odd 3.70 · ❌ -1.00un

⚽ Vancouver Whitecaps - Atlante FC
↳ Resultado Final: Atlante FC · odd 2.50 · ✅ +1.50un

⚽ Tigre - Belgrano
↳ Total de escanteios: Mais de 10.5 · odd 1.95 · ❔ não apurada

━━━━━━━━━━━━━━━━━━━━━━
✅ 5  ❌ 21  ❔ 3  ➖ 1
💰 Saldo: -12.40un
```

Ver [Liquidação](#liquidação--a-aposta-deu-green-ou-red) para o que está por trás.

**Alerta de quebra**, quando o detector dispara (`ALERTAR_QUEBRA=0` desliga).

## Liquidação — a aposta deu green ou red?

Até 2026-08-07 o bot parava no alerta e nunca olhava o resultado. Agora ele
busca o placar no SofaScore, resolve cada aposta e guarda o veredito, pro
resumo diário poder dizer o que aconteceu com o que ele recomendou.

### Cobertura: 41% → 88%, medido

Sobre as apostas reais alertadas, com o jogo já encerrado:

| | |
|---|---|
| resolvíveis só com a lógica de placar que já existia | 41% |
| **resolvíveis hoje** | **88%** |
| entre as apostas de **futebol** | 23 de 23 |

O que faltava não era placar — era estatística e nome de time. Três frentes:

1. **`/event/{id}/statistics`**, endpoint que o projeto não usava. Traz
   escanteios, cartões, chutes no gol e faltas, **separados por período**
   (`ALL`/`1ST`/`2ND`) — o que liberou escanteio, que sozinho era 9 das 32
   apostas não resolvíveis.
2. **Grafia real da casa.** `"vencer um dos tempos"` na primeira perna do combo
   e `"vencer uma dos tempos"` na segunda (6 de 13 ofertas da família). E
   `"2º tempo - ambas equipes marcam"`, que o `market_parser` recusa por não ter
   equivalente na Pinnacle mas que para *liquidar* é só subtrair o intervalo do
   final.
3. **Apelido de clube** — ver o desempate por horário, abaixo.

Os 12% que sobram são **tênis e basquete**, fora de escopo por construção (a
camada é só futebol), mais mercado de classificação (`"Para qualificar"`), que
não é função de uma partida só.

### A ideia que carrega o módulo: intervalo, não escalar

O SofaScore **omite a estatística que vale zero** — não existe item `Red cards`
quando não houve vermelho. Ausência é ambígua (0 ou não-reportado), e ler
ausência como 0 inventaria green.

Então nada é escalar: tudo é `[mínimo, máximo]`, e o veredito só sai quando é
**invariante sob o desconhecido**. Com 5 amarelos e vermelho ausente, o total é
`[5, ∞)`:

| aposta | veredito | por quê |
|---|---|---|
| mais de 4.5 cartões | ✅ green | já passou; o vermelho não muda |
| menos de 4.5 cartões | ❌ red | o mínimo já estourou a linha |
| menos de 6.5 cartões | ❔ **desconhecido** | depende do vermelho que não sei |

Não é política de arredondamento, é aritmética.

### Combo: AND de Kleene

1. partida não encerrada → `void` (adiada/cancelada **nunca** é derrota)
2. alguma perna confirmada falsa → `red`
3. senão alguma perna não resolvível → `desconhecido`, com o motivo no log
4. linha inteira batendo no número (over 2.0 com 2 gols) → `void` (push)
5. todas verdadeiras → `green`

A regra 2 antes da 3 significa que `(perna perdida, perna indefinida)` = `red`.
É sólido — `red` continua exigindo uma perna **confirmada** perdida, nunca
inferida — e cobre muito mais, porque combo quase sempre casa perna de gol
resolvível com perna de escanteio ou cartão. `LIQUIDACAO_RED_COM_PERNA_INDEFINIDA=0`
dá a semântica literal "uma perna indefinida derruba tudo pra desconhecido".

### Desempate por horário — o que autoriza afrouxar o nome

O matcher exige que os **dois** lados passem de `MATCH_MIN_SCORE` (80), e isso
derruba apelido consagrado. Medido em dado real:

| nosso nome | SofaScore | score |
|---|---|---|
| Hearts | Heart of Midlothian | **40** |
| FC Salzburgo | Red Bull Salzburg | **61,5** |
| Debrecen VSC | Debreceni VSC | 96 |

Eram 4 de 26 apostas perdidas por isso. O que autoriza afrouxar **só aqui** é
uma evidência que o matcher pré-jogo não tem: **um time joga no máximo uma
partida por horário**. Se um dos lados casa com folga *e* o apito bate no
minuto, a partida está determinada.

Continua sendo mais evidência, não menos: os vetos de UF (Botafogo-SP ×
Botafogo-RJ) e de gênero seguem valendo, e **dois candidatos no mesmo horário
fazem recusar** — ambiguidade não vira palpite. Validado: casou Benfica 6x1
Heart of Midlothian e Debreceni VSC 0x3 FC København corretamente.

### Onde roda, e por que não às 23:59

Incremental, `LIQUIDACAO_MAX_POR_CICLO` (3) partidas por ciclo, gravando cada
veredito na hora. **O relatório nunca faz rede** — só lê o banco.

O motivo é específico: `talvez_resumo_diario` grava `CHAVE_RESUMO` mesmo quando
o envio falha (proposital, pra não virar loop de reenvio). Um lote de ~5 min de
rede às 23:59 seria bloqueio no exato momento em que falhar é irrecuperável — o
relatório do dia se perderia de vez.

As partidas são agrupadas por **jogo normalizado**, não por oferta: as casas
Altenar publicam a mesma partida com grafias diferentes, e chavear pelo texto
cru faria a mesma busca fuzzy uma vez por casa.

### O que o saldo NÃO conta

| caso | por quê |
|---|---|
| veredito `desconhecido` ou `void` | não houve ganho nem perda |
| alerta sem stake gravada | anterior à migração; inventar a stake contradiria "o relatório registra o que foi recomendado" |
| stake abaixo do mínimo | o alerta saiu dizendo *"não vale a pena"* — creditar fabricaria histórico de aposta que o bot desaconselhou |

⚠️ **O P&L é hipotético.** O bot não sabe se você apostou nem quanto; ele assume
a stake de Kelly que a mensagem recomendou. É registro do sinal, não extrato.

### Conferir na mão

```bash
python -m betano_superodds.main --liquidar-agora      # liquida e imprime o P&L
python -m betano_superodds.main --liquidar-agora 20   # até 20 partidas
```

Existe porque a validação que pega **match errado** é conferir um veredito à mão
contra o placar no site — e match errado reporta P&L errado com confiança total,
que é o modo de falha caro aqui.

## Como o volume é contido

| trava | efeito |
|---|---|
| fila em `avaliacoes` | quem não coube no teto **continua pendente** e entra no ciclo seguinte |
| dedup por `offer_id` + edge (`Storage.ja_alertou`) | a mesma oferta alerta uma vez; só realerta se o edge melhorar ≥ `REALERTA_MELHORA_MIN_PP` sobre o melhor já alertado |
| janela `INTERVALO_MIN_REALERTA` (30min) | piso de tempo por cima da guarda de edge — realerta legítimo ainda espera essa janela |
| `MAX_REALERTAS_POR_OFERTA` (2) | teto de quantas vezes a MESMA oferta pode realertar, mesmo com melhora repetida |
| revalidação antes do envio | 1 request na casa confirma que a odd ainda existe (ver abaixo) |
| `MAX_AVALIACOES_POR_CICLO` (60) | ~200 ativas drenam em ~4 ciclos, sem estourar o WAF da Pinnacle |
| `REAVALIAR_APOS_MINUTOS` (60) | oferta parada volta pra fila: a linha da Pinnacle anda mesmo com a odd da Betano parada |
| `MAX_ALERTAS_POR_CICLO` (10) | se o cálculo quebrar e inflar edge em massa, não despeja centenas de mensagens |
| perna sem cobertura sai antes da rede | metade das ofertas tem perna exótica; elas custam 0 request |

Os tetos **logam o que ficou de fora** — truncar em silêncio faria "avaliei
tudo" parecer verdade quando não é.

## Revalidação: a odd ainda existe?

Um alerta saiu 12:21 com odd capturada 11:29 — a casa já tinha movido a linha
de escanteios de 9.5 pra 9 e o preço de 2.42 pra 1.70. Entrar no segundo em que
a mensagem chega não adiantava: a aposta não existia mais.

A defasagem é estrutural e **composta**:

    rodízio de casas (3 de 10 por ciclo)  ×  rodízio da janela de detalhes (@0→12→24→36)

A BetGorillas foi raspada 11:46, 12:00 e 12:14 e mesmo assim aquela oferta não
atualizou: o evento dela só cai na janela `@36`, vista 11:27 e depois só 12:33.
São **64 minutos** sem refresh. Não há frequência que resolva isso dentro do
orçamento de request — o que resolve é perguntar de novo, uma vez, na hora de
mandar a mensagem (`revalidacao.py`).

| veredito | o que acontece |
|---|---|
| `confirmada` | a odd bate com a guardada — alerta sai |
| `mudou` / `sumiu` | não alerta. Linha movida cai aqui: o rótulo deixa de bater, e com razão — não é a mesma aposta |
| `indeterminada` | rede falhou ou casa sem revalidação: **alerta sai mesmo assim**, porque value bet é rara e nem sempre volta. Só é barrado se a captura também estiver velha (`IDADE_MAX_PARA_ALERTA`) |

Só a Altenar revalida. A Betano é raspada **todo ciclo** — o dado dela nunca
passa de ~3 minutos, e confirmar custaria uma listagem inteira por esporte.

Alerta bloqueado **não volta pra fila**. `avaliacoes` é chaveada por
`content_hash`: quando a casa for reraspada e a odd mudar, a oferta fica
pendente sozinha. Forçar o retorno repetiria avaliação e revalidação a cada
ciclo contra a mesma odd velha, até a casa girar.

## A fila de avaliação

Não existe rota separada pra "oferta nova" e "oferta velha": quem define o
trabalho do ciclo é a tabela `avaliacoes`. Uma oferta está pendente quando
nunca foi avaliada **nesta versão** (`content_hash`) — o que cobre tanto a nova
quanto a que teve odd reajustada — ou quando a última avaliação passou do TTL.

Isso resolve um vazamento que existia: a varredura inicial anunciava "o resto
entra nos próximos ciclos" e não entrava, porque os ciclos seguintes só olhavam
o diff. ~137 ofertas ficavam órfãs, e havia value bet no meio delas.

Reiniciar é seguro: a fila retoma de onde parou e a dedup impede realerta.

> **Leia o [AVISOS.md](AVISOS.md)** antes de deixar rodando — ritmo, o que
> esperar no Telegram, e o que os números **não** garantem.

## Variáveis de ambiente

Todas podem ir no `.env` (veja `.env.example`) ou no ambiente.

| variável | padrão | o quê |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | token do @BotFather |
| `TELEGRAM_CHAT_ID` | — | destino das mensagens |
| `RESUMO_DIARIO_HORA` | `23` | hora local do heartbeat |
| `RESUMO_DIARIO_MINUTO` | `59` | minuto do heartbeat |
| `RESUMO_TOLERANCIA_MINUTOS` | `30` | atraso tolerado, inclusive cruzando a meia-noite |
| `LIQUIDACAO_MAX_POR_CICLO` | `3` | partidas liquidadas por ciclo |
| `LIQUIDACAO_ATRASO_MIN` | `150` | espera após o apito inicial pra liquidar |
| `LIQUIDACAO_MAX_TENTATIVAS` | `6` | tentativas de casar antes de desistir |
| `LIQUIDACAO_RED_COM_PERNA_INDEFINIDA` | `1` | perna perdida + perna indefinida = red |
| `ALERTAR_QUEBRA` | `1` | avisar quando o scraper quebrar |
| `MAX_AVALIACOES_POR_CICLO` | `60` | teto de ofertas avaliadas por ciclo |
| `REAVALIAR_APOS_MINUTOS` | `60` | quando uma oferta parada volta pra fila (`0` = uma vez só) |
| `MAX_ALERTAS_POR_CICLO` | `10` | teto de mensagens por ciclo |
| `BETANO_POLL_INTERVAL` | `180` | segundos entre ciclos (vale pra todas as casas) |
| `ENABLE_ALTENAR` | `1` | liga/desliga as 10 casas Altenar (`0` = só Betano) |
| `CASAS_ALTENAR` | *(vazio)* | slugs separados por vírgula; vazio = todas |
| `CASAS_POR_CICLO` | `3` | casas Altenar por ciclo, em rodízio (`0` = todas) |
| `ALTENAR_MAX_DETALHES` | `12` | eventos **por casa** que ganham request de detalhe |
| `ALTENAR_JANELA_SONDA` | `48` | profundidade da listagem varrida, uma fatia por visita |
| `ALTENAR_HORIZONTE_HORAS` | `24` | até quantas horas à frente vale gastar request de detalhe |
| `HORIZONTE_AVALIACAO_HORAS` | `12` | janela de kickoff que a fila de avaliação prioriza |
| `REAVALIAR_LONGE_MINUTOS` | `240` | TTL de reavaliação para jogo fora dessa janela |
| `MIN_ANTECEDENCIA_ALERTA` | `15` | abaixo disso o alerta sai marcado "em cima da hora" (`0` desliga) |

`ENABLE_ESPORTIVA` e `ESPORTIVA_MAX_DETALHES` são os nomes antigos e continuam
funcionando — a Altenar era uma casa só quando foram criados.
| `INTERPOLAR_LINHAS` | `1` | estimar linha entre duas publicadas |
| `EDGE_MIN_SIMPLES` / `EDGE_MIN_COMBO` | `5` / `18` | thresholds de value |
| `ODD_JUSTA_MAX` | `5.0` | teto da odd justa no simples (`0` desliga) |
| `EDGE_MIN_CONSENSO` | `20` | edge mínimo quando a justa vem das casas |
| `CONSENSO_MIN_CASAS` | `3` | casas necessárias pra formar consenso |
| `CONSENSO_JANELA_HORAS` | `3` | frescor do preço das outras casas |
| `EDGE_SUSPEITO_PCT` | `100` | acima disso o alerta sai com selo ⚠️ |
| `BOOST_ALTO_AVISO_PCT` | `100` | acima disso o alerta manda conferir o teto de aposta |
| `UNIDADE_PCT_BANCA` | `1.0` | quantos % da banca vale 1 unidade |
| `KELLY_FRACAO` | `0.25` | fração de Kelly (subir aumenta risco de ruína) |
| `STAKE_MAX_UNIDADES` | `5.0` | teto por aposta |
| `STAKE_DESCONTO_COMBO` | `0.5` | corte extra no combo (odd justa otimista) |
| `STATS_SOFASCORE_ATIVO` | `1` | segunda camada de stats (só rebaixa confiança) |
| `SOFASCORE_MIN_INTERVALO_S` | `0.4` | pausa entre requests no SofaScore |

## Stake — quanto apostar

O alerta calcula a aposta por **Kelly fracionado**, em unidades
(`value/stake.py`). 1un = 1% da banca; o que vale em dinheiro é decisão sua.

Usa-se **¼ de Kelly** porque Kelly cheio só é ótimo se a probabilidade estiver
certa — e a nossa é estimada, com erro de matching, de-vig, linha e (no combo)
independência assumida. Kelly é linear no edge, então edge inflado vira stake
inflada na mesma proporção.

| caso | boost | justa | Kelly cheio | recomendação |
|---|---|---|---|---|
| tênis simples | 1.95 | 1.79 | 9.4% | **2.25un** |
| favorito | 1.21 | 1.12 | **38.3%** | **5un** (teto) |
| azarão | 9.00 | 7.00 | 3.6% | **0.75un** |
| combo | 7.26 | 5.90 | 3.7% | **0.25un** |

Edge alto ≠ stake alta: o azarão tem +28,6% de edge e leva 0.75un; o favorito
com +8,0% bate no teto. Detalhes e travas no [AVISOS.md](AVISOS.md#4b).

## Pontos de atenção

- **Fragilidade**: é API não-oficial. Pode mudar sem aviso — daí o detector de quebra.
- **Rate limit**: padrão de 180s entre ciclos, no máximo 4 requests simultâneos, com jitter aleatório. O `403` que o `urllib` levou *depois de algum volume* mostra que o WAF escala a resposta: não baixe o intervalo sem necessidade.
- **ToS**: scraping de casa de apostas normalmente fere os termos de uso. Isto é para uso pessoal — não redistribua nem venda o dado.
