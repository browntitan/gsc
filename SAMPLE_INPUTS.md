# Sample Inputs

This document gives example prompts for the `supplychain_tc_pipeline` model and shows the expected behavior.

## Notes

- The pipeline expects:
  - clause number
  - termset number
  - query text
- It accepts termset references in several forms:
  - `termset 1`
  - `termet 1`
  - `T&C 1`
  - `CTM-P-ST-001`
- All of those normalize to `001`.
- Clauses `1` through `11` are placeholder/template content in this demo corpus.
- Clause `12` is the strongest file for meaningful retrieval examples.

## Single-Shot Prompts

Use these when the user gives everything in one message.

```text
What does clause 12 say about warranty period for termset 1?
```

```text
What does clause 10 say about indemnity for termset 3?
```

```text
What does clause 3 say about indemnity for termset 1?
```

```text
What does clause 12 say about inspection and acceptance impacts for CTM-P-ST-006?
```

Expected behavior:

- the pipeline extracts clause, termset, and query
- the pipeline runs retrieval immediately
- the pipeline returns a grounded answer

## Typo-Tolerant Inputs

These test the LLM extractor and formatter.

```text
What does clause 12 say about warranty period for termet 1?
```

```text
What does clause 12 say about remedies for termset one hundred? 
```

```text
Clause 12 for termet 1 on warranty waivers
```

Expected behavior:

- the pipeline should still recover the intended termset when the signal is clear
- if the signal is not clear enough, it should ask for the missing field

## Missing-Field Prompts

These should trigger a follow-up question instead of retrieval.

```text
What does clause 12 say about warranty period?
```

Expected behavior:

```text
I have Clause 12 and your question. Which termset number should I search under?
```

```text
termset 2 indemnity
```

Expected behavior:

```text
I have termset 002 and your question. What clause number should I use?
```

```text
Clause 10
```

Expected behavior:

```text
I have Clause 10. What termset number should I use, and what would you like to know about it?
```

## Multi-Turn Examples

### Example 1: fill in the missing termset

User:

```text
What does clause 12 say about warranty period?
```

Assistant:

```text
I have Clause 12 and your question. Which termset number should I search under?
```

User:

```text
termset 1
```

Expected behavior:

- the pipeline reuses the earlier clause and question
- the pipeline normalizes `termset 1` to `001`
- retrieval runs

### Example 2: update only the clause

User:

```text
What does clause 12 say about warranty period for termset 1?
```

User:

```text
Clause 10
```

Expected behavior:

- the pipeline keeps the active termset and query
- it reruns retrieval with the updated clause

### Example 3: update only the termset

User:

```text
What does clause 12 say about warranty period for termset 1?
```

User:

```text
termset 6
```

Expected behavior:

- the pipeline keeps the active clause and query
- it reruns retrieval with termset `006`

### Example 4: follow-up explanation

User:

```text
What does clause 12 say about warranty period for termset 1?
```

User:

```text
Can you explain that more?
```

Expected behavior:

- the pipeline keeps the active clause, termset, and query
- it explains the active result more clearly

## Alias Inputs

These confirm backward-compatible termset parsing.

```text
What does clause 12 say about warranty period for T&C 1?
```

```text
What does clause 12 say about warranty period for tc 001?
```

```text
What does clause 12 say about warranty period for CTM-P-ST-001?
```

Expected behavior:

- all of these should resolve to termset `001`

## No-Hit / Low-Evidence Prompts

These are useful for testing failure handling.

```text
What does clause 12 say about spacecraft launch insurance for termset 1?
```

```text
What does clause 2 say about export control waivers for termset 7?
```

Expected behavior:

- the pipeline should return either:
  - a low-evidence grounded answer
  - or a no-hit style response asking the user to confirm identifiers or try a different query

## Placeholder-Provenance Prompts

These should surface the placeholder warning because clauses `1-11` are not authoritative in this demo dataset.

```text
What does clause 1 say about definitions for termset 2?
```

```text
What does clause 5 say about acceptance for termset 6?
```

Expected behavior:

- the answer should include a short warning that the result comes from placeholder/template content

## Seed Validation Inputs

These are useful after reseeding.

```text
What does clause 1 say about definitions for termset 2?
```

```text
What does clause 12 say about warranty period for termset 1?
```

```text
What does clause 12 say about warranty waivers for termset 7?
```

If these work, the likely basics are correct:

- the collection loaded
- clause metadata parsed correctly
- termset normalization is working
- retrieval is filtering on clause + termset
