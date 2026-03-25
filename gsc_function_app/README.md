# GSC Function App

This Azure Functions Python project uses an HTTP-only architecture for one-time ASPX processing in Azure Government.

Important deployment note:
Deploy the contents of `gsc_function_app`, not the repo root. The deployment package root must contain `function_app.py`, `host.json`, and `requirements.txt` directly.

## Functions

- `clean_one_gsc_blob`
  - `POST /api/admin/clean-one`
- `embed_one_cleaned_blob`
  - `POST /api/admin/embed-one`
- `process_one_gsc_blob`
  - `POST /api/admin/process-one`
- `clean_gsc_batch`
  - `POST /api/admin/clean-batch`
- `embed_cleaned_batch`
  - `POST /api/admin/embed-batch`
- `process_gsc_batch`
  - `POST /api/admin/process-batch`
- `health_check`
  - `GET /api/admin/health`

All routes use `FUNCTION` auth. Use the `clean-*` and `embed-*` routes when you want full step-by-step control, and use the `process-*` routes when you want cleaning and embedding in one HTTP call.

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

## Create a correct portal ZIP package

If you want to deploy by ZIP from the Azure portal or Deployment Center, create the archive from inside `gsc_function_app` so the package root is correct:

```powershell
Set-Location "C:\path\to\openwebui-supplychain-demo\gsc_function_app"
.\package_for_portal.ps1
```

The generated ZIP is written to:

```text
gsc_function_app\dist\gsc_function_app_portal.zip
```

That ZIP should contain these files at the ZIP root, not nested under another folder:

```text
function_app.py
host.json
requirements.txt
```

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
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --output table
```

## Invoke `clean_one_gsc_blob`

```powershell
$cleanOneKey = az functionapp function keys list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name clean_one_gsc_blob `
  --query default `
  --output tsv

$cleanOneUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/clean-one?code=$cleanOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $cleanOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "path/to/example.aspx"
  } | ConvertTo-Json)
```

## Invoke `embed_one_cleaned_blob`

```powershell
$embedOneKey = az functionapp function keys list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name embed_one_cleaned_blob `
  --query default `
  --output tsv

$embedOneUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/embed-one?code=$embedOneKey"

Invoke-RestMethod `
  -Method Post `
  -Uri $embedOneUrl `
  -ContentType "application/json" `
  -Body (@{
    blob = "path/to/example.txt"
  } | ConvertTo-Json)
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

## Invoke `clean_gsc_batch`

```powershell
$cleanBatchKey = az functionapp function keys list `
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name clean_gsc_batch `
  --query default `
  --output tsv

$cleanBatchUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/clean-batch?code=$cleanBatchKey"

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
  --name funciton-app-shiv-gsc-test `
  --resource-group funciton-app-shiv-gsc-test_group `
  --function-name embed_cleaned_batch `
  --query default `
  --output tsv

$embedBatchUrl = "https://funciton-app-shiv-gsc-test.azurewebsites.us/api/admin/embed-batch?code=$embedBatchKey"

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

- If you deployed the repo root, redeploy using only the `gsc_function_app` contents.
- Confirm the deploy was run from the `gsc_function_app` folder.
- Run `az functionapp function list` after deployment completes and trigger sync finishes.
- Check streaming logs with `func azure functionapp logstream funciton-app-shiv-gsc-test --management-url https://management.usgovcloudapi.net`.
- Use `GET /api/admin/health` to confirm required app settings and dependency access.

### Check package layout after deploy

- In the Azure portal, open `Advanced Tools (Kudu)` for the Function App and inspect `/site/wwwroot`.
- `function_app.py`, `host.json`, and `requirements.txt` must be directly under `/site/wwwroot`.
- If you see them under `/site/wwwroot/gsc_function_app/`, the wrong folder was deployed and the runtime will not discover the app correctly.

### Import or module errors

- Make sure `requirements.txt` was deployed with the code.
- For ZIP/manual deploys, make sure remote build is enabled if the platform needs to install Python dependencies.
- The canonical cleaner filename is `aspx_cleaner.py`, and imports now use `from aspx_cleaner import ...`.
- Re-run `python -m py_compile` locally before deploying.

### Host runtime settings

- Verify `FUNCTIONS_WORKER_RUNTIME=python`.
- Verify `AzureWebJobsStorage` exists and points to a valid storage account the app can reach.
- These runtime settings are different from the custom app settings used by the HTTP handlers.

### PostgreSQL connection errors

- Verify `PGVECTOR_DATABASE_URL` is present and valid.
- Confirm the database allows the Function App outbound network path.
- Ensure the target role can create `vector` and `pgcrypto` extensions and can create the target table and indexes.

### Embedding endpoint response-shape mismatches

- The code expects `POST <AZURE_OPENAI_BASE_URL><AZURE_OPENAI_EMBEDDINGS_PATH>` with a response shaped like `data[0].embedding`.
- If your gateway differs, change `AZURE_OPENAI_EMBEDDINGS_PATH` first.
- If the payload shape differs more deeply, the raised error includes the unexpected response payload to speed up adjustment.
