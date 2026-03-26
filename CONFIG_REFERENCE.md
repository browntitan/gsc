# Config Reference

## Pipeline Valves / Environment Variables

| Name | Default | Valid Values | Purpose |
| --- | --- | --- | --- |
| `PIPELINE_NAME` | `Supply Chain Internal Policy Pipeline` | text | Display name shown by Pipelines/Open WebUI |
| `DATABASE_URL` | empty | Postgres URL | Shared database used by the pipeline |
| `CHUNK_TABLE_NAME` | `supply_chain_chunks` | SQL identifier | Shared pgvector table |
| `DEFAULT_COLLECTION_NAME` | `GSC-Internal-Policy` | text | Collection searched by the pipeline |
| `TOP_K` | `6` | integer `1-20` | Number of chunks returned from retrieval |
| `REQUEST_TIMEOUT_SECONDS` | `120` | integer | Timeout for model and embedding calls |
| `ROUTER_MODE` | `extractor_assisted` | `extractor_assisted`, `deterministic` | Input-understanding path |
| `ENABLE_LLM_EXTRACTOR` | `true` | `true`, `false` | Turns the extractor on or off |
| `ENABLE_LLM_FORMATTER` | `true` | `true`, `false` | Turns the formatter on or off |
| `EXTRACTOR_TIMEOUT_SECONDS` | `30` | integer | Timeout for extractor and formatter calls |
| `ENABLE_ANSWERABILITY_CHECK` | `true` | `true`, `false` | Turns the grounded no-answer guard on or off |
| `ANSWERABILITY_TIMEOUT_SECONDS` | `30` | integer | Timeout for the grounded no-answer guard |
| `EMBEDDING_DIMENSIONS` | `1536` | integer | Vector dimension expected by the table and Azure embedding deployment |

## Azure OpenAI Settings

| Name | Default | Purpose |
| --- | --- | --- |
| `AZURE_OPENAI_ENDPOINT` | empty | Azure OpenAI endpoint root, such as `https://<resource>.openai.azure.us` |
| `AZURE_OPENAI_API_KEY` | empty | Azure OpenAI API key |
| `AZURE_OPENAI_DEPLOYMENT_NAME` | empty | Azure chat deployment used for extractor, formatter, answerability, and final answer generation |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME` | empty | Azure embedding deployment used for query embeddings |
| `AZURE_OPENAI_API_VERSION` | `2024-02-01` | Azure OpenAI API version |

The pipeline is Azure OpenAI only. All Azure valves plus `DATABASE_URL` must be populated for successful runtime queries.

## Seed Script Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--input-path` | required | Clause file or directory to parse |
| `--collection-name` | `GSC-Internal-Policy` | Collection namespace to write into |
| `--database-url` | `postgresql://openwebui:openwebui@localhost:55432/openwebui` | Target Postgres database |
| `--chunk-table-name` | `supply_chain_chunks` | Shared chunk table |
| `--embedding-dimensions` | `1536` | Embedding dimension |
| `--azure-openai-endpoint` | empty | Azure OpenAI endpoint root |
| `--azure-openai-api-key` | empty | Azure API key |
| `--azure-openai-embedding-deployment-name` | empty | Azure embedding deployment used by the seed script |
| `--azure-openai-api-version` | `2024-02-01` | Azure OpenAI API version |
| `--request-timeout-seconds` | `120` | Embedding timeout |
| `--chunk-size` | `120` | Chunk size in words |
| `--chunk-overlap` | `20` | Chunk overlap in words |
| `--dry-run` | `false` | Parse only; do not embed or write |
| `--report-file` | empty | Optional JSON report path |
| `--replace-collection` | `false` | Clear the target collection before writing |
| `--delete-collection` | none | Delete an additional collection after the write |

## Recommended Local Defaults

For the local demo stack, keep the local Postgres target but use Azure OpenAI for chat and embeddings:

- `DEFAULT_COLLECTION_NAME=GSC-Internal-Policy`
- `ROUTER_MODE=extractor_assisted`
- `ENABLE_LLM_EXTRACTOR=true`
- `ENABLE_LLM_FORMATTER=true`
- `DATABASE_URL=postgresql://openwebui:openwebui@postgres:5432/openwebui`
- `AZURE_OPENAI_API_VERSION=2024-02-01`
- `AZURE_OPENAI_DEPLOYMENT_NAME=<chat-deployment>`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=<ada-embedding-deployment>`
- `EMBEDDING_DIMENSIONS=1536`

## Recommended Seed Command

```bash
export AZURE_OPENAI_ENDPOINT="https://<resource>.openai.azure.us"
export AZURE_OPENAI_API_KEY="<api-key>"
export AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME="<ada-embedding-deployment>"
export AZURE_OPENAI_API_VERSION="2024-02-01"

python scripts/ingest_supply_chain_txt.py \
  --input-path demo-data \
  --collection-name GSC-Internal-Policy \
  --replace-collection \
  --delete-collection supply_chain_tcs_demo
```
