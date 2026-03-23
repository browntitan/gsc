# Training Guide

This guide is for admins, operators, and internal users who need to learn how to use the supply-chain internal policy assistant in Open WebUI.

## Training Goal

By the end of training, the learner should be able to:

- understand what the assistant needs to run a search
- ask effective questions using clause and termset identifiers
- recognize when the assistant is reusing prior chat context
- identify placeholder/template results versus stronger source-backed results
- know how to validate that the seeded corpus and retrieval flow are working

## Audience

Use this guide for:

- Open WebUI admins
- internal policy operators
- business users who will query clause content
- trainers preparing a short demo or enablement session

## What The Assistant Does

The assistant is not a generic chatbot. It is a retrieval workflow over an internal policy corpus.

The assistant needs:

- clause number
- termset number
- query text

The current implementation:

- parses user messages and chat history inside the pipeline
- uses LLM-first extraction and formatting by default
- normalizes termset inputs such as `termset 1`, `termet 1`, `T&C 1`, and `CTM-P-ST-001`
- retrieves from the `GSC-Internal-Policy` collection in `supply_chain_chunks`

## Important Dataset Limitation

For this demo corpus:

- Clauses `1-11` are placeholder/template files
- Clause `12` is based on the provided transcription

Training should call this out clearly. Users should not mistake placeholder content for authoritative policy.

## Recommended Training Flow

### 1. Start with the mental model

Explain:

- the system is clause + termset + query driven
- it is designed to retrieve grounded policy text, not improvise
- it may ask follow-up questions when one of the required inputs is missing

### 2. Show one successful single-shot query

Use:

```text
What does clause 12 say about warranty period for termset 1?
```

Explain:

- clause `12` was detected
- termset `1` was normalized to `001`
- the rest of the sentence became the active query

### 3. Show a missing-field interaction

Use:

```text
What does clause 12 say about warranty period?
```

Then respond with:

```text
termset 1
```

Explain:

- the assistant asked only for the missing field
- it reused the prior clause and query from chat history

### 4. Show context reuse

After a successful query, send:

```text
Can you explain that more?
```

Explain:

- the assistant did not need the user to restate clause or termset
- it reused the active context from earlier turns

### 5. Show identifier update behavior

After a successful query, send:

```text
Clause 10
```

Explain:

- the assistant reused the active termset and active query
- only the clause changed

### 6. Show typo tolerance

Use:

```text
What does clause 12 say about warranty period for termet 1?
```

Explain:

- the extractor/formatter path handled the typo
- the normalized termset remained `001`

### 7. Show placeholder warning behavior

Use:

```text
What does clause 1 say about definitions for termset 2?
```

Explain:

- the assistant may answer from the seeded dataset
- the pipeline should warn that the result comes from placeholder/template content

## Trainer Script

Use this short sequence for a live training session:

1. Introduce the assistant as clause-and-termset-based retrieval.
2. Run a successful Clause 12 query.
3. Run a missing-field prompt and complete it on the next turn.
4. Run a follow-up explanation prompt.
5. Run a termset typo example.
6. Run a placeholder-clause example and point out the warning.
7. Show one SQL or UI validation check so users understand the system is corpus-backed.

## Good User Behavior To Teach

Teach users to:

- include the clause number when they know it
- include the termset number when they know it
- ask a focused business or legal question
- continue in the same chat if they want context reuse
- start a fresh search when the clause or termset changes substantially

Good examples:

```text
What does clause 12 say about warranty period for termset 1?
```

```text
What does clause 12 say about warranty waivers for termset 7?
```

```text
What does clause 10 say about indemnity for CTM-P-ST-003?
```

## Bad Or Weak User Behavior To Teach Against

Warn users against:

- asking without any identifiers
- expecting the assistant to know which clause they mean without context
- treating placeholder results as final policy guidance
- assuming a broad topic will always produce a precise answer

Weak examples:

```text
Tell me about indemnity
```

```text
What does it say?
```

```text
Can you review everything?
```

## Admin Training Checklist

Admins should be able to do all of the following:

- start the Docker stack
- confirm the pipeline is loaded in Pipelines
- run the seed script
- verify that `GSC-Internal-Policy` exists in Postgres
- explain the difference between chat-time parsing and seed-time parsing
- explain why Ollama chat defaults use `gpt-oss:20b`
- explain why embeddings stay on `nomic-embed-text`

## Validation Checklist

Run these checks during training or after setup changes.

### 1. Confirm the model is loaded

```bash
curl -sS -H 'Authorization: Bearer 0p3n-w3bu!' http://localhost:9099/models
```

### 2. Confirm the collection exists

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT collection_name, COUNT(*) AS row_count FROM supply_chain_chunks GROUP BY collection_name ORDER BY collection_name;"
```

### 3. Confirm clause and termset coverage

```bash
docker compose exec -T postgres \
  psql -U openwebui -d openwebui \
  -c "SELECT clause_number, tc_number AS termset_number, COUNT(*) AS row_count FROM supply_chain_chunks WHERE collection_name = 'GSC-Internal-Policy' GROUP BY clause_number, tc_number ORDER BY clause_number::int, tc_number;"
```

### 4. Confirm a known prompt works

```text
What does clause 12 say about warranty period for termset 1?
```

## Common Questions

### Why does the assistant ask for a termset number?

Because retrieval is filtered on both clause and termset. Without termset, the assistant does not know which applicable policy slice to search.

### Why does it sometimes warn about placeholder content?

Because the current demo dataset includes placeholder/template files for Clauses `1-11`.

### Why does the system use `gpt-oss:20b` for chat but not embeddings?

Because local Ollama supports chat/generation with `gpt-oss:20b`, but `/api/embed` returns `this model does not support embeddings` for that model. The embedding path therefore stays on `nomic-embed-text`.

## Suggested Practice Exercise

Ask the learner to complete this sequence:

1. Ask a single-shot Clause 12 query.
2. Ask a query missing the termset.
3. Provide the missing termset on the next turn.
4. Ask for a follow-up explanation.
5. Change only the clause.
6. Run one placeholder-clause query and explain the warning.

If they can do all six correctly, they understand the main workflow.

## Companion Docs

Use these together:

- `README.md`
- `USER_GUIDE.md`
- `CONFIG_REFERENCE.md`
- `SAMPLE_INPUTS.md`
