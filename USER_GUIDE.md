# Supply-Chain Internal Policy User Guide

This guide is for Open WebUI admins and operators using the consolidated single-pipeline implementation in this repo.

## Mental Model

The runtime path is:

- Open WebUI chat
- `supplychain_tc_pipeline`
- `supply_chain_chunks` in Postgres + pgvector
- grounded answer returned to chat

The pipeline parses user questions and chat history.

The seed script parses the policy source files separately.

## What The Pipeline Expects

The pipeline wants three inputs:

- `clause_number`
- `termset_number`
- `query_text`

It uses `termset` as the main user-facing concept, but it also accepts:

- `T&C 2`
- `termset 2`
- `termet 2`
- `CTM-P-ST-002`

All of those normalize to the same stored termset value: `002`.

## LLM-First Extraction

The input-understanding path is Azure OpenAI-backed by default.

Defaults:

- `ROUTER_MODE=extractor_assisted`
- `ENABLE_LLM_EXTRACTOR=true`
- `ENABLE_LLM_FORMATTER=true`
- `AZURE_OPENAI_API_VERSION=2024-02-01`
- `AZURE_OPENAI_DEPLOYMENT_NAME=<chat-deployment>`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME=<ada-embedding-deployment>`
- `EMBEDDING_DIMENSIONS=1536`

The flow is:

1. the pipeline reads the latest user message
2. it derives prior clause/termset/question context from earlier user turns
3. the extractor proposes structured fields
4. the formatter normalizes the fields into strict JSON
5. deterministic parsing validates and normalizes the final values
6. retrieval runs only when all required inputs are present

If the extractor or formatter fails, the pipeline falls back to deterministic parsing or a missing-field prompt.

Query embeddings come from Azure OpenAI using the separate embedding deployment valve. The stored collection must use the same 1536-dimensional embedding family for retrieval to work.

Before the first query, populate these valves:

- `DATABASE_URL`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT_NAME`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME`
- `AZURE_OPENAI_API_VERSION`

## Example Chat Behavior

### Single-shot request

User:

```text
What does clause 3 say about indemnity for termset 1?
```

Expected behavior:

- clause = `3`
- termset = `001`
- query = `indemnity`
- retrieval runs immediately

### Missing clause number

User:

```text
termset 2 indemnity
```

Expected behavior:

```text
I have termset 002 and your question. What clause number should I use?
```

### Identifier update

User:

```text
Clause 5
```

Expected behavior:

- the pipeline keeps the active termset and active question
- retrieval runs again with the updated clause

### Follow-up

User:

```text
Can you explain that more?
```

Expected behavior:

- the pipeline keeps the active clause, termset, and question
- it explains the current retrieval result more clearly

### New search

User:

```text
Now check clause 8 for termset 4 about delivery timing
```

Expected behavior:

- the pipeline replaces the active identifiers
- the pipeline runs a new retrieval

## Seeding The Policy Corpus

The seeding path is separate from the chat runtime.

Use:

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

The seed script:

- reads clause files `01_*.txt` through `12_*.txt`
- parses the top clause heading
- parses the `Applicable For` termset block
- normalizes termsets to three digits
- removes structural boilerplate before chunking
- duplicates chunks once per applicable termset
- embeds each chunk with Azure OpenAI
- writes the results into `supply_chain_chunks`

## What Metadata Is Stored

Each row stores:

- `clause_number`
- `tc_number`

For this implementation, `tc_number` is the normalized termset number.

Additional metadata is stored in `metadata`, including:

- `termset_number`
- `termset_code_full`
- `all_applicable_termsets`
- `clause_title`
- `source_status`
- `is_placeholder`

## Placeholder Warning

Clauses 1-11 in `demo-data/` are still placeholder/template content in this repo.

When retrieval hits one of those files, the pipeline prepends a short warning so operators can distinguish template material from authoritative content.

Clause 12 is the only file sourced from the provided transcription in this demo set.

## Local Validation

### Confirm the collection exists

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT collection_name, COUNT(*) AS row_count FROM supply_chain_chunks GROUP BY collection_name ORDER BY collection_name;"
```

### Confirm vectors were written

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT MIN(vector_dims(embedding)) AS min_dims, MAX(vector_dims(embedding)) AS max_dims FROM supply_chain_chunks WHERE collection_name = 'GSC-Internal-Policy';"
```

### Inspect clause and termset coverage

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT clause_number, tc_number AS termset_number, COUNT(*) AS row_count FROM supply_chain_chunks WHERE collection_name = 'GSC-Internal-Policy' GROUP BY clause_number, tc_number ORDER BY clause_number::int, tc_number;"
```

### Check the pipeline model

```bash
curl -sS -H 'Authorization: Bearer 0p3n-w3bu!' http://localhost:9099/models
```

You should see:

- `supplychain_tc_pipeline`

## Troubleshooting

### The model does not appear in Open WebUI

Check:

```bash
curl -sS -H 'Authorization: Bearer 0p3n-w3bu!' http://localhost:9099/models
```

### The seed script skipped files

Run:

```bash
python scripts/ingest_supply_chain_txt.py \
  --input-path demo-data \
  --dry-run \
  --report-file ingest-report.json
```

Then inspect `skipped_sections` in the JSON report.

### Retrieval returns no hits

Check:

- `DEFAULT_COLLECTION_NAME`
- clause number
- termset number
- collection contents in `supply_chain_chunks`

### Provider failures

This pipeline is Azure OpenAI only.

Check:

- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_DEPLOYMENT_NAME`
- `AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME`
- `AZURE_OPENAI_API_VERSION`
- `DATABASE_URL`
- seeded vectors are 1536-dimensional and were written with the same Azure embedding deployment family

## Enterprise Target Pattern

This repo is structured so the same pieces can be moved to an enterprise Azure deployment:

- upload `pipelines/supplychain_tc_pipeline.py` to the enterprise Pipelines service
- point `DATABASE_URL` at the enterprise Azure Gov Postgres database
- point the Azure OpenAI valves at the Azure Gov OpenAI resource and deployments
- run `scripts/ingest_supply_chain_txt.py` against the enterprise database
- keep the same `collection_name` model for isolation

That lets you reuse the same chat pipeline and the same seed path without introducing a separate retrieval service.
