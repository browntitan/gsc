# OpenWebUI Internal Policy Pipeline Demo

This repo runs a single Open WebUI Pipelines model for internal policy retrieval against Postgres + pgvector.

Primary runtime path:

- `pipelines/supplychain_tc_pipeline.py`
- `scripts/ingest_supply_chain_txt.py`
- shared table: `supply_chain_chunks`
- default collection: `GSC-Internal-Policy`

The chat pipeline parses user questions and chat history. The seed script parses the internal policy clause files in `demo-data/` and writes embedded chunks into pgvector.

Supporting docs:

- `USER_GUIDE.md`
- `CONFIG_REFERENCE.md`
- `SAMPLE_INPUTS.md`
- `TRAINING_GUIDE.md`
- `TECHNICAL_ARCHITECTURE.md`

## Architecture

Runtime:

- user chats in Open WebUI
- `supplychain_tc_pipeline` extracts `clause_number`, `termset_number`, and `query_text`
- the pipeline embeds the query
- Postgres + pgvector is filtered by `collection_name`, `clause_number_norm`, and `tc_number_norm`
- the pipeline returns a grounded answer with citations

Seeding:

- `scripts/ingest_supply_chain_txt.py`
- reads `demo-data/01_*.txt` through `demo-data/12_*.txt`
- parses the top clause heading, for example `1. Definitions`
- parses the `Applicable For` block
- duplicates each chunk once per applicable termset
- stores the normalized termset in the existing `tc_number` columns for retrieval compatibility

## LLM-First Input Extraction

The pipeline defaults to LLM-first extraction and formatting for input understanding.

Default behavior:

- `ROUTER_MODE=extractor_assisted`
- `ENABLE_LLM_EXTRACTOR=true`
- `ENABLE_LLM_FORMATTER=true`
- `ANSWER_PROVIDER=ollama`
- `ANSWER_MODEL=gpt-oss:20b`
- `EXTRACTOR_PROVIDER=ollama`
- `EXTRACTOR_MODEL=gpt-oss:20b`
- `FORMATTER_PROVIDER=ollama`
- `FORMATTER_MODEL=gpt-oss:20b`

The pipeline still keeps deterministic parsing as fallback and validation. It normalizes:

- `termset 1` -> `001`
- `termet 1` -> `001`
- `CTM-P-ST-001` -> `001`

The chat runtime uses `termset` as the primary user-facing term, but it still accepts legacy `T&C` phrasing as an alias.

Embedding stays on the Ollama embedding model `nomic-embed-text`. I verified that local Ollama returns `this model does not support embeddings` for `gpt-oss:20b`, so using it as the embedding default would break ingestion and retrieval.

## Local Quick Start

### 1. Start the stack

```bash
docker compose up -d
```

Default ports:

- Open WebUI: `http://localhost:3001`
- Pipelines: `http://localhost:9099`
- Postgres: `localhost:55432`

### 2. Confirm the pipeline loaded

```bash
curl -sS -H 'Authorization: Bearer 0p3n-w3bu!' http://localhost:9099/models
```

You should see:

- `supplychain_tc_pipeline`

### 3. Seed the internal policy collection

```bash
python scripts/ingest_supply_chain_txt.py \
  --input-path demo-data \
  --collection-name GSC-Internal-Policy \
  --replace-collection \
  --delete-collection supply_chain_tcs_demo
```

This:

- parses the clause files in `demo-data/`
- writes the new collection `GSC-Internal-Policy`
- removes the old local demo collection `supply_chain_tcs_demo`

### 4. Open Open WebUI

Open:

- `http://localhost:3001`

Then choose:

- `Supply Chain Internal Policy Pipeline`

## Example User Prompts

Single-shot:

```text
What does clause 3 say about indemnity for termset 1?
```

Identifier update:

```text
Clause 5
```

Termset update:

```text
termset 2
```

Follow-up:

```text
Can you explain that more?
```

## Validation Commands

### Check the collection contents

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT collection_name, COUNT(*) AS row_count, MIN(vector_dims(embedding)) AS min_dims, MAX(vector_dims(embedding)) AS max_dims FROM supply_chain_chunks GROUP BY collection_name ORDER BY collection_name;"
```

Expected local result after reseeding:

- `collection_name = GSC-Internal-Policy`
- `row_count > 0`
- `min_dims = 768`
- `max_dims = 768`

### Check clause/termset coverage

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT collection_name, clause_number, tc_number AS termset_number, COUNT(*) AS row_count FROM supply_chain_chunks WHERE collection_name = 'GSC-Internal-Policy' GROUP BY collection_name, clause_number, tc_number ORDER BY clause_number::int, tc_number;"
```

### Check chat completion through Pipelines

```bash
curl -sS -X POST http://localhost:9099/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "supplychain_tc_pipeline",
    "stream": false,
    "messages": [
      {"role": "user", "content": "What does clause 3 say about indemnity for termset 1?"}
    ]
  }'
```

## Remote / Enterprise Pattern

For a remote Pipelines server, upload the pipeline file directly:

```bash
curl -sS -X POST \
  -H "Authorization: Bearer $PIPELINES_API_KEY" \
  -F "file=@pipelines/supplychain_tc_pipeline.py" \
  https://YOUR-PIPELINES-HOST/pipelines/upload
```

For enterprise deployment:

- point the pipeline valves at the enterprise Postgres database
- seed the target collection with `scripts/ingest_supply_chain_txt.py`
- connect Open WebUI to the Pipelines endpoint
- keep the same table shape and the same collection model

## Config

The important runtime knobs are:

- `DATABASE_URL`
- `CHUNK_TABLE_NAME`
- `DEFAULT_COLLECTION_NAME`
- `ROUTER_MODE`
- `ENABLE_LLM_EXTRACTOR`
- `ENABLE_LLM_FORMATTER`
- `EXTRACTOR_PROVIDER`
- `EXTRACTOR_MODEL`
- `FORMATTER_PROVIDER`
- `FORMATTER_MODEL`
- `ANSWER_PROVIDER`
- `EMBEDDING_PROVIDER`

See `CONFIG_REFERENCE.md` for the full list.
