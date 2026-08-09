---
name: network-endpoint-recon
description: Processo pra descobrir a API JSON interna que alimenta o front de um site (Betano, Pinnacle, SofaScore, casas novas) e verificar se ela responde sem sessão logada. Use ao integrar uma casa de aposta nova, uma fonte de estatística nova, ou quando um endpoint conhecido parar de responder.
---

# Recon de endpoint interno

Todo site que mostra odds ou stats em tempo real busca esses dados de uma API
JSON própria. Ela quase sempre é pública (sem login, sem cookie) porque o front
roda no navegador de qualquer visitante. Achar essa API é mais barato e mais
estável do que raspar HTML.

## Etapa 1 — DevTools

1. Abrir a página **específica** que mostra o dado (a página do jogo, não a home).
2. F12 → aba **Network** → filtro **Fetch/XHR**.
3. Limpar o log e **navegar pela seção que você quer** (aba de escanteios, de
   H2H, de escalação). O request aparece no momento do clique — é assim que se
   sabe qual endpoint serve qual seção.
4. Clicar no request → **Response** pra confirmar que o JSON tem o dado, e
   **Headers** pra ver o que ele exige.

O que anotar de cada endpoint: URL com os path params identificados, headers
não-óbvios (`x-api-key`, `referer`, `origin`), e se a resposta muda com sessão.

## Etapa 2 — testar isolado, sem sessão

Copiar como cURL e rodar **sem os cookies**. Se responder igual, é pública.

Neste projeto o teste isolado sai melhor em Python, porque é o mesmo cliente que
o código vai usar em produção:

```python
from curl_cffi import requests as curl_requests

s = curl_requests.Session(impersonate="chrome")
r = s.get("https://api.exemplo.com/api/v1/event/12345", timeout=20)
print(r.status_code, r.text[:400])
```

`impersonate="chrome"` é obrigatório: vários desses hosts fecham em TLS
fingerprint antes de olhar header nenhum. `requests` puro toma 403 onde o
`curl_cffi` passa.

Se tomar 403 mesmo assim, tente nesta ordem: `Referer` do site, `Origin`,
`User-Agent`. Se só funcionar com cookie de sessão, **a fonte sai de escopo** —
reporte, não contorne com login automatizado.

## Etapa 3 — enumerar antes de mapear

Achar o endpoint é a parte fácil. A parte que engana é descobrir **onde mora o
dado que você quer**, porque o nome do mercado/seção no front raramente é a
chave do JSON.

Antes de escrever mapeamento, faça um passo de enumeração: puxe N objetos reais
e conte as chaves/descrições distintas.

```python
import collections
descs = collections.Counter()
for item in amostra:                       # 20-30 objetos reais bastam
    for bloco in item.get("blocos") or []:
        d = (bloco.get("special") or {}).get("description")
        if d:
            descs[d] += 1
for d, c in descs.most_common(120):
    print(f"{c:4d}  {d}")
```

A frequência é informação: o que aparece em quase todo objeto é mercado padrão;
o que aparece uma vez só costuma ser **parametrizado por entidade** — e
parametrizado por entidade não cabe em dict de match exato.

## Achado real — Pinnacle (2026-08)

`guest.api.arcadia.pinnacle.com/0.1`, `x-api-key` estática vinda do bundle JS
do front. Pública: sem login, sem cookie.

```
/sports/{id}/matchups                      todos os jogos
/matchups/{id}/related                     mercados derivados + participants
/matchups/{id}/markets/related/straight    os preços
```

⚠️ Preços vêm em **formato americano**.

A enumeração da Etapa 3 revelou o que o `SPECIAL_KEYS` (dict de match exato) não
podia representar — specials parametrizados pelo nome do time:

```
  8  Exact Total Goals                 ← padrão, em quase todo jogo
  7  Exact Total Goals 1st Half
  1  Aston Villa Goals                 ← parametrizado, 1 ocorrência por jogo
  1  Aston Villa Goals 1st Half
  1  Paris Saint-Germain To Score?
  1  Aston Villa To Win to Nil?
  1  Aston Villa Goals Odd/Even
  1  3-Way Handicap Paris Saint-Germain -2
```

Esses ficaram invisíveis pro bot por meses — não por ausência na API, mas por
ausência na tabela. Foi a causa raiz de um bug de edge fantasma (ver a skill
`value-bet-methodology`, seção 3). **Contagem 1 numa enumeração é o sinal.**

Conteúdo dos mercados, pra referência:

```
'Aston Villa Goals'          -> {'0': 2.59, '1': 2.59, '2': 5.06, '3': 13.38, '4': 40.68}
'Exact Total Goals 1st Half' -> {'0': 3.22, '1': 2.59, '2': 4.16, '3': 9.36, '4+': 20.54}
'Total Goals Range 1st Half' -> {'0 - 1': 1.4525, '2 - 3': 3.03, '4+': 20.54}
```

## Achado real — SofaScore (2026-08)

`api.sofascore.com/api/v1`. Pública: sem login, sem cookie, sem header
não-óbvio nenhum — `curl_cffi` com `impersonate="chrome"` e nada mais já
responde 200 (confirmado com 15 requests seguidos sem pausa). O host manda
`cache-control: max-age=60, public, s-maxage=7200`, ou seja, ele ESPERA ser
cacheado — sem cache aqui é o mesmo risco já pago com a Betano e a Pinnacle.

```
GET /search/all?q={termo}                    busca por nome (time, jogador...)
GET /team/{id}/events/next/{pagina}           próximos jogos do time
GET /team/{id}/events/last/{pagina}           jogos encerrados do time (forma recente)
GET /event/{id}/h2h                           confronto direto — só contagem agregada
GET /event/{id}/lineups                       escalação + desfalques
GET /event/{id}/managers                      técnico de cada lado
```

Os quatro usos pedidos (busca de evento por time/data, H2H, forma recente com
HT/FT, escalação/lesões) responderam sem sessão. Nenhuma feature saiu de
escopo por exigir login.

⚠️ **Armadilhas que não são óbvias no payload:**

- `/team/{id}/events/last/{pagina}` vem em ordem **crescente** de data — o
  jogo mais recente é o ÚLTIMO da lista, não o primeiro. Ler os primeiros N
  elementos como "os N mais recentes" pega os mais VELHOS da janela por
  engano. Sondado ao vivo: página 0 do Flamengo trouxe jogos de março a julho
  de 2026, do mais antigo pro mais novo.
- `/event/{id}/h2h` só devolve `teamDuel.{homeWins,awayWins,draws}` — uma
  contagem agregada, não a lista dos jogos. `/event/{id}/h2h/events` (o path
  óbvio pra tentar a lista) devolve 404: não existe. Pra forma recente com
  placar HT/FT usa-se `/team/{id}/events/last/*`, um time por vez.
- `homeScore`/`awayScore` têm `current` (inclui pênaltis, se houve),
  `normaltime` (só os 90 min) e `period1` (placar no intervalo). Usar
  `current` num jogo decidido nos pênaltis conta o placar do shootout como se
  fosse gol de jogo — `normaltime` é o campo certo pra qualquer estatística de
  placar.
- `/event/{id}/lineups` responde `missingPlayers` (desfalques/dúvidas) mesmo
  pra jogo **ainda não confirmado** (`confirmed: false`, dias antes do
  apito) — é o que torna a red flag de notícia fresca possível: a informação
  já está lá antes do jogo começar, que é justamente quando o mercado pode
  não ter precificado ainda. Cada entrada traz `type` ("missing"/"doubtful"),
  `reason` (código — `1` é lesão, `11`/`13` são cartão acumulado/vermelho),
  `description` em texto livre, e `proposedMarketValueRaw.value` (valor de
  mercado do jogador — único proxy disponível pra "é titular", já que o
  endpoint não devolve o elenco inteiro pra comparar).

Conteúdo de referência (evento real, Flamengo x Vitória, sondado 2026-08-06):

```
GET /event/{id}/lineups (jogo futuro, confirmed:false)
  home.missingPlayers:
    Nicolás de la Cruz — type=missing, reason=11, yellow_card_accumulation_suspension
    Luiz Araújo        — type=missing, reason=1,  Knee Injury
    Léo Pereira        — type=doubtful, reason=1, Unknown
  away.missingPlayers:
    Luan Cândido        — type=missing, reason=13, red_card_suspension
```

## Fora de escopo

Nada do SofaScore ficou fora por exigir sessão. O que ficou fora foi por
VOLUME/RISCO, não por acesso:

- **Troca de técnico recente.** `/event/{id}/managers` responde o técnico
  ATUAL de cada lado, mas "recente" exige saber o técnico ANTERIOR — e nada
  aqui persiste esse estado entre execuções. Fica documentado como endpoint
  disponível, não como feature implementada (ver `stats_check.py`).
- **Cartão / chute a gol / escanteio no histórico.** O endpoint de forma
  recente (`events/last`) só traz o placar (FT + HT), não incidentes por
  jogo — isso é outro endpoint (estatísticas por partida,
  `/event/{id}/statistics`, não sondado) e outro volume de request.

## Ao integrar uma casa nova

Ler `ADAPTER_CONTRACT.md` na raiz: o adapter tem que devolver `Offer` com o
contrato já estabelecido. E `CASAS-MAPEADAS.md` / `CASAS-PENDENTES.md` pro que
já foi levantado — 10 das 36 casas rodam Altenar e cabem no parser existente,
mas **casas Altenar compartilham o mesmo feed de preços**, então somar casas
Altenar não soma fontes independentes.

Critério de entrada de uma casa: **API pública sem login**. Ter odd turbinada
nunca foi o filtro.

## Higiene

- Não guarde credencial nem cookie de sessão no repo.
- Rate limit e cache desde o primeiro commit — a maioria desses hosts corta
  requisição agressiva, e aí a fonte some no pior momento.
- Endpoint interno não tem contrato: pode mudar sem aviso. Falha de parse deve
  virar log explícito, nunca zero silencioso — **casa zerada quase sempre é
  bug**, e 0 ofertas não gera erro sozinho.
