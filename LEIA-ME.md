# Bot de mercado para Discord

Corre sozinho no GitHub Actions a cada 15 minutos e envia para um canal do Discord:

- **Aberturas de mercado:** Tóquio, China e Hong Kong, Londres e Nova Iorque, nos dias úteis, na hora de Portugal (as mudanças de hora são tratadas automaticamente).
- **Relatório às 08:00 e às 21:00:** preço, variação em 24h e 7 dias, tendência em 4h e no diário, mínimos e máximos, funding e open interest, notícias de alto impacto do dia e onde está o preço em cada um dos teus ranges.
- **Avisos 30 minutos antes** de notícias de alto impacto dos EUA (CPI, Fed, emprego).
- **Notícias filtradas** por palavras-chave (SEC, ETF, Fed, hacks, liquidações, etc.).
- **Alertas nos teus ranges DMT:** preço a chegar ao topo ou ao fundo, desvio para lá da linha, fecho de volta para dentro, e range invalidado ao passar a DL.
- **Movimentos bruscos** (mais de 3% numa hora) e **funding extremo**.
- **Liquidez:** máximo e mínimo de ontem e da semana passada, e da sessão anterior em cada abertura (Ásia na abertura de Londres, Londres na de Nova Iorque). Alerta quando um destes níveis é varrido e o preço fecha de volta.
- **Ranges candidatos** no 1h e no 4h, procurados de hora a hora, com alerta quando aparece um novo.
- **Open interest:** alerta quando sobe muito com o preço parado ou cai de repente com o preço a mexer (liquidações).
- **Extras:** Fear & Greed no relatório da manhã, notícias do dia marcadas quando já saíram, aviso de fim de semana à sexta à noite e as notícias da semana seguinte no relatório de domingo.

## Instalação (uns 15 minutos)

### 1. Webhook do Discord
1. No teu servidor do Discord, cria um canal (por exemplo `#mercado`).
2. Definições do canal → **Integrações** → **Webhooks** → **Novo webhook**.
3. Dá-lhe um nome e carrega em **Copiar URL do webhook**. Guarda esse URL; é a "senha" do canal, não o partilhes.

### 2. Repositório no GitHub
1. Em github.com → **New repository**. Nome, por exemplo, `bot-trading`.
2. Marca como **Public**. Os repositórios públicos têm minutos ilimitados no GitHub Actions; um privado com corridas a cada 15 minutos ultrapassa os minutos gratuitos do mês. O URL do webhook fica protegido como segredo (passo 3), por isso não aparece. O que fica visível é o código e o `niveis.json`.
3. Carrega os ficheiros: **Add file → Upload files** e arrasta tudo, incluindo a pasta `.github`.
   - Se a pasta `.github` não aparecer (está oculta em alguns sistemas), cria o workflow à mão: **Add file → Create new file**, escreve o caminho `.github/workflows/bot.yml` e cola o conteúdo do ficheiro.

### 3. Guardar o webhook como segredo
Settings → **Secrets and variables** → **Actions** → **New repository secret**
- Name: `DISCORD_WEBHOOK`
- Secret: o URL que copiaste no passo 1

### 4. Dar permissão ao bot para guardar o estado
Settings → **Actions** → **General** → em *Workflow permissions* escolhe **Read and write permissions** → **Save**.

### 5. Testar
Separador **Actions** → (se pedir, ativa os workflows) → **bot-trading** → **Run workflow** → modo `teste` → **Run workflow**.
Em menos de um minuto deve chegar ao Discord uma mensagem de confirmação e um relatório.
Se não chegar, abre a execução, clica em **correr** e copia o registo ("Correr o bot") para me mostrares.

A partir daqui corre sozinho. O GitHub pode atrasar as corridas agendadas alguns minutos, por isso um aviso pode chegar um pouco depois da hora exata.

## Adicionar os teus ranges DMT
Edita o `niveis.json` diretamente no GitHub (ícone do lápis, funciona no telemóvel):

```json
{
  "ranges": [
    {
      "id": "btc-4h",
      "ativo": true,
      "par": "BTCUSDT",
      "topo": 70000,
      "fundo": 66000,
      "dl": 30,
      "lado": "long",
      "nota": "range 4h"
    }
  ]
}
```

- `lado`: `long` (só vigia o fundo), `short` (só o topo) ou `ambos`.
- `dl`: a DL em percentagem da altura do range.
- Para deixar de vigiar um range, põe `"ativo": false`. Se mudares o topo, o fundo ou a DL, os alertas desse range recomeçam do zero.
- O bot não conta toques. A contagem e a decisão de entrar continuam contigo e com a verificação.

## Afinar (config.json)
- `moedas`: as moedas dos relatórios e alertas.
- `relatorios`: as horas dos relatórios.
- `noticias.palavras`: as palavras que tornam uma notícia relevante. Mais palavras, mais mensagens.
- `alertas.movimento_1h_pct`, `alertas.funding_8h_pct`, `alertas.zona_toque_pct`, `alertas.oi_1h_pct`: os limites dos alertas.
- `liquidez.moedas`: as moedas vigiadas nos varrimentos de máximos e mínimos.
- `scanner`: moedas, timeframes, altura mínima e máxima do range (em % do preço), tolerância dos toques e número mínimo de toques de cada lado.
- `fear_greed`: `false` para tirar o índice do relatório.

## Fontes de dados
- Preços e velas: API pública da Binance (`data-api.binance.vision`).
- Funding e open interest: Hyperliquid.
- Calendário económico: feeds desta semana e da próxima da Forex Factory.
- Fear & Greed: alternative.me.
- Notícias: RSS do CoinDesk, Cointelegraph e Decrypt.

Se uma fonte falhar, o resto continua a funcionar e o erro fica no registo da execução.
