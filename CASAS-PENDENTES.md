# Casas pendentes — não resolvidas, e por quê

**25 de 36 casas** não tiveram API pública confirmada no levantamento de
**2026-08-03**. Em 2026-08-04, uma sessão de captura de XHR em browser real
resolveu 6 delas e reclassificou 2 — ver [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real).
Em 2026-08-05, a Novibet e a EsportesDaSorte também foram resolvidas (a
Novibet: o erro genérico que parecia "faltam os IDs" era na verdade headers
de contexto faltando; a EsportesDaSorte: a rota certa não era nenhum dos
dois candidatos óbvios, era um esporte virtual à parte — ver as seções
próprias abaixo). Em 2026-08-20 a Lottu também foi resolvida (ver
[seção 8](#8-bet7k-e-lottu--casas-fora-do-levantamento-original-de-36-2026-08-20)).
**16 permanecem pendentes** de fato.

**Nenhuma está descartada.** "Pendente" aqui significa *não resolvido pelo
método usado* — que foi HTTP puro, sem browser. A maioria cai por falta de
captura de XHR real, não por bloqueio técnico comprovado.

Metodologia: [LEVANTAMENTO-CASAS.md](LEVANTAMENTO-CASAS.md) ·
As que passaram: [CASAS-MAPEADAS.md](CASAS-MAPEADAS.md)

---

## 0. Resolvidas em 2026-08-04 — captura de XHR em browser real

A previsão do levantamento original se confirmou: "nenhuma [pendente] foi
testada com captura de XHR de browser real, que é o método que resolveria a
maioria delas". Nesta sessão, 8 casas marcadas como baixa dificuldade foram
atacadas com `claude-in-chrome` (navegação real + inspeção de
`performance.getEntriesByType('resource')`, já que a API de rede da extensão
não capturou tudo de forma confiável). **6 resolvidas, 2 reclassificadas.**
Nenhum código foi alterado — isto é só o mapa de rota; falta implementar.

Todos os endpoints abaixo foram replayados fora do browser com `curl_cffi`
(`impersonate="chrome"`, mesmo transporte do scraper) para confirmar acesso
público. Onde marcado, uma visita prévia à página (para pegar cookie de
sessão anônima) foi necessária antes do endpoint aceitar a chamada.

| casa | plataforma real | precisa de cookie prévio? | implementada? |
|---|---|---|---|
| **CasaDeAposta** | própria (BFF Next.js) | sim | ✅ `casadeaposta.py` |
| **GingaBet** | **Altenar** — slug `gingabet.br` | não | ✅ `config.CASAS_ALTENAR` |
| **EsportesDaSorte** | SportingTech | não | ✅ `sportingtech.py` |
| **OleyBet** | SportingTech (mesma da EsportesDaSorte) | sim | ❌ mesma plataforma, cookie não testado |
| **Superbet** | própria, via Fastly | não | ❌ falta a rota da oferta turbinada |
| **Novibet** | própria (Angular + backend .NET, estilo SBTech) | não (só headers `x-gw-*`) | ✅ `novibet.py` |

> ⚠️ **Leia isto antes de estimar as 2 que faltam** (correção de 2026-08-05,
> Novibet e EsportesDaSorte resolvidas em 2026-08-05 — ver seções próprias
> abaixo).
>
> A tabela acima dizia "resolvidas", e a seção "Ordem sugerida" dizia que elas
> só precisavam de "um parser cada". **Não era o caso.** O que foi descoberto em
> 2026-08-04 é o *ponto de entrada da plataforma* — o feed geral de eventos e
> odds. Onde mora a **oferta turbinada** de cada uma continuava desconhecido, e
> era exatamente essa a parte difícil: na Betano e na Altenar o projeto sabe o
> endereço do boost (`smart-picks`, array `boosts` do `GetEventDetails`); nas
> 2 que sobraram (OleyBet, Superbet), ainda não.
>
> Verificado por HTTP em 2026-08-05, sem browser — **estes caminhos já foram
> tentados, não repita**:
>
> | casa | o que foi testado | resultado |
> |---|---|---|
> | Superbet | `/v2/pt-BR/events/by-date` (prematch e live) | `200`, **3.039 eventos** — mas só o mercado `preselected` (1X2 principal). Nenhuma tag de boost: as tags são `v2`, `pm_boostable_market`, `Pre-selected market`, `BPWM`, `Combinable`. `pm_boostable_market` é "pode entrar em bet-builder", não "está turbinada". |
> | Superbet | `/v2/pt-BR/struct` (2,4 MB) | `200`, zero ocorrências de `SUPER`, `uperodd`, `urbinad` ou `Melhorad`. A seção de super odds **não é uma competição/torneio** na estrutura. |
> | Superbet | `specialOfferIds` dos eventos | **0 de 3.039** eventos têm o campo preenchido. |
> | Superbet | `/v2/pt-BR/special-offers`, `/events/{id}/odds`, `/offers/special` | `404`. `/v2/pt-BR/events/{id}` responde `200` (32 KB, todos os mercados de um evento) — mas varrer 3.039 eventos um a um não é viável. |
> | EsportesDaSorte | `/api/generic/sportbet/getPopularOdds` (o "candidato mais provável" citado abaixo) | `400` sem os parâmetros certos; `200` com `languageId=23&deviceType=d` (achados lendo o bundle), mas devolve **"apostas populares"**, uma odd só por item, sem par original/turbinada. Não é a rota certa — ver seção própria abaixo. |
> | EsportesDaSorte | `/api-v2/today-sport-types/...` | `200`, mas devolve só a lista de esportes (<1 KB). Não tem odd. |
> | EsportesDaSorte | `betBoosterDataService` (parecia promissor pelo nome) | Widget de terceiro (**LVision**, domínio `betbooster-widget.lvision.io` achado no bundle) — bet builder calculado no cliente a partir do `fixture-detail` normal, sem endpoint de boost próprio. Descartado. |
> | Novibet | `/spt/feed/marketviews/location/v2/0/0/` | **`500 application/json`** — a rota está viva e engasgou nos IDs falsos. **Pista falsa**: o erro genérico (`{"code":5000,"message":"Object reference not set to an instance of an object."}`) voltava igual pra qualquer par de IDs, mesmo os corretos — a causa não eram os IDs, eram os headers `x-gw-*` faltando. Ver seção própria abaixo. |
> | Novibet | `/spt/feed/structure/v2/`, `/sports/v2/`, `/highlights/v2/`, `/coupon/v2/` | `404` — nomes de rota chutados errados. A rota certa é `/spt/feed/navigation/coupon/v2/<id1>/<id2>/`, achada só via captura de XHR real (ver abaixo). |
>
> **Conclusão:** as 2 que sobraram (OleyBet, Superbet) precisam da mesma
> captura de XHR em browser real que resolveu as 8 — desta vez navegando até
> a **seção de super odds / odds turbinadas** de cada casa, não até a home
> nem até um evento comum. Sondagem HTTP às cegas não converge; foi tentada
> e está registrada acima. A OleyBet roda a mesma plataforma da
> EsportesDaSorte (SportingTech) já resolvida — provavelmente é a mesma rota
> `esportes-super-odds`, só falta confirmar a captura e o cookie prévio que
> essa casa exige.

### Novibet — o erro genérico era header faltando, não ID errado

Resolvida em 2026-08-05, com uma volta atrás no diagnóstico: o levantamento
original tinha certeza de que faltavam os dois IDs numéricos do path
(`/spt/feed/marketviews/location/v2/<id1>/<id2>/`), porque `/0/0/` devolvia
`500`. Só que testar `/0/0/` de novo em 2026-08-05 — e também testar **os IDs
reais**, extraídos de uma captura de XHR do hub "Odds Turbinadas" feita pelo
usuário — deu o mesmo erro, byte a byte:

```
{"code":5000,"message":"Object reference not set to an instance of an object."}
```

IDs certos e IDs errados dando o mesmo erro é sinal de que o problema não é
o ID. Era: faltava um bloco de headers de contexto que o app sempre manda e
que nenhuma sondagem HTTP anterior tinha replicado —

```
x-gw-application-name: NoviBR
x-gw-channel: WebPC
x-gw-client-layout: Desktop
x-gw-client-timezone: America/Sao_Paulo
x-gw-cms-key: _BR
x-gw-country-sysname: BR
x-gw-currency-sysname: BRL
x-gw-domain-key: _BR
x-gw-language-sysname: pt-BR
x-gw-odds-representation: Decimal
```

Sem eles, o backend .NET quebra com `NullReferenceException` antes de sequer
olhar pros IDs da rota — daí o erro idêntico pra qualquer valor. Com eles,
**nenhum cookie é necessário** — confirmado com `curl_cffi` limpo, sem
sessão nenhuma.

O fluxo completo usa dois endpoints, nenhum dos dois documentado em lugar
nenhum — achados só por captura de XHR em browser real navegando até o hub
de turbinadas (`/apostas-esportivas/popular/6685003/...`):

```bash
# 1. árvore de navegação do hub: esportes -> competições
GET /spt/feed/navigation/coupon/v2/4324/6685003/
GET /spt/feed/navigation/coupon/v2/4324/6685003/<sportIds-separados-por-vírgula>

# 2. ofertas de uma competição (marketViewGroupId da árvore acima)
GET /spt/feed/marketviews/location/v2/4324/<marketViewGroupId>/
```

**Pegadinha da chamada 1**: sem repassar os IDs dos esportes de volta na
URL, a árvore lista os esportes mas com `subItems: []` — as competições só
aparecem quando os IDs são ecoados. Por isso são 2 chamadas: a primeira
descobre os esportes ativos no hub agora, a segunda pede a árvore completa.
`4324` é um id fixo da árvore de navegação do site (aparece também em
`/spt/feed/navigation/menu/4324`, o menu geral de esportes); `6685003` é o
`marketViewGroupId` do próprio hub "Odds Turbinadas"
(`marketViewGroupSysname: "SUPER_OOST"` na resposta confirma).

Cada oferta (`prebuiltTickets[]` da chamada 2) já vem com `price` (odd
original) e `boostedPrice` (odd turbinada) prontos — mesmo padrão da
CasaDeAposta, sem precisar de heurística pra decidir o que é boost.

Único cuidado extra: as respostas ficam em cache no Cloudflare por 5 min
(`cache-control: public, max-age=300, immutable`) — o app do site contorna
isso com um query param `&timestamp=<epoch>`; `novibet.py` faz o mesmo.

Implementada em `betano_superodds/novibet.py`. Rodada isolada em
2026-08-05 trouxe 81 ofertas turbinadas reais (Copa do Brasil, Liga Europa,
Liga Conferência, Leagues Cup, ATP/WTA, WNBA), batendo com o que a página
mostra na tela.

### EsportesDaSorte — as duas pistas óbvias eram widgets diferentes; a real é um "esporte" à parte

Resolvida em 2026-08-05. O candidato mais citado no levantamento original,
`/api/generic/sportbet/getPopularOdds`, foi lido direto do bundle Angular
(3,5 MB, **não é lazy-loaded** como o da Novibet — o app inteiro carrega de
cara, o que tornou a leitura de código bem mais produtiva que grep cego):

```
GET /api/generic/sportbet/getPopularOdds?languageId=23&deviceType=d
```

Funciona (`200`, sem cookie — os parâmetros que faltavam eram só esses
dois), mas é **"apostas populares/em alta"**: uma odd só por item, sem par
original/turbinada nenhum. Não é a rota. A segunda pista, um
`betBoosterDataService` que parecia muito promissor pelo nome, também não
era: é um widget de terceiro — achado o domínio
`betbooster-widget.lvision.io` no bundle, da **LVision**, fornecedora de
"bet builder" (o cliente monta a própria combinada com mercados do mesmo
jogo, calculado no browser a partir do `fixture-detail` normal). Nenhum
endpoint de boost por trás.

A oferta turbinada de verdade mora num **esporte virtual dedicado**,
`stN: "Super Odds"` (`stId 712`), achado só por captura de XHR em browser
real (usuário navegou até a seção "Odds Turbinadas" do site):

```
GET /api-v2/fixture/category-details/d/23/esportesdasortevip/null/false/ante/20/esportes-super-odds/football/multis
```

Essa URL **não segue o construtor genérico** que resolve `today-sport-types`
e outros irmãos (`prerenderService.getUrl`, que só cola valores de
`requestBody` no path) — usa um construtor irmão, `getOrderUrl`, que monta o
path a partir de `antePostEvent`/`betTypeGroupLimit` + `stN`/`cN`/`seaN`
(aqui fixos: `esportes-super-odds`/`football`/`multis`). Ler o código dos
dois construtores lado a lado foi o que decifrou a URL sem precisar chutar.

Sem cookie de sessão — mas **precisa** dos headers `origin` +
`sec-fetch-dest`/`sec-fetch-mode`/`sec-fetch-site`. Sem eles a rota responde
`200` só que com `{"success":false,...,"responseKey":"NO_DATA_FOUND"}` — um
"sucesso vazio" enganoso, não um erro que chama atenção.

Cada combo turbinado já vem com a odd atual em `hO`, e a odd original vem
**embutida no próprio nome do mercado** — ex.
`"Grêmio Para Ganhar & Carlos Vinicius Para Marcar a Qualquer Momento (Era 3.4)"`
com `hO: 4`. É o mesmo texto "(Era X)" que aparecia nos registros de aposta
do usuário nos CSVs de `csv_apostas/` — não era anotação manual, é texto
literal da API. Extraído por regex, sem heurística de percentual.

Implementada em `betano_superodds/sportingtech.py` (desenhada pra
compartilhar código com a OleyBet, mesma plataforma — só falta confirmar a
rota e o cookie prévio dela). Rodada isolada em 2026-08-05 trouxe 25 ofertas
turbinadas reais (Copa do Brasil, Liga Europa, jogos combinados de múltiplas
partidas), batendo com os nomes de mercado já vistos no histórico de apostas
do usuário.

### GingaBet — Altenar, slug `gingabet.br`

**A maior descoberta da lista.** Os 9 slugs testados no levantamento original
(`ginga`, `gingabet`, `gingabetbr`, ...) falharam porque nenhum tentou o slug
**com o ponto**: o app guarda suas chaves de widget no `localStorage` como
`WSDK_gingabet.br_betStakes` (WSDK = Widget SDK da Altenar), e `gingabet.br`
literal é o `integration` correto.

```bash
curl -s "https://sb2frontend-altenar2.biahosted.com/api/widget/GetEvents\
?integration=gingabet.br&culture=pt-BR&countryCode=BR&deviceType=1\
&numFormat=en-GB&timezoneOffset=-180&langId=1&sportId=66&champIds=&period=0"
```

`200 application/json`, ~2,4 MB, sem cookie. **Pronta para entrar em
`config.CASAS_ALTENAR` como 11ª linha** — é o caso trivial que o próprio
`config.py` documenta, mas a implementação fica para uma rodada seguinte.

### CasaDeAposta — API própria, first-party

O domínio real é `casadeapostas.bet.br` (não `casadeaposta.bet.br` — o nome
tem plural e o levantamento original não achou porque o DNS do domínio
chutado nem resolve). A API de odds não é a `next-client-api.bookmakernext.com`
mencionada antes — é **first-party**, no próprio domínio da casa:

```bash
GET https://casadeapostas.bet.br/api/odds/games
    ?startDate=<ISO>&endDate=<ISO>&languageId=21&gameMode=3
    &pageNumber=0&pageSize=5&sportId=1
    &marketTypeIds=17,1,1028,175,206,10,27,24,9,11,186,7,8,189
```

Sem login — mas precisa de um cookie de sessão anônima, obtido com um GET
simples em `/br/sports` antes (2 cookies, nenhum deles de autenticação).
Confirmado com `curl_cffi.Session`: `200`, JSON completo com odds e "SUPER
ODDS". Endpoints irmãos descobertos no mesmo host: `/api/odds/bettable-sports`,
`/api/odds/games-by-leagueid`, `/api/odds/game-periods`.

**Atualização de 2026-08-19 — implementada como 4ª família do pool de
consenso (`casadeaposta_livro.py`), NÃO como fonte de oferta.** Sondagem ao
vivo revelou 3 coisas que a entrada acima não sabia:

1. A lista de `marketTypeIds` acima (copiada de uma sessão anterior) estava
   incompleta — sem ela filtrada, o mesmo endpoint devolve um catálogo bem
   maior (até `openMarketCount`), incluindo escanteios e cartões. A
   implementação atual **não filtra por `marketTypeIds`** de propósito.
2. Sem esse filtro, ~450 dos 530 nomes de mercado distintos observados eram
   "**<Nome do Jogador> para marcar (incl. prolongamento)**" — um mercado de
   artilheiro por jogador por jogo, mais variantes tipo "Primeiro jogador a
   receber cartão", "Último marcador", "<Time> 1st player to score" e o
   catálogo `Jogador - *` (cartões/chutes, cada um com book de UM lado só ou
   patamares cumulativos não-complementares — mesma armadilha que a Superbet
   já documentou pro catálogo dela). `casadeaposta_livro.py` usa ALLOWLIST
   de nome de mercado (`_MERCADOS_SEGUROS`), não denylist, restrita a
   mercados de JOGO/TIME (1x2, Total, Dupla chance, Ambas Marcam, Escanteios
   1x2/handicap/total, Handicap, Ímpar/Par, Vencedor). O catálogo de
   artilheiro/cartão POR JOGADOR fica de fora — cobertura real, mas fica pra
   quem for confirmar com segurança o formato do book de cada um antes de
   ligar.
3. `startDate` na listagem vem SEM sufixo de fuso e é **UTC** (confirmado
   comparando kickoff de jogos do Brasileirão contra o horário real de bola
   rolando) — `models.to_utc_iso` trataria isso como hora LOCAL se recebido
   cru, por isso o scraper gruda `+00:00` antes de normalizar.

A listagem já traz `markets[].odds[]` completos — diferente da Superbet, não
precisa de request de detalhe por evento. `league`/nome de campeonato
continua sem endpoint conhecido (`bettable-sports` devolve `[]`,
`leagues`/`game-categories`/`tournaments` são 404) — `liga` fica `None` por
enquanto, mesma degradação que a Superbet já tinha quando `/struct` falhava.

### EsportesDaSorte + OleyBet — mesma plataforma confirmada (SportingTech)

A hipótese do levantamento original ("mesma estrutura de URL, provável mesma
plataforma") se confirmou: ambas rodam **SportingTech**, com API first-party
no próprio domínio e o mesmo formato de rota
`/api-v2/<endpoint>/d/23/<tenant>/<ids>/<payload-base64>`. Só o `tenant` muda:
`esportesdasortevip` numa, `oleybet` na outra.

```bash
GET https://esportesdasorte.bet.br/api-v2/today-sport-types/d/23/esportesdasortevip/24/1785812400000/eyJyZXF1ZXN0Qm9keSI6eyJ0aW1lUmFuZ2VJbkhvdXJzIjoyNCwic3RhcnREYXRlIjoxNzg1ODEyNDAwMDAwfX0=
GET https://oleybet.bet.br/api-v2/today-sport-types/d/23/oleybet/24/1785812400000/eyJyZXF1ZXN0Qm9keSI6eyJ0aW1lUmFuZ2VJbkhvdXJzIjoyNCwic3RhcnREYXRlIjoxNzg1ODEyNDAwMDAwfX0=
```

EsportesDaSorte responde `200` sem cookie nenhum. OleyBet exige a mesma
sessão anônima prévia que a CasaDeAposta (confirmado com
`curl_cffi.Session`). O payload base64 no final da URL é só
`{"requestBody":{"timeRangeInHours":24,"startDate":...}}` — não é
autenticação, é o corpo da consulta indexado na URL. Endpoints irmãos vistos
no mesmo host: `/api-v2/fixture/category-details`, `/api-v2/event-card`,
`/api-v2/league-card`, e `/api/generic/sportbet/getPopularOdds` (candidato
mais provável para o mercado turbinado, não testado a fundo ainda).

### Superbet — API de oferta pública confirmada, via Fastly

A hipótese do levantamento ("a Superbet tem API de oferta pública em outros
mercados") também se confirmou. `superbet-content.freetls.fastly.net` (única
pista anterior) é só CDN de conteúdo estático — a API de odds real fica em
outro host Fastly:

```bash
GET https://production-superbet-offer-br.freetls.fastly.net/v2/pt-BR/events/by-date\
    ?currentStatus=active&offerState=live&startDate=2026-08-04%2021:16:00
```

`200 application/json`, sem cookie, sem login. Devolve os eventos "SUPERODDS"
vistos na home. Endpoints irmãos: `/v2/pt-BR/struct`,
`/subscription/v2/pt-BR/structure`, `/subscription/events/live/count`.

### Novibet — achado de 2026-08-04, superado pela resolução completa

Esta foi a descoberta original (o `/spt/feed/...` só aparece na captura de
rede depois de navegar até um evento específico — no load da home as
chamadas já tinham estourado o buffer padrão de 250 entradas do
`PerformanceResourceTiming` do browser). Faltava o par `<id1>/<id2>` e o
motivo do erro genérico ao chutá-los. **Ambos resolvidos em 2026-08-05** —
ver a seção "Novibet — o erro genérico era header faltando, não ID errado"
na [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real),
que substitui esta.

### PixBet — reclassificada: não é rota errada, é outra plataforma

O levantamento original leu o `404` em `api.pixbet.com/api/v1/client` como
"rota errada, API viva". Na prática, esse host só cobre conta/carteira
(`.../player/wallet`, `.../player/settings`) — **não tem rota de esportes
nenhuma para acertar**. A engine de odds real é um provedor externo, **FSB
Tech** (`prod20383.fssb.io`), embutido via iframe com um `operatorToken`
obtido em `api.pixbet.com/api/v1/client/first/operatorToken`. Isso não é o
caso trivial "só faltou o path certo": é acesso mediado por token de sessão,
categoria mais parecida com a Betsson (motivo 1) do que com um endpoint
público direto. Fica pendente de fato — não é baixa dificuldade.

### BetVip — reclassificada: host e path certos, falta o ID dinâmico

A hipótese do Betby promofeed se confirmou, e o slug real apareceu direto no
bundle JS: não é `betvip`/`vip`/`betvipbr` (os testados antes), é
**`raeth4un`**. O path completo também saiu do bundle:

```
https://api-raeth4un-feed.sptpub.com/api/v1/promofeed/brand/${id}/pt-br
```

Mas `${id}` é um ID numérico (formato `23492983...`, ~19 dígitos) carregado
em runtime — não o slug do subdomínio. Testar com um ID de exemplo achado no
mesmo bundle devolveu `404` (rota existe, ID errado). Achar esse ID exige
rastrear de onde a config do app o injeta (provavelmente uma resposta de
`api-betvip-betbr.bs2bet.com`, não capturada nesta sessão) — mais trabalho do
que os outros itens desta lista, mas bem mais perto da solução do que antes.

---

## Resumo por motivo

| motivo | casas | dificuldade |
|---|---|---|
| **Plataforma mapeada, falta a rota da oferta TURBINADA** | **1** | **média — precisa de browser** |
| API existe mas exige auth/header ou token de sessão | 5 | média |
| Plataforma identificada, transporte é WebSocket | 2 | alta |
| Plataforma identificada, rota não descoberta | 2 | baixa |
| Config de API carregada em runtime | 7 | baixa/média |
| Anti-bot bloqueando antes da aplicação | 1 | muito alta |
| Grupo internacional, plataforma fechada | 2 | alta |

A primeira linha (EsportesDaSorte, OleyBet, Superbet) foi criada em
2026-08-05: elas estavam contadas como resolvidas, e não estavam — ver o
aviso na [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real).
A Novibet e a EsportesDaSorte também caíram nessa linha originalmente, mas
foram resolvidas de verdade em 2026-08-05 (ver seções próprias) e saíram da
contagem de pendentes. A OleyBet migrou pra "exige auth/header": a rota
(`esportes-super-odds`, mesma plataforma da EsportesDaSorte) já é conhecida,
só falta confirmar o cookie prévio que essa casa exige — não é mais "rota
desconhecida".

Total: **16** ainda pendentes (dos 25 originais, 6 resolvidos em 2026-08-04,
Novibet + EsportesDaSorte resolvidas em 2026-08-05 —
[seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real) — e
Lottu resolvida em 2026-08-20 — [seção 8](#8-bet7k-e-lottu--casas-fora-do-levantamento-original-de-36-2026-08-20)).
A Betsson aparece no motivo 1 e é citada de novo no motivo 6 como contexto de
grupo — está contada uma vez só. PixBet migrou do motivo 3 (rota não
descoberta) para o motivo 1 (token de sessão) depois da reclassificação.

---

## 1. API existe, mas exige autenticação ou header

Estas responderam com **erro de aplicação**, não de rede — prova de que a API
está viva e alcançável. O que falta é credencial ou cabeçalho.

| casa | endpoint testado | resposta | leitura |
|---|---|---|---|
| **Betsson** | `/api/sb/v1/sports` | `400` JSON `E_VALIDATION_INVALIDHEADER` | falta **um header**, não necessariamente login — é a mais promissora do grupo |
| **BolsaDeAposta** | `exchange.bolsadeaposta.bet.br/customer/api/` | `401` JSON | exige auth de verdade |
| **ReiDoPitaco** | `pitaco.bet.br/api/sports` | `401` | exige auth de verdade |
| **PixBet** | `prod20383.fssb.io` (FSB Tech, via iframe) | precisa de `operatorToken` de sessão | reclassificada em 2026-08-04 — ver [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real); não é mais "rota errada", é plataforma token-gated |

**Sobre a BolsaDeAposta**: é uma **exchange**, não uma casa tradicional. O preço
lá é formado por back/lay entre apostadores, sem margem de casa embutida — não
tem "odd turbinada" para raspar. Conceitualmente ela seria mais útil como
*referência de odd justa* (papel da Pinnacle hoje) do que como fonte de oferta.

**Próximo passo**: para a Betsson, descobrir qual header falta — provavelmente
um identificador de marca/site, visível no DevTools em uma request qualquer.

## 2. Plataforma identificada, mas o transporte é WebSocket

| casa | plataforma | evidência |
|---|---|---|
| **SeuBet** | BetConstruct / Swarm | `geoapi.bcapps.net`, `go.cmsbetconstruct.com` |
| **Ultrabet** | BetConstruct / Swarm | `cmsbetconstruct.com`, `api-tracking.ultra.bet.br` |

O Swarm entrega odds por **WSS com protocolo de subscrição**, não por `GET`
JSON. Dá para fazer, mas exige cliente de WebSocket, handshake de sessão e
manter assinatura viva — arquitetura diferente da do scraper atual, que é
stateless por ciclo. **Custo alto, prioridade baixa.**

Ultrabet, **Maxima** e **Suprema** são a mesma plataforma — resolver uma
entrega as três.

## 3. Plataforma identificada, rota não descoberta

Aqui a base de API já é conhecida. Falta só o caminho certo — resolvível em
minutos com o DevTools aberto.

**CasaDeAposta** saiu desta lista em 2026-08-04 (resolvida — era outro domínio
e outra API do que se supunha aqui; ver
[seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real)).
**BetVip** teve o slug do Betby confirmado (`raeth4un`, não os testados
abaixo) mas fica pendente por um ID dinâmico — mesma seção 0.

| casa | base de API encontrada | observação |
|---|---|---|
| **Betnacional** | `bet6.com.br` (SistemaNS, plataforma do grupo) | `account-history-svc-betnacional.bet6.com.br` |
| **BetVip** | Betby (`api-raeth4un-feed.sptpub.com`) + `api-betvip-betbr.bs2bet.com` | path e slug certos achados em 2026-08-04; falta o ID numérico de brand, carregado em runtime — ver seção 0 |

Sobre **Betby** (PixBet, BetVip, e também presente na BetGorillas): o feed
principal roda por WebSocket, mas há endpoints HTTP de promoção
(`api-...-feed.sptpub.com/api/v1/promofeed/brand/`) que podem servir justamente
para as ofertas turbinadas — confirmado no caso da BetVip em 2026-08-04.

## 4. Config de API carregada em runtime

O bundle JS não contém o endpoint como string literal: o app busca a config
depois de carregar. Só captura de XHR em browser real resolve.

**GingaBet, EsportesDaSorte, OleyBet, Superbet e Novibet saíram desta lista em
2026-08-04** — todas resolvidas com o método previsto (captura de XHR em
browser real). Ver [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real).

**Restam: IceBet, BravoBet, Panda, JogaJunto, VivaSorte, Betsul, KTO** —
ainda não atacadas com o mesmo método. (A Lottu saiu desta lista em
2026-08-20 — ver [seção 8](#8-bet7k-e-lottu--casas-fora-do-levantamento-original-de-36-2026-08-20).)

## 5. Anti-bot bloqueando antes da aplicação

| casa | resposta | leitura |
|---|---|---|
| **Bet365** | `403` HTML em `/SportsBook.API/web` | bloqueio na borda, antes da aplicação |

A Bet365 tem uma das defesas mais agressivas do setor. `curl_cffi` com
`impersonate` não passou. **Não recomendo insistir** — o custo é alto e o risco
de queimar o IP afeta as raspagens que já funcionam.

## 6. Grupo internacional, plataforma fechada

| casa | grupo | observação |
|---|---|---|
| **SportingBet** | Entain | `www.entaingroup.com`, `scmedia.sportingbet.com` |
| **BetMGM** | Entain / MGM | `cdn.betmgm.co.uk` — mesma stack da SportingBet |

SportingBet e BetMGM compartilham plataforma: resolver uma entrega as duas.

A **Betsson** também é grupo internacional, mas está no motivo 1 porque a API
dela respondeu — é um caso melhor que estes dois.

---

## 7. Fonte pendente DENTRO de uma casa já implementada — Betano multi-jogo

Não é casa nova: é uma família de super odd da Betano que o scraper não vê.

São as "Super Combinações Melhoradas" — um combo de 2-3 **jogos diferentes**
vendido como seleção única: `Todos ganham: Flavio Cobolli e Alexander Blockx e
Hamad Medjedovic` (4.30 → 5.40), `Talia Gibson e Madison Keys: Vencer sem perder
um set` (4.00 → 5.00). Aparecem sob pseudo-ligas do tipo `Montreal Masters
Especiais do dia` / `ATP Especiais-Super Combinações Melhoradas`.

**Já testado em 2026-08-05, não repita:**

| rota | resultado |
|---|---|
| `/api/smart-picks` com `includeTypes` **0–9** | `200`, mas **zero** picks com seleções de eventos diferentes — todo pick tem um `eventId` só. Os tipos existentes são 1, 4 e 5; a config atual (`0,1,2,4,5`) já cobre. |
| `/api/sport/{slug}/jogos-de-hoje/` (futebol, basquete, tênis) | zero ocorrências de `Especiais`, `Combina`, `Todos ganham` ou `Turbinada`. |
| `/api/especiais-apostas/` | **existe** (697 KB) mas é só mercado de longo prazo (campeão, posição final, prêmios) e as abas são só Futebol e Basquete — **tênis não tem**. |
| `/api/sport/tenis/especiais-apostas/`, `/api/promotions` | vazio / `404`. |

O `betBuilderBoost` que aparece em `/api/sport/tenis/` (com
`winningPercentage: 25.0`) é a campanha do badge "25% SUPER TURBINADA" — é
config de bet-builder, não a oferta pronta.

**Por que a captura não foi feita:** essas pseudo-ligas são **do dia**. Os cards
observados estavam marcados `04/08 15:22` e `04/08 15:34`; às 23h45 do mesmo dia
já não existiam em rota nenhuma. A captura de XHR precisa ser feita **com as
ofertas no ar**, durante o dia de torneio.

**Depois de achar a fonte**, ainda falta trabalho estrutural: hoje
`value/pipeline.avaliar_oferta` casa **um** evento por oferta, e estas abrangem
2-3. Ver o plano em `~/.claude/plans/` (item 3b) — `Leg` ganha o evento próprio,
o matcher ganha busca por participante, e `calcular_odd_justa` passa a aceitar
um matchup por perna. Nota boa: multiplicar probabilidades de jogos diferentes é
**legítimo** (são independentes de verdade), ao contrário do combo de mesmo jogo.

## Ordem sugerida, se for retomar

Itens 1–5 da lista anterior (GingaBet, PixBet, EsportesDaSorte, OleyBet,
CasaDeAposta) foram atacados em 2026-08-04 — ver
[seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real). Próxima
ordem, para quem for retomar:

1. ~~**Implementar o que já foi descoberto**~~ — **corrigido em 2026-08-05.**
   GingaBet, CasaDeAposta, **Novibet** e **EsportesDaSorte** foram
   implementadas e estão rodando. Restam OleyBet (mesma plataforma da
   EsportesDaSorte, só falta o cookie prévio) e Superbet (plataforma mapeada,
   rota da oferta turbinada ainda não achada — ver o aviso na
   [seção 0](#0-resolvidas-em-2026-08-04--captura-de-xhr-em-browser-real),
   que lista os caminhos HTTP já tentados e descartados; a próxima tentativa
   tem que ser com browser, navegando até a seção de super odds da casa).
2. **Betsson** — descobrir o header que falta.
3. **BetVip** — achar de onde vem o ID numérico do brand Betby.
4. **Betnacional** — base conhecida (`bet6.com.br`), falta mapear a rota.
5. **PixBet** — via FSB Tech, exige entender o fluxo do `operatorToken`.
6. O resto (IceBet, BravoBet, Panda, JogaJunto, VivaSorte, Betsul, KTO) —
   mesmo método de captura de XHR em browser real que resolveu a seção 0 (a
   Lottu saiu desta lista em 2026-08-20, ver seção 8).

O método que resolveu a seção 0 é reaplicável aqui: abrir a aba de esportes no
browser, ler `performance.getEntriesByType('resource')` (mais confiável que a
aba Network da extensão nesta sessão) e, se o host certo não aparecer de
primeira, buscar o bundle JS principal por trechos como `/api/` ou o nome de
plataformas conhecidas.

## Contexto importante antes de investir nisso

Vale lembrar por que mais casas importam: a validação deu **0 value bets em 241
ofertas**, e o motivo é estrutural — o boost sai em cima da linha da própria
casa, que já embute margem. Mais casas = mais volume = mais chance de um boost
furar a margem.

Mas as **10 casas Altenar já multiplicam o volume por ~5 sem código novo**.
Faz sentido ligar e medir isso primeiro — se o edge continuar negativo em todas
elas, a hipótese "mais volume resolve" fica enfraquecida, e caçar as pendentes
uma a uma passa a ser esforço com retorno duvidoso.

---

## 8. Bet7k e Lottu — investigadas a pedido do usuário (2026-08-20)

Bet7k **não** estava no levantamento original — nem no `LEVANTAMENTO-CASAS.md`,
nem em lugar nenhum deste doc. A Lottu já estava listada no motivo 4 ("config
carregada em runtime", ver a seção 4 acima) desde o levantamento original,
só nunca tinha sido atacada. As duas apareceram no histórico de apostas do
usuário (`csv_apostas/`) sem estar no roll, e foram investigadas via captura
de XHR em browser real a pedido dele. **Bet7k foi abandonada** depois de duas
rodadas sem convergir (decisão do usuário); **a Lottu foi resolvida e
implementada** (`betano_superodds/lottu.py`) — saiu da contagem de pendentes.

### Bet7k — FSB Tech, pública, endpoint de boost nunca fechado — ABANDONADA

Domínio real `7k.bet.br` (`bet7k.bet.br` redireciona). Sportsbook roda via
iframe **FSB Tech**, mesma plataforma da PixBet (motivo 1 da tabela acima),
tenant `kbet`:

```
https://prod20350-kbet-152319626.fssb.io/br-pt/spbk?operatorToken=logout
```

Diferente da PixBet, **`operatorToken=logout` é público de verdade** — não é
um token de sessão do usuário, é o valor literal que a FSB usa para sessão
anônima. Confirmado com `curl_cffi.Session` limpa, sem cookie nenhum: o GET
devolve os cookies `authorization`/`session` (JWT com `customerType:"anon"`,
`customerId:-1`) sozinho, sem precisar de warm-up em `7k.bet.br` primeiro.
`agentId=152319626` e `siteId=20350` (do JWT) identificam o tenant nos
endpoints abaixo.

Dois endpoints REST confirmados funcionando **fora do browser**, com
`curl_cffi impersonate="chrome"` puro:

```bash
GET /api/pulse/snapshot/events?lang=BR-PT
    # listagem completa (~2900 eventos), sem odds — Fixture + Outright,
    # campo Tags (só viu "FastMarket", nada de boost)

GET /api/eventpage/events/{eventId}?hideX25X75Selections=false
    # detalhe completo de 1 evento — TODOS os mercados/seleções com odds
    # (decimal + americana + fracionária), formato array posicional
    # compacto (não é objeto com chaves nomeadas), ~2,5MB por evento
```

A promoção "**Mega Impulso**" (badge `BB Mega Impulso`, visível na home em
`/esportes`) é confirmada como feature nativa da FSB — achada lendo o bundle
`index-BOZo7ChF.min.js` do iframe: enum `BoostedOdds=6`/`BoostedBetBuilder=7`,
tipo `tradingPromotionBoostedOdds`/`tradingPromotionBoostedBuilder`, com
campos `BoostType`/`BoostLevel`/`MaxStake`/`DbTrueOdds` (odd "verdadeira") vs
odd de exibição — ou seja, é exatamente o par original/turbinada que o
projeto precisa, não uma promoção de bônus de conta como a da Lottu abaixo.

**O que falta**: o evento de teste usado (Corinthians x Rosario Central, que
mostrava o badge na home) não tinha o campo de promoção populado no payload
de `/api/eventpage/events/{id}` — o texto "Boost" não aparece em lugar nenhum
da resposta. A rota que o app usa nativamente para isto,
`/api/pulse/markets?agentId&filter=EventId=="..."&lang&siteId` (achada lendo
`gAn`/`pAn`/`mAn` no bundle, e **confirmada no Network do browser real** com
essa URL exata, 200), **não responde igual fora do browser** — `curl_cffi`
com os mesmos headers/cookies devolve o shell HTML da SPA (mesmo
comportamento de uma rota inexistente), enquanto o browser recebe JSON.
Suspeita: WAF/anti-bot específico nesse path (é o mais pollado, candidato
natural a fingerprinting adicional além do TLS). Não testado ainda: replay
com `curl_cffi` a partir de uma sessão que primeiro passou por
`/api/eventpage/events/{id}` várias vezes (aquecer o padrão de tráfego) ou
capturar a chamada com um evento que tenha o Mega Impulso confirmado ativo
*no momento da captura* (o evento de teste pode ter perdido o boost entre a
navegação e a inspeção — combos turbinados saem do ar rápido).

**Segunda rodada (mesmo dia)**: tentado destravar `/api/pulse/markets` por
mais três ângulos, nenhum resolveu — (1) replicar a sequência completa de
chamadas que o browser faz antes de pedir markets (`snapshot/events` →
`sportscenter/carousels/events-with-items` → `master/match-tracker/list` →
`betslip/combo-bonus/bonuses` → `eventpage/events/{id}`), pra descartar um
gate por padrão de tráfego — sem efeito; (2) extrair do browser real quais
cookies existem além de `authorization`/`session` (achados `operatorToken` e
`events_updates`, ambos ausentes na sessão `curl_cffi`) e replicá-los —
sem efeito; (3) inspecionar os headers da resposta 200-fantasma: vem com
`x-powered-by: Express` e `content-type: text/html`, **a mesma assinatura**
de rota genuinamente inexistente vista nos testes de path chutado errado
(diferente do `/api/eventpage/...`, que sempre veio `application/json`).

Isso muda a leitura: pode não ser (só) WAF bloqueando um client não-browser
— é bem possível que `/api/pulse/markets` seja código mort/de outro
deployment da FSB (a rota `gAn`/`WBn` decodificada do bundle pode nem ser o
transporte usado de fato pela view "South American Plus"), e o Mega Impulso
esteja vindo por outro caminho que só aparece no primeiro paint (dado
embutido no HTML/SSR, não uma chamada de API separada). Não dá pra decidir
entre as duas hipóteses sem DevTools aberto por um humano num evento com o
boost confirmado ativo no momento — as ferramentas de captura automatizada
usadas nesta sessão (`read_network_requests`/`performance` via
`javascript_tool`) não conseguiram flagrar a chamada real nem uma vez,
mesmo com a URL exata já em mãos.

**Veredito final: abandonada** (decisão do usuário em 2026-08-20, depois da
segunda rodada não convergir). Fixture list e detalhe de evento com odds
cheias funcionam via `curl_cffi`, mas o endpoint do Mega Impulso nunca foi
fechado e não vale mais o esforço adicional de captura manual. Não retomar
sem motivo novo — se um dia valer a pena, o ponto de partida certo é
DevTools manual num evento com o boost confirmado ativo (ver o histórico
acima pra não repetir os caminhos já testados).

### Lottu — pública, GO — endpoint único pra toda a seção "Odds Turbinadas"

Domínio `lottu.bet.br` → `www.lottu.bet.br`. Plataforma **ngbras** (Angular
first-party + API própria), host de dados `alpha-sb.ngbras.com`. Todos os
endpoints abaixo são públicos — sem cookie, sem login — mas **exigem o header
`Origin: https://www.lottu.bet.br`**; sem ele, `400
{"code":3004,"message":"Missing parameters","info":{"parameter":"origin"}}`
(tem que ser header HTTP — mandar `origin` como query param não funciona).

**Primeira pista era falsa**: a promoção "Acelerador de Odds" (banner
principal da home) É um bônus de fidelidade — `bonus_type: "ODDS_BOOST"` em
`/promotion/available-sportsbook`, multiplicador automático por tamanho de
múltipla, sem odd original/turbinada fixa. Achado inicialmente e descartado
por não caber no schema `Offer`. **Mas existe uma seção separada**, achada só
pelo usuário passando a URL direto (`/s/CLE?group_type=GROUP&identifier=
oddsturbinadas`) — o menu "Odds Turbinadas" de verdade, com cards de combo
com odd original riscada → odd turbinada, exatamente o padrão do projeto.

O endpoint que alimenta essa tela (achado por tentativa de rota depois que a
extração de rede via browser falhou repetidas vezes nessa página — ver nota
de higiene abaixo) é:

```
GET https://alpha-sb.ngbras.com/event/highlights
```

Devolve TODOS os "highlights" ativos (32 na sondagem) num array plano, sem
paginação nem filtro por evento — cada item já é um combo pronto com odd
original e turbinada:

```json
{
  "_id": "6a85eb9e1d02191929637786",
  "date": "2026-08-20T22:29:50.000Z",
  "status": "NOT_STARTED",
  "country": "Brasil",
  "championship": "Odds Turbinadas - Brasileiro Série B",
  "question": "Athletic x CRB",
  "group": "oddsturbinadas",
  "title": "PRESSÃO ATHLETICANA!",
  "odds": {
    "answers": [{
      "answer": "Athletic Para Ganhar Um Dos Tempos & Athletic Para Ter o Maior Número de Escanteios",
      "answer_list": ["Athletic Para Ganhar Um Dos Tempos", "Athletic Para Ter o Maior Número de Escanteios"],
      "value": 4.75,
      "old_value": 3.97
    }]
  }
}
```

Mapeamento direto pro `Offer` (`ADAPTER_CONTRACT.md` §2): `_id` → evento_id;
`question` (formato `"A x B"`, precisa normalizar o `" x "` pro separador
que `matcher.split_times` reconhece) → evento; `championship` → liga;
`odds.answers[].answer` → mercado, com as pernas separadas por `&`
(**precisa converter pro `SEPARADOR_PERNAS` do projeto antes de montar
`mercado`** — mesma armadilha da SportingTech documentada no contrato,
§2.3); `old_value` → odd_original; `value` → odd_boost; `date` → kickoff
(replicar em `inicio_evento` e `valido_ate`, só uma data disponível).

**Campo `group` distingue dois tipos de turbinada** na sondagem: 28 itens
`"oddsturbinadas"` e 4 `"CasadinhasTurbinadas"` (mesmo formato de odds, só
outro rótulo — same-game combo). Nenhum outro valor de `group` visto; não dá
pra saber se o endpoint filtra por seção do site ou sempre devolve tudo — a
sondagem foi feita direto na URL da seção "Odds Turbinadas" e todos os 32
vieram junto, então por ora tratar os dois grupos como a mesma família de
oferta.

**Nota de higiene da sondagem**: a extração via `read_network_requests`/
`performance.getEntriesByType` no browser falhou consistentemente pra achar
esse endpoint nessa página específica (só capturava
`/event/live-summary`, usado pela sidebar "Partidas Populares", não pelo
conteúdo principal) — o endpoint certo só apareceu tentando variações
plausíveis de rota (`/event/highlights`) fora do browser depois que
`/widget/{identifier}` devolveu `401` (rota existe, exige auth — descartada)
em vez do `404` de rota inexistente. Fica registrado porque é um padrão
reaproveitável quando a captura de rede falhar: testar variações de rota
vizinhas às que já respondem, usando 404 vs "outro erro" pra distinguir rota
inexistente de rota real com barreira.

**Veredito: GO — implementada.** API pública, sem login, endpoint único e
simples (não precisa nem de sessão anônima como a Bet7k) — mais fácil que
qualquer outra casa já implementada. Adapter em `betano_superodds/lottu.py`
(Padrão A), `ENABLE_LOTTU` em `config.py`, wired em `main.py`, testes em
`tests/test_lottu.py`. Rodada isolada em 2026-08-20 trouxe 32 ofertas reais
(Copa Libertadores, Copa Sul-Americana, Brasileiro Série B).
