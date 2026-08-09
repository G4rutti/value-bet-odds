# Contrato de adapter de casa de apostas

Extraído lendo os seis adapters já implementados: `esportiva.py` (Altenar,
serve 11 casas), `novibet.py`, `casadeaposta.py`, `sportingtech.py`
(EsportesDaSorte), e `value/pinnacle.py` (não é uma casa — é a fonte de
referência, ver a seção final sobre por que ela NÃO segue este contrato).
GingaBet não tem adapter próprio: é uma linha em `config.CASAS_ALTENAR` que
reusa `esportiva.py` — ver "Padrão B" abaixo.

Isto documenta o que já existe, não inventa API nova. Onde os seis adapters
divergem, a decisão registrada aqui é a que já apareceu mais de uma vez ou a
que tem teste de regressão comprovando por quê.

## 1. Dois esquemas normalizados — não confundir

O projeto tem **dois** schemas normalizados, para propósitos opostos:

| | `Offer` (`betano_superodds/models.py`) | `Market`/`Matchup` (`betano_superodds/value/models.py`) |
|---|---|---|
| Representa | uma oferta turbinada de UMA casa | o preço de referência da Pinnacle (sharp) |
| Quem produz | os adapters deste contrato | só `value/pinnacle.py` |
| Cardinalidade | 1 por combo/seleção turbinada | 1 `Matchup` por jogo, N `Market` dentro |

**Um adapter de casa de apostas só produz `Offer`.** Nunca `Market`/`Matchup`
— isso é exclusivo da Pinnacle. Confundir os dois foi a causa dos bugs mais
caros já registrados no projeto (comparar oferta de mercado restrito com odd
justa de mercado amplo — ver `market_parser.py`, comentários sobre o falso
+141,8% do Mirassol).

## 2. O schema `Offer`

```python
Offer(
    fonte: str,            # identifica a VIA dentro da casa — ver §2.1
    evento_id: str,        # id ESTÁVEL do jogo na casa (nunca o texto do nome)
    evento: str,           # "Time A - Time B" — separador exigido pelo matcher, ver §2.2
    mercado: str,          # pernas juntas por " + " (SEPARADOR_PERNAS) — ver §2.3
    odd_original: float | None,
    odd_boost: float,      # SEMPRE > odd_original quando odd_original existe
    url: str,
    liga: str | None = None,
    valido_ate: str | None = None,     # ISO — normalizado por __post_init__
    inicio_evento: str | None = None,  # KICKOFF real, não fim de promoção — ver §2.4
    boost_pct: float | None = None,    # se a casa não manda, calcule: (boost/original - 1) * 100
    casa: str = "Betano",              # nome de exibição — obrigatório informar p/ casa nova
    limite_aposta: float | None = None,
)
```

Campos derivados automaticamente, não preencher:
`offer_id` (hash de `casa|fonte|evento_id|mercado`), `content_hash` (muda com
as odds — dispara reavaliação), `alert_hash` (dedup de alerta, não usa
`odd_original`), `ganho_pct`, `capturado_em`.

### 2.1 `fonte`

Identifica de qual mecanismo dentro da casa a oferta veio (uma casa pode ter
mais de um: a Altenar tem `_1x2` e `_boost`, a Betano tem `mr12` e
`smartpick`). Convenção: `"{slug_da_casa}_{mecanismo}"` em minúsculas. Isto
entra em `offer_id`, então **renomear quebra o histórico** — ofertas antigas
viram "órfãs" no banco (ver comentário em `test_esportiva.py:379`).

### 2.2 `evento`

Formato `"Mandante - Visitante"`. O matcher (`value/matcher.py:split_times`)
só reconhece um conjunto fixo de separadores (` - `, ` vs `, ` x `, ` v `,
en/em dash) — se a casa manda `"A vs. B"` (Altenar) ou objetos de participante
separados, o adapter tem que montar a string normalizada, não repassar o
formato cru. Ver `esportiva.py:389` (`.replace(" vs. ", " - ")`).

### 2.3 `mercado`

Pernas de uma combinada juntas com `" + "` — a constante é
`value.market_parser.SEPARADOR_PERNAS`, **importe-a, não hard-code o
literal**. Se a API da casa separa pernas com outro caractere (a SportingTech
usa `&`/`|`), converta ANTES de montar `mercado` — não fazer isso faz o
parser tratar a combinada inteira como perna única e casar contra a odd justa
de só uma parte dela (edge inflado; caso real documentado em
`sportingtech.py:79-84`, mesma classe do bug do Mirassol).

Cada perna deve ter o formato `"<Mercado>: <Seleção>"` sempre que a API
distinguir os dois (ver `_normalizar_perna` em `sportingtech.py` para o caso
em que a API omite a palavra "total"). Uma perna sem nome de mercado
recuperável é preferível deixá-la ambígua e deixar o `market_parser` recusar
(`SEM_COBERTURA`) a inventar um nome.

### 2.4 `valido_ate` vs `inicio_evento`

São campos DIFERENTES por design, não redundância:

- `inicio_evento` = kickoff real do jogo. É o que a fila de avaliação usa pra
  priorizar (`storage.pendentes_de_avaliacao`) e o que barra alerta
  pós-jogo.
- `valido_ate` = fim da promoção/boost, que pode ser antes OU depois do
  apito. **Nunca usar um no lugar do outro** — `esportiva.py` tem teste de
  regressão pra isso (`test_inicio_do_combo_vem_do_jogo_nao_do_fim_da_promocao`)
  porque `boostInfo.endDate` já foi usado por engano como kickoff.

Se a casa só manda uma data, replique-a nos dois campos (padrão usado por
`novibet.py`, `sportingtech.py`, `casadeaposta.py`, que só têm uma data
disponível) e documente essa limitação num comentário — não finja precisão
que não existe.

## 3. Dois padrões de estrutura

### Padrão A — casa única (Novibet, CasaDeAposta)

```python
class XScraper:
    def __init__(self, session=None):
        self._session = session
        self._owns = session is None

    async def __aenter__(self):
        if self._session is None:
            self._session = curl_requests.AsyncSession(
                headers={...},                       # headers fixos da casa
                impersonate=config.IMPERSONATE,       # OBRIGATÓRIO, ver §4
                timeout=config.REQUEST_TIMEOUT_SECONDS,
            )
        return self

    async def __aexit__(self, *exc):
        if self._owns and self._session is not None:
            await self._session.close()
            self._session = None

    async def scrape(self) -> list[Offer]:
        ...

async def scrape_offers() -> list[Offer]:
    async with XScraper() as scraper:
        return await scraper.scrape()
```

`CASA` é uma constante de módulo (string), não um parâmetro.

### Padrão B — multi-tenant (Altenar via `esportiva.py`, SportingTech)

Quando uma plataforma serve várias casas com o mesmo endpoint e só um
parâmetro muda (o caso mais comum e mais barato de escalar — 11 casas
Altenar sem parser novo nenhum), o padrão é:

```python
@dataclass(frozen=True)
class CasaX:
    nome: str
    slug: str      # ou "tenant" — o parâmetro que muda por casa
    site: str      # base do link exibido no alerta

CASAS_X: tuple[CasaX, ...] = (
    CasaX("Nome Exibido", "slug-da-api", "https://dominio.bet.br"),
)

class XScraper:
    def __init__(self, casa: CasaX, session=None):
        self.casa = casa
        ...
```

`config.CASAS_ALTENAR` é a tabela de referência; `CASAS_SPORTINGTECH` segue o
mesmo desenho com `tenant` no lugar de `slug`. **Antes de escrever um parser
novo, verifique se a casa não é apenas mais uma linha numa tabela existente**
— duas casas do levantamento (OleyBet/SportingTech, GingaBet/Altenar) eram
exatamente isso.

### Como escolher

Se descobrir no reconhecimento que duas ou mais casas pendentes compartilham
a mesma plataforma (ex. SeuBet/Ultrabet/Maxima/Suprema em BetConstruct,
SportingBet/BetMGM em Entain), implemente Padrão B mesmo que só uma esteja
funcionando hoje — a tabela já fica pronta pra próxima.

## 4. Transporte — regra não-negociável

**Sempre `curl_cffi.requests.AsyncSession` com `impersonate=config.IMPERSONATE`
("chrome"), nunca `httpx`/`requests`/`aiohttp`.** Os WAFs destas casas fazem
fingerprint do handshake TLS; a stack TLS padrão do Python leva 403 mesmo
com headers idênticos ao navegador, porque o handshake em si já denuncia.
Isso já está documentado e testado contra a Betano (`config.py:14-19`) e vale
igual para qualquer casa nova — **não tente `httpx` "só pra confirmar", já
foi tentado e falha por design**.

Não sobrescreva o `User-Agent`: o `impersonate` já manda um coerente com o
fingerprint escolhido: divergir os dois é o que entrega o bot.

Erros de rede/parsing viram sempre `ScraperError` (`betano_superodds.scraper.ScraperError`):

```python
try:
    r = await self._session.get(url)
except RequestException as exc:
    raise ScraperError(f"falha de rede em {self.casa.nome}: {exc}") from exc
if r.status_code != 200:
    raise ScraperError(f"{self.casa.nome} respondeu HTTP {r.status_code}")
try:
    dados = r.json()
except ValueError as exc:
    raise ScraperError(f"{self.casa.nome} não devolveu JSON: {exc}") from exc
```

Uma resposta `200` com corpo de "sucesso vazio" (ex. SportingTech
`{"success": false, "responseKey": "NO_DATA_FOUND"}`) **não é erro** — vira
`return []` com log informativo, não `ScraperError`. Confundir os dois faz o
ciclo logar falha todo dia em vez de "0 ofertas hoje".

## 5. Invariantes que TÊM que valer (todas com bug real por trás)

1. **`odd_boost` é sempre a MAIOR das duas odds candidatas.** Nunca confiar
   na ordem dos campos do payload — comparar e pegar o maior
   (`esportiva.py:404-406`: "inverter as duas faria todo edge sair
   invertido").
2. **Sem `evento_id` estável, descartar a oferta.** Sem ele não dá pra montar
   `offer_id`, revalidar depois, nem casar no diff (`novibet.py:214-218`,
   `casadeaposta.py:106-112`).
3. **`odd_boost <= odd_original` não é oferta, é ruído — descartar.**
4. **Filtrar promoção de conta nova / boas-vindas quando a casa distingue
   isso no payload** (campo tipo `isWelcome`). É captação de conta, ganho
   absurdo (+500% ou mais) e uma vez por CPF — furaria a fila de avaliação
   (que ordena por `boost_pct`) na frente de oferta real. Ver
   `esportiva.py:39-47` para os números medidos que definem o corte.
5. **Nunca inventar `market_key` ou nome de mercado quando o payload não diz
   qual é.** Recusar (deixar o `market_parser` marcar `SEM_COBERTURA`) é
   sempre mais seguro que uma perna com rótulo ambíguo — ver §2.3.
6. **Uma casa que falha não pode derrubar as outras.** No `main.py:coletar`
   cada casa roda em `try/except Exception` próprio; a nova casa segue o
   mesmo padrão automaticamente por estar no mesmo bloco.

## 6. Checklist de integração (fora do arquivo do adapter)

Um adapter novo não fica pronto só com o arquivo do scraper. Falta:

1. **`config.py`**: uma flag `ENABLE_<CASA>` (`os.getenv("ENABLE_<CASA>",
   "1") not in ("0", "false", "False")`), e se for Padrão B, a tabela
   `CASAS_<PLATAFORMA>`.
2. **`main.py:coletar()`**: um bloco `if config.ENABLE_<CASA>: try: async
   with XScraper() as sc: ... except Exception: log.warning(...)`, seguindo
   exatamente o padrão dos três blocos existentes (linhas 159–197). Sem
   isto, o adapter existe mas nunca roda no ciclo real.
3. **Nome não pode colidir** com nenhuma casa já cadastrada — `casa` entra em
   `offer_id`, duplicar quebra a identidade (há teste de regressão pra isso
   em `CASAS_ALTENAR`: `test_tabela_de_casas_nao_tem_nome_repetido`).
4. **Log de "ligada"/"desligada"** no arranque, mesmo padrão das três casas
   em `main.py:314-318`.

## 7. Contrato de teste

**Só a Altenar (`test_esportiva.py`) tem testes hoje.** Novibet, CasaDeAposta
e SportingTech foram implementadas e têm ZERO teste de regressão — isto é
uma lacuna do projeto, não um padrão a seguir. Todo adapter novo tem que vir
com testes no formato abaixo, mesmo que o adapter "de referência" mais
recente não tenha.

### 7.1 Seam de teste obrigatório

O scraper precisa expor **um único método de rede sobrescrevível** — o
`ScraperFake` de `test_esportiva.py` subclassa o scraper real e sobrescreve
só esse método:

```python
class ScraperFake(EsportivaScraper):
    def __init__(self, payload, casa=None):
        super().__init__(casa, session=object())  # sessão nunca usada de verdade
        self._payload = payload

    async def _get(self, path, **params):
        return self._payload
```

Adapters Padrão A que hoje chamam `self._session.get(...)` diretamente
dentro de `scrape()` (é o caso de `casadeaposta.py` e `sportingtech.py` hoje
— falha de design que não deve ser repetida) **não são testáveis assim**.
Todo adapter novo tem que extrair a chamada de rede pra um método próprio
(`_get`/`_get_json`, como `novibet.py` já faz) especificamente para permitir
este seam.

### 7.2 Fixture: payload real capturado, não sintético

O payload de teste tem que ser uma captura real (golden fixture), anotada
com os "porquês" que já causaram bug — não um dict minimalista inventado.
Ver `test_esportiva.py:68-119` (`detalhe()`): cada campo tem comentário
explicando por que está ali (seleção suspensa com preço 0, `endDate`
propositalmente diferente de `startDate`, mercados espalhados entre
`markets`/`childMarkets`). Formato aceito: dict Python inline no arquivo de
teste (convenção atual) OU um JSON separado em `tests/fixtures/<casa>.json`
carregado no setup — qualquer um serve, desde que venha de uma resposta real
da API (`curl_cffi` puro, fora do scraper) e não de payload imaginado.

### 7.3 Casos mínimos que todo adapter novo precisa cobrir

Espelhando o que `test_esportiva.py` já teve que aprender às custas de bug
real:

| # | Caso | Por quê (precedente) |
|---|---|---|
| 1 | Payload feliz gera as `Offer` esperadas (evento, mercado, odds, url) | básico |
| 2 | `odd_boost <= odd_original` é descartado | ruído no payload real |
| 3 | Sem `evento_id` reconhecível, a oferta é descartada (não quebra) | `offer_id` instável |
| 4 | Promoção de boas-vindas / conta nova é filtrada, SE a casa distinguir isso | Altenar `isWelcome` |
| 5 | `odd_boost` é sempre a maior mesmo se a API inverter os campos | edge invertido |
| 6 | Pernas de combo usam `" + "` mesmo que a API separe diferente | bug real da SportingTech |
| 7 | Resposta HTTP não-200 vira `ScraperError` | distinguir de "0 ofertas" |
| 8 | "Sucesso vazio" da API (ex. `NO_DATA_FOUND`) vira `[]`, não `ScraperError` | SportingTech |
| 9 | Payload vazio/malformado não derruba o scraper (`{}`, listas vazias) | robustez de ciclo |
| 10 | JSON inválido/HTML no lugar de JSON vira `ScraperError` com mensagem clara | distinguir bloqueio de WAF |

Um adapter cujos testes não cobrem pelo menos os itens 1–3 e 7–9 não deve
ser mesclado — são os que já causaram incidente real em pelo menos uma das
seis casas existentes.

## 8. O que este contrato NÃO cobre

- **Descoberta de rota** (headers, cookies, TLS) — isso é investigação por
  casa, documentada em `CASAS-PENDENTES.md`/`LEVANTAMENTO-CASAS.md`, não um
  contrato de código.
- **Matching contra a Pinnacle e parsing de texto de mercado** — isso é
  `value/matcher.py` e `value/market_parser.py`, que já funcionam para
  qualquer `Offer` bem formada independente da casa de origem. Um adapter
  correto não precisa (e não deve) sabor nada sobre Pinnacle.
