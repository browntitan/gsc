# GSC Function App

This Azure Functions Python project uses an HTTP-only architecture for one-time ASPX processing in Azure Government.

For detailed documentation on each function including inputs, outputs, data transformation steps, and database schema, see [FUNCTION_REFERENCE.md](FUNCTION_REFERENCE.md).

## Functions

- `clean_one_gsc_blob`
  - `POST /api/gsc/clean-one`
- `embed_one_cleaned_blob`
  - `POST /api/gsc/embed-one`
- `process_one_gsc_blob`
  - `POST /api/gsc/process-one`
- `clean_gsc_batch`
  - `POST /api/gsc/clean-batch`
- `embed_cleaned_batch`
  - `POST /api/gsc/embed-batch`
- `process_gsc_batch`
  - `POST /api/gsc/process-batch`
- `health_check`
  - `GET /api/gsc/health`

All routes use `FUNCTION` auth. Use the `clean-*` and `embed-*` routes when you want full step-by-step control, and use the `process-*` routes when you want cleaning and embedding in one HTTP call.

## Required app settings

| Name | Description |
|---|---|
| `AzureWebJobsFeatureFlags` | Must be set to `EnableWorkerIndexing` for Python v2 functions to be discovered |
| `FUNCTIONS_WORKER_RUNTIME` | Must be set to `python` |
| `cdooaipocdata1_STORAGE` | Azure Blob Storage connection string (find in Storage Account > Access keys) |
| `PGVECTOR_DATABASE_URL` | PostgreSQL connection string (e.g. `postgresql://user:pass@host:5432/dbname`) |
| `AZURE_OPENAI_BASE_URL` | Azure OpenAI endpoint URL |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key |

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

Deploy using the Azure Functions Core Tools with remote build:

```powershell
Set-Location "C:\path\to\openwebui-supplychain-demo\gsc_function_app"
az cloud set --name AzureUSGovernment
az login
func azure functionapp publish func-app-gsc-3 --build remote --python
```

The `--build remote` flag is critical — it triggers Oryx to run `pip install -r requirements.txt` on the Azure Linux host, ensuring native packages like `psycopg[binary]` are compiled for the correct platform.

## Create and deploy a new Function App from VS Code

Set VS Code to Azure Government before signing in:

1. Open `File > Preferences > Settings`.
2. Search for `Azure: Cloud`.
3. Set it to `AzureUSGovernment`.

Then use these VS Code command palette commands:

```text
Azure: Sign In
Azure: Select Subscriptions
Azure Functions: Install or Update Core Tools
Azure Functions: Create New Project...
Azure Functions: Create Function App in Azure...(Advanced)
Azure Functions: Deploy to Function App
```

Recommended choices for this project when `Azure Functions: Create New Project...` runs:

```text
Language: Python
Programming model: Model V2
Template for first function: Skip for now
```

Recommended choices when `Azure Functions: Create Function App in Azure...(Advanced)` runs:

```text
Subscription: your Azure Government subscription
Function app name: <globally-unique-name>
Runtime stack: Python
Runtime version: choose the current supported Python version for your target app
OS: Linux
Hosting plan: Consumption or Elastic Premium
Resource group: create new or select existing
Storage account: create new or select existing
Application Insights: create new or select existing
```

After VS Code creates the new app, publish your local project:

```text
Azure Functions: Deploy to Function App
```

VS Code then prompts you to pick the target Function App and confirm overwrite for the remote contents.

## List indexed functions

```powershell
az functionapp function list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --output table
```

## Invoke `clean_one_gsc_blob`

```powershell
$cleanOneKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name clean_one_gsc_blob `
  --query default `
  --output tsv

$cleanOneUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/clean-one?code=$cleanOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $cleanOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "1.-Definitions.aspx"
  } | ConvertTo-Json)
```

## Invoke `embed_one_cleaned_blob`

```powershell
$embedOneKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name embed_one_cleaned_blob `
  --query default `
  --output tsv

$embedOneUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/embed-one?code=$embedOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $embedOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "1.-Definitions.txt"
  } | ConvertTo-Json)
```

## Invoke `process_one_gsc_blob`

```powershell
$processOneKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name process_one_gsc_blob `
  --query default `
  --output tsv

$processOneUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/process-one?code=$processOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $processOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "1.-Definitions.aspx"
    embed = $true
  } | ConvertTo-Json)
```

## Invoke `clean_gsc_batch`

```powershell
$cleanBatchKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name clean_gsc_batch `
  --query default `
  --output tsv

$cleanBatchUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/clean-batch?code=$cleanBatchKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $cleanBatchUrl `
  -ContentType "application/json" `
  -Body (@{
    limit = 5
    prefix = ""
  } | ConvertTo-Json)
```

## Invoke `embed_cleaned_batch`

```powershell
$embedBatchKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name embed_cleaned_batch `
  --query default `
  --output tsv

$embedBatchUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/embed-batch?code=$embedBatchKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $embedBatchUrl `
  -ContentType "application/json" `
  -Body (@{
    limit = 5
    prefix = ""
  } | ConvertTo-Json)
```

## Invoke `process_gsc_batch`

```powershell
$processBatchKey = az functionapp function keys list `
  --name func-app-gsc-3 `
  --resource-group func-app-gsc-3_group `
  --function-name process_gsc_batch `
  --query default `
  --output tsv

$processBatchUrl = "https://func-app-gsc-3.azurewebsites.us/api/gsc/process-batch?code=$processBatchKey"

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

- Confirm `AzureWebJobsFeatureFlags` is set to `EnableWorkerIndexing` in Azure Portal app settings.
- Confirm `FUNCTIONS_WORKER_RUNTIME` is set to `python`.
- Confirm the deploy was run from the `gsc_function_app` folder with `--build remote --python`.
- Check Kudu (Advanced Tools > Debug console) to verify `.python_packages` exists in `site/wwwroot`.
- Run `az functionapp function list` after deployment completes and trigger sync finishes.
- Check streaming logs with `func azure functionapp logstream func-app-gsc-3 --management-url https://management.usgovcloudapi.net`.
- Use `GET /api/gsc/health` to confirm required app settings and dependency access.

### Import or module errors

- Make sure `requirements.txt` was deployed with the code.
- The canonical cleaner filename is `aspx_cleaner.py`, and imports now use `from aspx_cleaner import ...`.
- Re-run `python -m py_compile` locally before deploying.

### PostgreSQL connection errors

- Verify `PGVECTOR_DATABASE_URL` is present and valid (check for typos in the hostname).
- Confirm the database allows the Function App outbound network path.
- Ensure the target role can create `vector` and `pgcrypto` extensions and can create the target table and indexes.

### Embedding endpoint response-shape mismatches

- The code expects `POST <AZURE_OPENAI_BASE_URL><AZURE_OPENAI_EMBEDDINGS_PATH>` with a response shaped like `data[0].embedding`.
- If your gateway differs, change `AZURE_OPENAI_EMBEDDINGS_PATH` first.
- If the payload shape differs more deeply, the raised error includes the unexpected response payload to speed up adjustment.
