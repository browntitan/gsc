# Config Reference

## Pipeline Valves / Environment Variables

| Name | Default | Valid Values | Purpose |
| --- | --- | --- | --- |
| `PIPELINE_NAME` | `Supply Chain Internal Policy Pipeline` | text | Display name shown by Pipelines/Open WebUI |
| `DATABASE_URL` | `postgresql://openwebui:openwebui@postgres:5432/openwebui` | Postgres URL | Shared database used by the pipeline |
| `CHUNK_TABLE_NAME` | `supply_chain_chunks` | SQL identifier | Shared pgvector table |
| `DEFAULT_COLLECTION_NAME` | `GSC-Internal-Policy` | text | Collection searched by the pipeline |
| `TOP_K` | `6` | integer `1-20` | Number of chunks returned from retrieval |
| `REQUEST_TIMEOUT_SECONDS` | `120` | integer | Timeout for model and embedding calls |
| `ROUTER_MODE` | `extractor_assisted` | `extractor_assisted`, `deterministic` | Input-understanding path |
| `ENABLE_LLM_EXTRACTOR` | `true` | `true`, `false` | Turns the extractor on or off |
| `ENABLE_LLM_FORMATTER` | `true` | `true`, `false` | Turns the formatter on or off |
| `EXTRACTOR_TIMEOUT_SECONDS` | `30` | integer | Timeout for extractor and formatter calls |

## Provider Selection

| Name | Default | Valid Values | Purpose |
| --- | --- | --- | --- |
| `ANSWER_PROVIDER` | `ollama` | `ollama`, `azure_openai` | Provider used for grounded answer generation |
| `ANSWER_MODEL` | `gpt-oss:20b` | provider-specific model/deployment | Default answer-model override |
| `EMBEDDING_PROVIDER` | `ollama` | `ollama`, `azure_openai` | Provider used for embeddings |
| `EMBEDDING_MODEL` | empty | provider-specific model/deployment | Optional embedding-model override |
| `EMBEDDING_DIMENSIONS` | `768` | integer | Vector dimension expected by the table |
| `EXTRACTOR_PROVIDER` | `ollama` | `ollama`, `azure_openai` | Default extractor provider |
| `EXTRACTOR_MODEL` | `gpt-oss:20b` | provider-specific model/deployment | Default extractor model override |
| `FORMATTER_PROVIDER` | `ollama` | `ollama`, `azure_openai` | Default formatter provider |
| `FORMATTER_MODEL` | `gpt-oss:20b` | provider-specific model/deployment | Default formatter model override |

## Ollama Settings

| Name | Default | Purpose |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | `http://host.docker.internal:11434` | Base URL used for chat and embedding calls |
| `OLLAMA_CHAT_MODEL` | `gpt-oss:20b` | Default Ollama model for answer/extractor/formatter calls |
| `OLLAMA_EMBED_MODEL` | `nomic-embed-text` | Default Ollama embedding model |

## Azure OpenAI Settings

| Name | Default | Purpose |
| --- | --- | --- |
| `AZURE_OPENAI_BASE_URL` | empty | Azure OpenAI base URL |
| `AZURE_OPENAI_API_KEY` | empty | Azure OpenAI API key |
| `AZURE_OPENAI_CHAT_MODEL` | empty | Azure deployment/model for chat-style calls |
| `AZURE_OPENAI_EMBED_MODEL` | empty | Azure deployment/model for embeddings |

Only the active provider settings are required. Ollama mode does not need Azure values, and Azure mode does not need Ollama-specific overrides.

## Seed Script Flags

| Flag | Default | Purpose |
| --- | --- | --- |
| `--input-path` | required | Clause file or directory to parse |
| `--collection-name` | `GSC-Internal-Policy` | Collection namespace to write into |
| `--database-url` | `postgresql://openwebui:openwebui@localhost:55432/openwebui` | Target Postgres database |
| `--chunk-table-name` | `supply_chain_chunks` | Shared chunk table |
| `--embedding-provider` | `ollama` | Embedding provider |
| `--embedding-model` | empty | Optional generic embedding-model override |
| `--embedding-dimensions` | `768` | Embedding dimension |
| `--ollama-base-url` | `http://host.docker.internal:11434` | Ollama base URL |
| `--ollama-embed-model` | `nomic-embed-text` | Default Ollama embedding model |
| `--azure-openai-base-url` | empty | Azure embedding endpoint |
| `--azure-openai-api-key` | empty | Azure API key |
| `--azure-openai-embed-model` | empty | Default Azure embedding model |
| `--request-timeout-seconds` | `120` | Embedding timeout |
| `--chunk-size` | `120` | Chunk size in words |
| `--chunk-overlap` | `20` | Chunk overlap in words |
| `--dry-run` | `false` | Parse only; do not embed or write |
| `--report-file` | empty | Optional JSON report path |
| `--replace-collection` | `false` | Clear the target collection before writing |
| `--delete-collection` | none | Delete an additional collection after the write |

## Recommended Local Defaults

For the local demo stack:

- `DEFAULT_COLLECTION_NAME=GSC-Internal-Policy`
- `ROUTER_MODE=extractor_assisted`
- `ENABLE_LLM_EXTRACTOR=true`
- `ENABLE_LLM_FORMATTER=true`
- `ANSWER_PROVIDER=ollama`
- `ANSWER_MODEL=gpt-oss:20b`
- `EXTRACTOR_PROVIDER=ollama`
- `EXTRACTOR_MODEL=gpt-oss:20b`
- `FORMATTER_PROVIDER=ollama`
- `FORMATTER_MODEL=gpt-oss:20b`
- `EMBEDDING_PROVIDER=ollama`
- `OLLAMA_CHAT_MODEL=gpt-oss:20b`
- `OLLAMA_EMBED_MODEL=nomic-embed-text`

`gpt-oss:20b` is not used as the embedding default because local Ollama returns `this model does not support embeddings` for `/api/embed` with that model.

## Recommended Seed Command

```bash
python scripts/ingest_supply_chain_txt.py \
  --input-path demo-data \
  --collection-name GSC-Internal-Policy \
  --replace-collection \
  --delete-collection supply_chain_tcs_demo
```
