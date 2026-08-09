# Levantamento de casas — registro do que foi feito

Data: **2026-08-03**. Objetivo: descobrir, entre as **36 casas** onde há conta,
quais expõem API JSON pública acessível **sem login**, para expandir o scanner
além de Betano e Esportiva Bet.

Resultado em duas listas:

- [CASAS-MAPEADAS.md](CASAS-MAPEADAS.md) — **11 casas** acessíveis (10 novas)
- [CASAS-PENDENTES.md](CASAS-PENDENTES.md) — **25 casas** não resolvidas, com o motivo de cada uma

---

## Por que este levantamento existe

O critério de entrada de uma casa no scanner **nunca foi "tem odd turbinada"** —
quase toda casa tem. O que restringe é acesso técnico:

1. API JSON pública, sem login/token/quota (é o que dispensa Playwright);
2. a odd original obtível, junto no payload ou a 1 request de distância;
3. estrutura que caiba no pipeline `Offer` existente.

E há um limite econômico por cima dos três: os WAFs escalam bloqueio com volume,
então cada casa nova precisa pagar o próprio orçamento de requisições.

## A hipótese que guiou o trabalho

Casa de apostas BR raramente escreve o próprio sportsbook. A maioria é
**white-label de um punhado de plataformas** (Altenar, Kaizen, BetConstruct,
Betby, Digitain...). Então o levantamento foi feito **por plataforma, não por
marca**: identificar a engine resolve várias marcas de uma vez, e um parser por
plataforma serve todas as casas dela.

A hipótese se confirmou — foi exatamente o que produziu o resultado.

---

## Fase 1 — homepages e assinatura de plataforma

Fetch das 36 homepages com `curl_cffi` (`impersonate="chrome"`, o mesmo
transporte do scraper, pelo motivo de fingerprint de TLS já documentado no
README), extraindo hosts de terceiros e casando contra assinaturas de ~20
plataformas conhecidas.

**28 de 36 responderam.** 8 falharam em DNS porque o domínio não é
`<marca>.bet.br` como eu havia presumido.

O sinal útil não veio das assinaturas em si — veio dos **hosts de terceiros**
carregados pela página. `sb2wsdk-altenar2.biahosted.com` na 4Play e na
JogoDeOuro foi o primeiro indício de que a Altenar não estava só na Esportiva.

## Fase 1b — corrigir os 8 domínios

Resolução DNS de candidatos. Sete das oito usam a forma curta em `.bet.br`:

| casa | domínio presumido | domínio real |
|---|---|---|
| BateuBet | ~~bateubet.bet.br~~ | `bateu.bet.br` |
| BravoBet | ~~bravobet.bet.br~~ | `bravo.bet.br` |
| GingaBet | ~~gingabet.bet.br~~ | `ginga.bet.br` |
| IceBet | ~~icebet.bet.br~~ | `ice.bet.br` |
| MultiBet | ~~multibet.bet.br~~ | `multi.bet.br` |
| SeuBet | ~~seubet.bet.br~~ | `seu.bet.br` |
| Ultrabet | ~~ultrabet.bet.br~~ | `ultra.bet.br` |
| Panda | ~~pandabet.bet.br~~ | `pandabet.com.br` (não tem `.bet.br`) |

Também apareceram no caminho: PixBet é `pix.bet.br` e BetPix é
`betpix365.bet.br` — são casas **diferentes**, apesar do nome parecido.

## Fase 2 — bundles JS (o que deu errado)

Baixei os bundles JS de cada SPA e procurei nomes de plataforma. **Método
ruim**: os catálogos de cassino listam dezenas de estúdios cujos nomes colidem
com plataformas de sportsbook — "NSoft", "Vincent", "Uplay", "Salsa", "OpenBet"
apareceram em casas que não usam nenhuma delas.

Exemplo do ruído: a BetPix casou com *sete* plataformas ao mesmo tempo. Isso é
lista de fornecedor de cassino, não engine de esportes.

**Lição aplicada nas fases seguintes:** assinatura textual não vale nada;
o que vale é **host de rede real** e, acima de tudo, **resposta da API ao vivo**.

## Fase 3 — achar o slug `integration` da Altenar

A API da Altenar é multi-tenant: o mesmo endpoint serve todas as casas, e quem
seleciona a marca é o parâmetro `integration` (na Esportiva, `esportiva`).

Varredura de HTML + bundles em busca de `integration:"..."` e de hosts
`*.biahosted.com`, em 9 caminhos por site (`/`, `/sports`,
`/apostas-esportivas`, `/pt/sports`, `/ptb/bet/main`, ...).

Achou 4 slugs diretamente — `bateu`, `betpix365`, `esportiva`, `vaidebet` — e
os quatro responderam `HTTP 200` no `GetEvents` **sem nenhum header de auth**.

## Fase 4 — brute-force de slug + varredura de mercado turbinado

Como o slug costuma ser o nome da marca, testei candidatos óbvios para todas as
casas restantes, em 3 esportes (futebol 66, basquete 67, tênis 68), procurando
mercados com `aumentad|turbin|boost|super odd` no nome.

**Controle**: o slug inexistente `zzznaoexiste123` devolve 0 eventos — ou seja,
o teste discrimina de verdade, não devolve conteúdo genérico para qualquer
entrada.

**Resultado: 10 casas confirmadas na Altenar.**

## Fase 5 — combos (`boosts`) e o caso GingaBet

Para cada casa Altenar confirmada, amostrei 24 eventos e chamei
`GetEventDetails` procurando o array `boosts` — que é a fonte mais rica, porque
traz **as duas odds juntas** (original e turbinada) sem request extra.

Quatro casas tinham combos turbinados no ar no momento do teste: EstrelaBet
(28), BetGorillas (28), 4Play (22), vupi (16).

**GingaBet ficou de fora**: a home carrega `sb2wsdk-altenar2.biahosted.com`, mas
nenhum dos 9 slugs candidatos respondeu, e a varredura dos bundles não achou a
string `integration`. Provavelmente a config vem em runtime.

## Fase 6 — chutar rotas nas casas fora da Altenar (o que deu errado)

Testei ~45 endpoints candidatos (`/api/sports`, `/api/sportsbook/v1/sports`...)
nas casas de plataforma própria. **Quase tudo 404.** Chutar rota não escala.

Mas três respostas foram informativas, porque um erro *de aplicação* prova que a
API existe:

| casa | resposta | leitura |
|---|---|---|
| Betsson | `400` JSON `E_VALIDATION_INVALIDHEADER` | API viva, falta um header — não necessariamente login |
| BolsaDeAposta | `401` JSON | API viva, exige auth |
| ReiDoPitaco | `401` | idem |
| PixBet | `404` JSON `"The route sports could not be found."` | API viva e **respondendo JSON sem auth** — só a rota está errada |
| Bet365 | `403` HTML | anti-bot bloqueando antes da aplicação |

## Fase 7 — extrair bases de API reais dos bundles

Em vez de chutar rotas, extrair do bundle toda URL absoluta que pareça de API.
Isso rendeu as bases de verdade e fechou a classificação de plataforma das casas
que sobraram — inclusive descobrir que **SeuBet e Ultrabet são BetConstruct**
(`go.cmsbetconstruct.com`, `geoapi.bcapps.net`) e que **CasaDeAposta roda
BookmakerNext** (`next-client-api.bookmakernext.com`).

Para Novibet, Superbet, KTO, EsportesDaSorte, OleyBet e VivaSorte não rendeu
nada: são apps que carregam a config de API em runtime, e o endpoint não aparece
como string literal no bundle.

---

## O que o levantamento provou

**A Altenar é o atalho.** 10 das 36 casas rodam a mesma engine que a Esportiva
Bet, no mesmo endpoint, sem login — muda só um parâmetro de query. São
**9 casas novas sem escrever parser novo**.

Duas descobertas laterais que importam para a modelagem:

- **vupi é white-label da EstrelaBet** — mesmo backend, mas os feeds voltaram
  com contagens diferentes (1154 vs 1167 eventos) e boosts diferentes, então
  vale raspar as duas em vez de tratar como duplicata.
- **BetPix365 e VaiDeBet devolveram feed idêntico** (842 eventos / 4955 mercados
  no futebol) — mesma configuração de Altenar. Aqui a duplicata é real e a
  dedup por `offer_id` vai depender da casa estar na identidade (já está,
  `models.py:58`).

## Como reproduzir um teste

```bash
curl -s "https://sb2frontend-altenar2.biahosted.com/api/widget/GetEvents\
?integration=estrelabet&culture=pt-BR&countryCode=BR&deviceType=1\
&numFormat=en-GB&timezoneOffset=-180&langId=1&sportId=66&champIds=&period=0" \
  | python -c "import json,sys; d=json.load(sys.stdin); print(len(d['events']),'eventos')"
```

Trocar `integration` pelo slug da casa. Slug inválido devolve 0 eventos.

## Limites deste levantamento

- **É um retrato de um momento.** "Sem boost no ar" não é o mesmo que "não
  oferece boost" — quatro casas Altenar não tinham mercado turbinado durante o
  teste e podem ter amanhã.
- **A amostra de combos foi de 24 eventos por casa**, dos primeiros da listagem.
  `boosts=0` significa "não achei nesta amostra", não "não existe".
- **As pendentes não estão descartadas.** Nenhuma foi testada com captura de XHR
  de browser real, que é o método que resolveria a maioria delas.
- **ToS**: vale o mesmo aviso do resto do projeto — isto é uso pessoal, e
  scraping de casa de apostas normalmente fere os termos de uso.
