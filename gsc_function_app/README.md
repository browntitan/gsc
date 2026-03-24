# GSC Function App

This Azure Functions Python project uses an HTTP-only architecture for one-time ASPX processing in Azure Government.

## Functions

- `process_one_gsc_blob`
  - `POST /api/admin/process-one`
- `process_gsc_batch`
  - `POST /api/admin/process-batch`
- `health_check`
  - `GET /api/admin/health`

All routes use `FUNCTION` auth.

## Required app settings

- `cdooaipocdata1_STORAGE`
- `PGVECTOR_DATABASE_URL`
- `AZURE_OPENAI_BASE_URL`
- `AZURE_OPENAI_API_KEY`

## Optional app settings and defaults

- `OUTPUT_CONTAINER=gsc-cleaned`
- `CHUNK_TABLE_NAME=gsc_vector_rag`
- `COLLECTION_NAME=gsc-internal-policies`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT=text-embedding-ada-002`
- `AZURE_OPENAI_EMBEDDINGS_PATH=/embeddings`
- `EMBEDDING_DIMENSIONS=1536`
- `REQUEST_TIMEOUT_SECONDS=120`
- `CHUNK_SIZE=120`
- `CHUNK_OVERLAP=20`

## Local validation

```powershell
Set-Location "C:\path\to\openwebui-supplychain-demo\gsc_function_app"
python -m pip install -r requirements.txt
python -m py_compile .\function_app.py .\aspx_cleaner.py .\pgvector_ingest.py
func start
```

## Azure Government deploy

```powershell
Set-Location "C:\path\to\openwebui-supplychain-demo\gsc_function_app"
az cloud set --name AzureUSGovernment
func azure functionapp publish funciton-app-shiv-gsc-test --management-url https://management.usgovcloudapi.net
```

## List indexed functions

```powershell
az functionapp function list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --output table
```

## Invoke `process_one_gsc_blob`

```powershell
$processOneKey = az functionapp function keys list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name process_one_gsc_blob `
  --query default `
  --output tsv

$processOneUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/process-one?code=$processOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $processOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "path/to/example.aspx"
    embed = $true
  } | ConvertTo-Json)
```

## Invoke `process_gsc_batch`

```powershell
$processBatchKey = az functionapp function keys list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name process_gsc_batch `
  --query default `
  --output tsv

$processBatchUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/process-batch?code=$processBatchKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $processBatchUrl `
  -ContentType "application/json" `
  -Body (@{
    limit = 5
    prefix = ""
    embed = $true
  } | ConvertTo-Json)
```

## Troubleshooting

### Functions do not appear after deploy

- Confirm the deploy was run from the `gsc_function_app` folder.
- Run `az functionapp function list` after deployment completes and trigger sync finishes.
- Check streaming logs with `func azure functionapp logstream funciton-app-shiv-gsc-test --management-url https://management.usgovcloudapi.net`.
- Use `GET /api/admin/health` to confirm required app settings and dependency access.

### Import or module errors

- Make sure `requirements.txt` was deployed with the code.
- The canonical cleaner filename is `aspx_cleaner.py`, and imports now use `from aspx_cleaner import ...`.
- Re-run `python -m py_compile` locally before deploying.

### PostgreSQL connection errors

- Verify `PGVECTOR_DATABASE_URL` is present and valid.
- Confirm the database allows the Function App outbound network path.
- Ensure the target role can create `vector` and `pgcrypto` extensions and can create the target table and indexes.

### Embedding endpoint response-shape mismatches

- The code expects `POST <AZURE_OPENAI_BASE_URL><AZURE_OPENAI_EMBEDDINGS_PATH>` with a response shaped like `data[0].embedding`.
- If your gateway differs, change `AZURE_OPENAI_EMBEDDINGS_PATH` first.
- If the payload shape differs more deeply, the raised error includes the unexpected response payload to speed up adjustment.
