# Composer multi-canal de anúncios (sabiá)

Front-end + backend pra compor um anúncio e **cross-postar** — uma aba por
canal, cada uma com **preview ao vivo**, limites e validação próprios:

| canal | provedor | texto | mídia |
| --- | --- | --- | --- |
| Instagram | Graph API (fluxo próprio, `instagram.py`) | legenda ≤2150 | 1–10 JPEG, em ordem |
| Reel | Graph API (`media_type=REELS`, mesma conta/token) | legenda ≤2150 (funciona igual à do post) | 1 vídeo MP4/MOV, 9:16, 3 s–15 min |
| WhatsApp | Whapi.Cloud (`channels.py`; ver aviso de ToS em `scripts/post_whatsapp.py`) | ≤2150 | ≤1 |
| Telegram | Bot API oficial | ≤1024 (legenda) | ≤10, em ordem (media group) |
| Mastodon | API da instância | ≤500 | ≤4, em ordem |
| Reddit | API oficial (app "script") | título ≤300 + corpo ≤40000 | viram links no corpo |
| E-mail | SMTP | assunto ≤255 + corpo | anexos |
| Agenda | Google Calendar API v3 (evento na agenda que o [calendario.pedalhidrografi.co](https://calendario.pedalhidrografi.co) exibe) | título ≤255 + descrição ≤2150 + início/fim/local | — |

## O post universal (🌐, a primeira aba)

A matriz do cross-posting (`ph:UniversalPost` + `ph:UniversalPostShape`):
escreva **blocos de texto em escada** — cada degrau é a diferença entre os
limites de texto consecutivos dos canais, então a soma dos blocos 1..N é
exatamente o limite do canal do degrau N:

| bloco | orçamento | soma | entra em |
| --- | --- | --- | --- |
| 1 | 500 | 500 | todos (até o Mastodon) |
| 2 | 524 | 1024 | todos menos Mastodon |
| 3 | 1126 | 2150 | WhatsApp, Instagram, Reel, Agenda, Reddit, e-mail |
| 4 | 37850 | 40000 | Reddit e e-mail |
| 5 | 60000 | 100000 | só e-mail |

Os blocos são emendados por **concatenação pura** (sem separador automático) —
pontuação e quebras entram no fim de cada bloco. Junte até 20 imagens e
**derive**: a app grava a matriz no TTL, corta o texto na escada pra cada
canal, **recorta cada imagem na proporção ⭐ do canal** (JPEG 1080px, recorte
central) e preenche todas as abas. Nada é publicado nesse passo — revise e
publique canal a canal; cada anúncio derivado sai com `prov:wasDerivedFrom`
apontando pra matriz. A escada é *derivada* da tabela de canais em
`ttl_store.text_ladder()`, e o `test_channels.py` confere que a shape (SHACL
estático) codifica os mesmos degraus — mudou limite de canal, a shape
acompanha.

## Ferramentas de composição

O botão **📋 copiar** leva texto+imagens de uma aba pras outras (cortando nos
limites de cada canal; vídeo não vira imagem — do Reel só o texto viaja). O
**✂️ ajustar** abre um editor de imagem no navegador que **corta** (arrastável),
**preenche** (fundo borrado) ou **estica** cada imagem pra proporção do canal —
os presets e a proporção ⭐ vêm das shapes, e o resultado sai JPEG 1080px (o
único formato que o Instagram publica). Cada publicação é gravada no dataset
TTL na forma que o shape do canal espera (`definitions/shapes.ttl`) e validada
com SHACL.

As **especificações de mídia** (proporção recomendada, tamanho máximo, formatos
aceitos — incl. vídeo, ainda não enviado pela app) moram como anotações nas
shapes (`ph:recommendedAspectRatio`, `ph:maxImageBytes`,
`ph:acceptedImageFormat`…) — a app as lê de lá (`/api/config` → `media`) pra
montar hints/checagens; não há números duplicados no código.

Roda local (dry-run) e no **Cloud Run + Cloud Storage**.

## Rodar local (dry-run, sem postar de verdade)

```bash
cd app
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
# abra http://localhost:8080
```

Sem `GCS_BUCKET`, o app entra em **DRY_RUN**: salva as imagens em `app/uploads/`,
escreve o post em `definitions/data_manual.ttl` (com `ph:isPosted`) e **não**
chama o Instagram (o Instagram exige URLs públicas, que o localhost não tem).
Após salvar, roda SHACL e mostra violações, se houver.

## Variáveis de ambiente

| Var | Local | Cloud Run |
| --- | --- | --- |
| `IG_ACCESS_TOKEN` | (não precisa em dry-run) | token de publicação (`IG…`, graph.instagram.com) |
| `IG_MANAGE_TOKEN` | — | token p/ DELETE com `instagram_manage_contents` (pode ser `EAA…`, graph.facebook.com); usa `IG_ACCESS_TOKEN` se vazio |
| `IG_USER_ID` | `me` | `me` ou id numérico |
| `GCS_BUCKET` | — | bucket público p/ imagens (ativa modo GCS) |
| `DATA_TTL` | `definitions/data_manual.ttl` | `gs://<bucket>/data_manual.ttl` |
| `DRY_RUN` | `true` (default sem bucket) | `false` |
| `PORT` | `8080` | injetado pelo Cloud Run |

(Os valores também podem vir do `.env` na raiz do repo.)

### Canais de cross-posting (todos opcionais)

Canal sem as suas vars aparece como **não configurado** na aba (bolinha cinza):
ainda salva rascunho e roda em dry-run, mas recusa publicar ao vivo (400 com a
lista do que falta).

| Canal | Vars |
| --- | --- |
| WhatsApp | `WHAPI_TOKEN`, `WHAPI_ANNOUNCE_GROUP` (id do grupo de anúncios; descubra com `python scripts/post_whatsapp.py --list-announce`) |
| Telegram | `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (`@nomedocanal` ou `-100…`; o bot precisa ser admin) |
| Mastodon | `MASTODON_BASE_URL` (ex.: `https://ciclo.social`), `MASTODON_ACCESS_TOKEN` (escopo write) |
| Reddit | `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`, `REDDIT_SUBREDDIT` (app tipo "script" em reddit.com/prefs/apps) |
| E-mail | `SMTP_HOST`, `SMTP_PORT` (587; 465 = SSL), `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_FROM`, `EMAIL_TO` (vírgulas separam) |
| Agenda | `GCAL_CALENDAR_ID` (id `…@group.calendar.google.com` da agenda do grupo), `GCAL_TIMEZONE` (padrão `America/Sao_Paulo`). **Sem token**: a credencial é ADC — na Cloud Run, compartilhe a agenda com a SA de runtime ("Fazer alterações em eventos"); localmente, `gcloud auth application-default login` |

## Cloud Run

Use o script da raiz do repo — cria o bucket (imagens + `data_manual.ttl`),
sobe os tokens do `.env` pro Secret Manager, e faz o deploy:

```bash
./deploy.sh
# overrides: REGION=us-central1 BUCKET=meu-bucket IG_USER_ID=<id> SEED_DATA=1 ./deploy.sh
```

O que ele faz:

- **Bucket público** (`allUsers:objectViewer`) — o Instagram precisa baixar as
  imagens por URL pública. O `data_manual.ttl` mora no mesmo bucket (também fica
  público; é conteúdo de divulgação, não segredo — separe em outro bucket se
  quiser privado).
- **Filesystem efêmero do Cloud Run** → tanto imagens quanto `.ttl` vão pro GCS
  (`GCS_BUCKET` + `DATA_TTL=gs://…`). Shapes/ontologia ficam embutidos na imagem.
- **Deploy PROTEGIDO** (`--no-allow-unauthenticated`): o serviço posta/apaga no
  Instagram de verdade, então NÃO pode ser público. Acesse via
  `gcloud run services proxy ph-composer --region <REGION>` ou dê `run.invoker`
  pra sua conta (o script imprime os comandos no fim).
- `SEED_DATA=1` sobe seu `definitions/data_manual.ttl` local pro bucket uma vez.

## Conferir os tokens

```bash
python scripts/check_tokens.py   # só leitura: não posta nem apaga; sai 1 se apagar estiver quebrado
```

### Renovação automática do `IG_ACCESS_TOKEN`

O token de publicação dura 60 dias, mas **renova indefinidamente enquanto
válido** — e a app faz isso sozinha: `POST /api/token-refresh` (app/tokens.py)
chama o `refresh_access_token`, troca o token em memória e grava uma versão
nova no secret `ig-access-token` (local: no `.env`). O `deploy.sh` cria um job
do **Cloud Scheduler** (`ig-token-refresh`, toda segunda 09:00 SP) que chama a
rota, então o token só morre se ficar >60 dias sem renovar (serviço fora do ar)
ou se a conta trocar de senha/revogar o app — aí é refazer o login uma vez e
`./deploy.sh`. A resposta da rota nunca inclui o token, só metadados; ela também
reporta os dias restantes de **acesso a dados** do `IG_MANAGE_TOKEN` (esse
relógio, ~90 dias, só renova com login manual: `scripts/refresh_manage_token.py`).

**Publicar e apagar usam tokens e hosts diferentes** — dá pra publicar
perfeitamente e mesmo assim nunca conseguir apagar:

| | publicar | apagar |
| --- | --- | --- |
| token | `IG_ACCESS_TOKEN` (Instagram Login, `IG…`) | `IG_MANAGE_TOKEN` (Facebook Login, `EAA…`) |
| host | `graph.instagram.com` | `graph.facebook.com` |
| permissões | `instagram_business_content_publish` | `instagram_basic` + `instagram_manage_contents` |

`graph.instagram.com` responde `Unsupported delete request`: **não existe**
DELETE lá. Quando o token de apagar morre, a app continua publicando e só o ✕
quebra (502) — falha silenciosa. Rode o `check_tokens.py` nessa hora.

O `IG_MANAGE_TOKEN` é um token de PÁGINA tirado de um token de usuário de longa
duração (`python scripts/refresh_manage_token.py`). Ele tem **dois relógios
independentes**:

- `expires_at = 0` — **não expira**. Só cai se a senha da admin mudar, se as
  permissões forem revogadas ou o app removido.
- `data_access_expires_at` — **90 dias** desde a última atividade da pessoa.
  O token continua *válido*, mas as chamadas param de devolver dados até
  reautorizar. É o próximo jeito de isso quebrar; o `check_tokens.py` avisa.

Um token pego direto no Graph API Explorer é **curto (~1h)** — não use.

## Testar

```bash
cd app && python test_post_delete.py   # Instagram: Graph API falsa; não toca na conta real
cd app && python test_channels.py      # demais canais: APIs falsas (Whapi/Telegram/Mastodon/Reddit/SMTP/Agenda)
```

O `test_channels.py` cobre a forma RDF de cada canal (imagem única no
WhatsApp, lista ordenada no Telegram/Mastodon, conjunto no Reddit/e-mail), os
limites que viram 400 (texto, contagem, formato e tamanho de imagem — lidos
das shapes), a trava de confirmação (409), canal sem config (400) e rascunhos
sem chamada de rede.

Cobre o carrossel, a trava de engajamento e o fato de que os tipos derivados
pelas `sh:rule` (`ph:AppOwnedInstagramPost`, `ph:DeletableInstagramPost`)
**nunca** podem ser gravados no `.ttl`: como as regras só acrescentam tipos, um
`ph:DeletableInstagramPost` persistido faria um post popular seguir removível
pra sempre, furando a trava.

## Limites / pendências

- **Permissão de publicação:** o token precisa de `instagram_business_content_publish`
  (escopo extra; o token de leitura que pega posts não basta).
- **Posicionamento de tags:** a API exige `x,y` por conta marcada; usamos o centro
  (0.5, 0.5) por padrão. Dá pra evoluir pra arrastar a tag na imagem.
- **Colaboradoras:** enviadas como `collaborators` na criação do container; a
  pessoa precisa aceitar o convite no app dela (comportamento do Instagram).
- **Vídeo:** só o Reel envia vídeo (um MP4/MOV; o processamento na API demora —
  a app espera ~3 min). O POST de Instagram segue imagem-só de propósito. Nos
  demais canais as shapes já documentam formatos/tamanhos de vídeo pra um
  suporte futuro (`ph:acceptedVideoFormat`, `ph:maxVideoBytes`).
- **Reddit + imagens:** entram como links no corpo do post (o upload nativo de
  galeria exige lease S3; fora do escopo).
- **`location_id`:** o Instagram aceita o id da página de localização (ex.: `2094742`),
  não um nome livre — busque o id antes.
