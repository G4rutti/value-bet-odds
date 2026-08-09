# AVISOS — o que esperar do bot rodando

Leia antes de deixar ligado e ir dormir.

---

## 1. O que acontece depois que você dá `python run.py`

Ele **não termina**. Fica de pé até você dar `Ctrl+C`. A tela vai ficar
parecida com isso pra sempre:

```
[16:29:06] telegram: ativo (resumo diário às 20h)
[16:29:12] polling a cada 180s — Ctrl+C para parar
[16:29:15] NOVA: CA Sarmiento - Independiente ... — 5.40 → 6.48 (+20.0%)
[16:29:15] ciclo: 198 ativas (+1 novas, ~3 alteradas, -1 expiradas)
[16:29:15] fila de avaliação: avaliando 4 oferta(s)
[16:29:15] value: 0 avaliada(s) de 4 candidata(s), 0 value bet(s)
        ← aqui ele dorme 180s e repete
```

**A cada 3 minutos** (`BETANO_POLL_INTERVAL=180`) ele repete o ciclo inteiro:

```
raspa a Betano + 3 casas Altenar → compara → salva → avalia value → alerta
```

Não precisa fazer nada. Se fechar o terminal, morre — veja a seção 7.

### As casas não são raspadas todas de uma vez

São **11 casas**, e raspar as 10 da Altenar todo ciclo estouraria o WAF. Então o
ciclo visita **3 por vez**, girando a lista — cada casa volta a cada ~12 min.
Você vai ver isso no log:

```
[16:29:12] casas Altenar neste ciclo: BetGorillas@0, vupi@12, BateuBet@24
[16:29:15] ciclo: 367 ativas (+148 novas, ~0 alteradas, -0 expiradas, 79 de casas fora do ciclo)
```

**"casas fora do ciclo" não é oferta perdida.** São ofertas ativas de casas que
ninguém visitou neste ciclo — elas continuam no banco, intactas, e voltam a ser
conferidas quando for a vez delas. É por isso que o número de ativas não cai
quando o rodízio gira.

**O número depois do `@` é a profundidade da varredura.** Dentro de cada casa, o
orçamento de detalhes só cobre uma fatia da listagem por visita, e o ponto de
partida gira. `vupi@12` quer dizer "nesta visita a vupi foi sondada a partir do
12º jogo". Isso existe porque em algumas casas o boost não fica no topo — na
VaiDeBet os combos só começam no 20º jogo. Uma casa leva ~4 visitas pra cobrir a
janela inteira.

`python query.py --casas` mostra quanto cada casa está contribuindo.

---

## 2. O ritmo, em números

| o quê | de quanto em quanto |
|---|---|
| ciclo de scraping | **180s** (`BETANO_POLL_INTERVAL`) — o ciclo em si leva ~100s |
| casas Altenar por ciclo | **3** de 10 (`CASAS_POR_CICLO`), em rodízio |
| cada casa volta a ser raspada | a cada **4 ciclos** (~12 min) |
| detalhes de evento por casa | até **12** (`ALTENAR_MAX_DETALHES`) |
| ofertas avaliadas por ciclo | até **60** (`MAX_AVALIACOES_POR_CICLO`) |
| reavaliação de uma oferta parada | **60 min** (`REAVALIAR_APOS_MINUTOS`) |
| alertas por ciclo | até **10** (`MAX_ALERTAS_POR_CICLO`) |
| resumo diário | **20h** (`RESUMO_DIARIO_HORA`) |

O ciclo levar ~100s dentro de uma janela de 180s significa que a folga não é
enorme. Se você subir `CASAS_POR_CICLO` ou `ALTENAR_MAX_DETALHES`, suba o
`BETANO_POLL_INTERVAL` junto — senão um ciclo começa a atropelar o seguinte.

### A fila

Com ~200 ofertas ativas e teto de 60 por ciclo, a primeira varredura leva
**~4 ciclos (12 min)** pra cobrir tudo. Você vai ver:

```
fila de avaliação: 203 pendentes, avaliando 60 neste ciclo; as outras 143 entram nos próximos
fila de avaliação: 143 pendentes, avaliando 60 neste ciclo; as outras 83 entram nos próximos
fila de avaliação: 83 pendentes, avaliando 60 neste ciclo; as outras 23 entram nos próximos
fila de avaliação: avaliando 23 oferta(s)
```

Depois disso a fila fica vazia e só entra o que é novo, o que teve odd
reajustada, ou o que passou de 60 min desde a última avaliação.

**Por que reavaliar oferta que não mudou:** a odd da Betano pode ficar parada
enquanto a linha da Pinnacle anda. É exatamente aí que um edge nasce sem nada
"mudar" do lado de cá. Ponha `REAVALIAR_APOS_MINUTOS=0` se quiser avaliar cada
versão uma vez só e gastar menos request.

---

## 3. O que chega no Telegram

**Alerta de value bet** — só quando o edge passa o mínimo (5% simples / 18%
combo). Traz odd boost, odd justa, edge, o jogo que casou na Pinnacle e o link.

**Resumo diário às 20h** — ofertas ativas, ciclos rodados, quantas foram
avaliadas, quantas value bets, melhor edge do dia.

**Alerta de quebra** — se 3 ciclos seguidos vierem vazios, provavelmente a
Betano mudou a API.

Nada mais. Oferta nova comum **não** vira mensagem: seriam ~200 por dia.

---

## 4. ⚠️ O bot vai ficar quieto. Isso é o esperado.

Na validação: **0 value bets em 241 ofertas.** Todos os edges negativos, de
-0,8% a -28%.

O motivo é estrutural: o boost da Betano é aplicado em cima da linha **dela**,
que já embute margem. Turbinar 1.19 → 1.20 não bate os 1.22 justos da Pinnacle.

**Dias sem nenhuma mensagem além do resumo das 20h são normais.** O resumo
existe justamente pra você distinguir "nada bom apareceu" de "o processo
morreu de madrugada". Se o resumo parar de chegar, aí sim tem algo errado.

---

## 4b. O stake — quanto apostar

O alerta traz uma linha assim:

```
💰 apostar: 2.25un (¼ Kelly)
🪙 apostar: 0.25un (¼ Kelly, combo, linha estimada)
💰 apostar: 5un (¼ Kelly, teto 5un)
```

**1 unidade = 1% da banca** (`UNIDADE_PCT_BANCA`). Quanto vale em dinheiro é
decisão sua — se a sua banca é R$ 1.000, 1un = R$ 10 e "2.25un" = R$ 22,50.
Usar unidade em vez de reais é o que faz a recomendação continuar válida
quando a banca cresce ou encolhe.

O cálculo é **Kelly**: a fração da banca que maximiza o crescimento no longo
prazo, dado o preço e a probabilidade real. Mas usamos **um quarto** dele
(`KELLY_FRACAO=0.25`), e isso não é conservadorismo gratuito:

> Kelly cheio só é ótimo se a probabilidade estiver **certa**. A nossa é
> estimada, e cada etapa injeta erro — matching fuzzy, de-vig proporcional,
> linha às vezes interpolada, combo assumindo independência. Kelly é linear no
> edge, então edge inflado vira stake inflada na mesma proporção.
> Superestimar `p` com Kelly cheio é a forma clássica de quebrar a banca
> *tendo* vantagem real.

### ⚠️ O teto de aposta da casa — o alerta não sabe qual é

Boost desproporcional (1.29 → 12.00, +830%) **não é generosidade**: é promoção
com aposta máxima baixa. A API da Altenar **não publica esse teto** — conferido
campo a campo, não existe nenhuma chave de limite no payload. O limite
provavelmente só é aplicado na hora de apostar, já logado.

O sistema grava o teto quando ele existe (`limite_aposta`), mas hoje isso é
sempre nulo. Como substituto honesto, boost acima de **+100%**
(`BOOST_ALTO_AVISO_PCT`) ganha um aviso no alerta:

```
💰 Apostar: 5un (¼ Kelly, teto 5un)
⚠️ boost de +830% — confira o limite de aposta no site antes
```

Quando esse aviso aparecer, **abra o link antes de calcular quanto apostar**. A
recomendação de 5 unidades pode ser impossível de executar.

### As travas

| trava | por quê |
|---|---|
| teto de **5un** (`STAKE_MAX_UNIDADES`) | odd 1.21 contra 1.12 justa pede **38% da banca** no Kelly cheio — o teto é a rede contra uma odd justa errada |
| **metade** no combo (`STAKE_DESCONTO_COMBO`) | a odd justa de combo é otimista por construção |
| **×0.75** com linha interpolada | a probabilidade foi estimada entre duas publicadas |
| arredonda **pra baixo** | errar pra menos custa retorno; pra mais custa banca |
| mínimo **0.25un** | abaixo disso o alerta diz "não vale a pena" |

Exemplos reais do cálculo:

| caso | boost | justa | edge | Kelly cheio | recomendação |
|---|---|---|---|---|---|
| tênis simples | 1.95 | 1.79 | +8.9% | 9.4% | **2.25un** |
| favorito | 1.21 | 1.12 | +8.0% | **38.3%** | **5un** (no teto) |
| azarão | 9.00 | 7.00 | +28.6% | 3.6% | **0.75un** |
| combo | 7.26 | 5.90 | +23.1% | 3.7% | **0.25un** |

Repare que **edge alto não significa stake alta**: o azarão tem edge de 28,6% e
leva 0.75un, enquanto o favorito com 8% leva o teto. Kelly pondera pelo risco,
não pelo edge sozinho.

Pra mudar a agressividade: `KELLY_FRACAO=0.5` no `.env` dobra tudo. **Não
recomendo** — veja a citação acima. `KELLY_FRACAO=0.125` deixa mais conservador.

## 4c. ⚠️ Edge gigante é motivo pra desconfiar, não pra apostar

Já aconteceu duas vezes de um alerta bonito ser bug. Os dois do mesmo tipo:
o sistema casou um mercado **restrito** com a odd justa de um mercado **amplo**.

O caso real mais recente: `Mirassol total de gols: Mais de 1.5`, pagando 3.70,
alertado com **+141,8%** e stake no teto de 5 unidades. O sistema tinha usado a
odd justa do total de gols da **partida inteira** (1.53) em vez do total só do
Mirassol (4.36). Com a correção, o mesmo alerta vira **−15%**.

**O cheiro:** a casa sempre embute margem, então a odd justa costuma ser um
pouco **abaixo** da paga. Justa 1.53 contra odd 3.70 não é uma pechincha — é
mercado errado. Antes de apostar num edge acima de ~30% em mercado simples:

1. abra o link e confira se o mercado no site é **exatamente** o do alerta;
2. desconfie de qualquer coisa que envolva "de uma equipe" — total de gols de
   um time, escanteios de um time, cartões de um time;
3. compare a odd justa com a paga: diferença absurda quase sempre é bug, não
   oportunidade.

Combo com edge alto tem outra causa, também documentada: a odd justa de combo é
otimista por assumir pernas independentes (seção 5).

## 5. ⚠️ O que o número NÃO garante

**Odd justa de combo é otimista.** As pernas são multiplicadas assumindo
independência, o que é falso: jogo com resultado definido tende a ter mais
gols, mais escanteios. A probabilidade real é maior que o produto, então a odd
justa sai baixa demais e o edge sai **inflado**. Por isso o mínimo de combo é
18% e não 5%. Trate combo como estimativa, não como preço.

**Linha interpolada é estimativa.** Quando a Betano oferece 9.5 e a Pinnacle
publica 8.5 e 10.5, o sistema estima o meio. O alerta vem marcado
`⚠️ linha interpolada`. Nunca extrapola fora do intervalo, e a linha exata
sempre tem precedência. `INTERPOLAR_LINHAS=0` desliga.

**O matching de evento é fuzzy.** Exige 80 de score nos dois times e janela de
18h. É conservador — prefere não casar a casar errado —, mas não é infalível.
O alerta mostra qual jogo casou na Pinnacle e com que score: **confira** antes
de apostar.

**Metade das ofertas nunca vai ser avaliada.** Cartões, chutes no gol, aces,
duplas faltas, artilheiro, tie-break — verificado na API, a Pinnacle não
publica nenhum desses. Aparecem como `sem cobertura` e é isso mesmo.

**A cobertura depende de quanto falta pro jogo.** A Pinnacle só sobe escanteios
e mercados derivados perto do início: 48h+ antes, só 3% dos jogos têm. Mercado
que "não existe" às vezes só ainda não subiu.

---

## 6. ⚠️ Fragilidade e limites

**As APIs são não-oficiais.** Podem mudar sem aviso. O detector de quebra só
dispara quando **todas** as fontes vêm vazias; uma casa sozinha que quebrar
aparece como `WARNING` no log e nada mais. Se a Pinnacle mudar, você vai ver
`sem_odd_justa` em massa.

**Casa com 0 ofertas nem sempre é defeito — mas desconfie.** MultiBet, BetPix365
e JogoDeOuro têm a API aberta e simplesmente não tinham promoção no ar quando
foram medidas (0 boost em 30 jogos conferidos, nenhum mercado turbinado). Isso é
informação, não erro.

Mas atenção: **zero também é o sintoma de bug**, porque "nenhuma oferta" não
gera erro nenhum no log. Três casas já devolveram zero por defeito nosso — nome
de mercado não reconhecido, boost só no detalhe, boost fora da janela sondada.
Se uma casa que costumava produzir zerar, `python query.py --casas` é o lugar de
ver, e a causa provável é a listagem dela ter mudado — não a promoção ter
acabado.

**Não baixe o `BETANO_POLL_INTERVAL`.** O WAF das casas escala a resposta ao
volume — o `urllib` levou 403 *depois de algum volume*, não de cara. 180s com
jitter e no máximo 4 requests simultâneos é o que foi validado. Com 11 casas o
volume por ciclo é bem maior do que quando esse número foi calibrado: se
aparecer `403` no log, o primeiro botão a mexer é `CASAS_POR_CICLO` para baixo,
não o intervalo para cima.

**Não sobrescreva o User-Agent.** O `impersonate` do `curl_cffi` já manda um
coerente com o fingerprint TLS; um UA divergente é justamente o que entrega bot.

**ToS.** Scraping de casa de apostas normalmente fere os termos de uso. Isto é
para uso pessoal — não redistribua nem venda o dado.

**Nada aqui é conselho de aposta.** O sistema calcula um edge estimado contra
uma referência. A decisão é sua, o risco é seu.

---

## 7. Parar, reiniciar, deixar rodando

`Ctrl+C` para. Fechar o terminal também mata o processo.

**Reiniciar é seguro.** Tudo que importa está no SQLite:

- as ofertas e o histórico;
- o que já foi avaliado (a fila retoma de onde parou);
- o que já foi alertado (**não realerta** o que já mandou);
- se o resumo do dia já saiu.

Reiniciar dez vezes não gera dez alertas da mesma oferta.

### Pra rodar sem terminal aberto (Windows) — use o `bot.bat`

Clique duas vezes no **`bot.bat`** e escolha no menu, ou pela linha de comando:

```
bot.bat iniciar     sobe em segundo plano, sem janela nenhuma
bot.bat parar       encerra
bot.bat status      diz se está rodando e mostra o fim do log
bot.bat log         acompanha o log ao vivo (Ctrl+C fecha só o log)
bot.bat autostart   sobe sozinho toda vez que você logar no Windows
bot.bat desativar   desliga o início automático
```

O `bot.bat iniciar` não é um `python run.py` disfarçado: ele sobe um
**supervisor** que reergue o bot se o processo morrer, esperando 30s entre
tentativas. Isso é seguro justamente porque reiniciar é seguro (seção acima) —
e a espera existe pra que um erro de configuração não vire milhares de
reinícios por minuto enchendo o disco.

Os logs ficam em `logs\bot-AAAA-MM-DD.log`, um por dia. Eles são a **única**
saída quando o bot roda oculto: não há janela pra olhar.

**Fechar a janela do menu não para o bot** — ele roda solto. Use `bot.bat parar`.

No Linux, um `systemd --user` ou `tmux`.

---

## 8. Quando algo parece errado

Pra ver como cada mensagem fica sem esperar acontecer:

```bash
python testar_mensagens.py --seco   # preview no console
python testar_mensagens.py          # manda uma amostra de cada tipo pro chat
```

| sintoma | provável causa |
|---|---|
| `telegram: INATIVO` | `.env` sem token/chat id — rode `python run.py --setup` |
| `HTTP 401 Unauthorized` | token errado ou revogado |
| `HTTP 400 chat not found` | `TELEGRAM_CHAT_ID` errado, ou você nunca falou com o bot |
| `POSSÍVEL QUEBRA DO SCRAPER` | 3 ciclos vazios em **todas** as fontes |
| `<Casa> falhou neste ciclo` | só aquela casa caiu; as outras seguem, nada dela expira |
| `N de casas fora do ciclo` | normal — é o rodízio, não perda de oferta |
| `sem_match_evento` em massa | jogos do banco já foram disputados, ou a Pinnacle não cobre a liga |
| `sem cobertura: X` | mercado que a Pinnacle não publica — normal, veja a seção 5 |
| tudo `sem_odd_justa` de repente | a Pinnacle pode ter mudado a estrutura |
| resumo das 20h não chegou | o processo caiu — veja a seção 7 |

Log detalhado: `python run.py -v`.
Conferir o banco sem mexer no loop: `python query.py` e `python avaliar.py`.

---

## 9. Histórico honesto deste projeto

Duas coisas que já estiveram erradas aqui e valem lembrança:

**A cobertura foi presumida, não medida.** A versão antiga descartava tênis,
basquete, handicap e totais por equipe com base em comentário no código. Os
quatro existem na Pinnacle. Foram 20% → 56% das pernas mapeadas só por medir em
vez de supor. Se for acrescentar algo em `SEM_COBERTURA`, **confira na API
primeiro**.

**A fila já foi um vazamento.** A varredura inicial dizia "o resto entra nos
próximos ciclos" e não entrava — os ciclos seguintes só olhavam novas e
alteradas, e ~137 ofertas ficavam órfãs pra sempre. Uma value bet de +9% estava
nesse buraco. Hoje a tabela `avaliacoes` é a fila, e o teto por ciclo é adiamento
de verdade, não descarte.
