# Composer de posts do Instagram

Front-end + backend pra compor um post (legenda, imagens, colaboradoras, contas
marcadas, local), com **preview ao vivo** estilo Instagram, e **publicar** via
Graph API. Roda local (dry-run) e no **Cloud Run + Cloud Storage**.

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
|-----|-------|-----------|
| `IG_ACCESS_TOKEN` | (não precisa em dry-run) | token de publicação (`IG…`, graph.instagram.com) |
| `IG_MANAGE_TOKEN` | — | token p/ DELETE com `instagram_manage_contents` (pode ser `EAA…`, graph.facebook.com); usa `IG_ACCESS_TOKEN` se vazio |
| `IG_USER_ID` | `me` | `me` ou id numérico |
| `GCS_BUCKET` | — | bucket público p/ imagens (ativa modo GCS) |
| `DATA_TTL` | `definitions/data_manual.ttl` | `gs://<bucket>/data_manual.ttl` |
| `DRY_RUN` | `true` (default sem bucket) | `false` |
| `PORT` | `8080` | injetado pelo Cloud Run |

(Os valores também podem vir do `.env` na raiz do repo.)

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

## Limites / pendências

- **Permissão de publicação:** o token precisa de `instagram_business_content_publish`
  (escopo extra; o token de leitura que pega posts não basta).
- **Posicionamento de tags:** a API exige `x,y` por conta marcada; usamos o centro
  (0.5, 0.5) por padrão. Dá pra evoluir pra arrastar a tag na imagem.
- **Colaboradoras:** enviadas como `collaborators` na criação do container; a
  pessoa precisa aceitar o convite no app dela (comportamento do Instagram).
- **Vídeos/Reels:** só imagens por enquanto.
- **`location_id`:** o Instagram aceita o id da página de localização (ex.: `2094742`),
  não um nome livre — busque o id antes.
