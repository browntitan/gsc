# GSC Function App — Detailed Function Reference

## Overview

This Azure Function App is a data pipeline for processing GSC (Global Supply Chain) policy documents. It takes raw `.aspx` SharePoint page exports stored in Azure Blob Storage, cleans them into plain text, and optionally generates vector embeddings stored in a PostgreSQL database with pgvector for RAG (Retrieval-Augmented Generation) search.

The pipeline has three stages that can be run independently or combined:

1. **Clean** — Strip HTML/ASP.NET markup from `.aspx` files and save as plain `.txt`
2. **Embed** — Parse clause structure from cleaned text, chunk it, generate embeddings via Azure OpenAI, and store in PostgreSQL
3. **Process** — Run both Clean and Embed in a single call

---

## Architecture

```
Azure Blob Storage                    Azure Function App                         PostgreSQL + pgvector
+----------------+                   +------------------------+                 +--------------------+
| "gsc"          |  -- clean ------> | aspx_cleaner.py        |                 |                    |
| container      |     (read .aspx)  |  * strip ASP.NET       |                 |                    |
|                |                   |    page directives     |                 |                    |
| *.aspx files   |                   |  * extract SharePoint  |                 |                    |
+----------------+                   |    canvas content      |                 |                    |
                                     |  * remove scripts,     |                 |                    |
+----------------+                   |    styles, nav, etc    |                 |                    |
| "gsc-cleaned"  |  <-- output ----  |  * extract visible     |                 |                    |
| container      |     (write .txt)  |    text via BS4        |                 |                    |
|                |                   +------------------------+                 |                    |
| *.txt files    |  -- embed ------> +------------------------+                 | gsc_vector_rag     |
|                |     (read .txt)   | pgvector_ingest.py     | -- upsert ----> | table              |
+----------------+                   |  * parse clause        |                 |                    |
                                     |    heading & number    |                 | * clause_number    |
                                     |  * extract term set    |                 | * chunk_text       |
                                     |    codes               |                 | * embedding        |
                                     |  * segment by          |                 | * metadata         |
                                     |    section headings    |                 | * ...              |
                                     |  * chunk text (120     |                 +--------------------+
                                     |    words, 20 overlap)  |
                                     |  * call Azure OpenAI   |
                                     |    for embeddings      |
                                     |  * upsert to postgres  |
                                     +------------------------+
```

---

## Functions

### 1. `health_check` — System Health Report

**Route:** `GET /api/gsc/health`
**Auth:** Function key required (pass as `?code=<key>` query parameter)

#### Purpose

Returns a comprehensive diagnostic report of the entire system. Use this as the **first endpoint to test** after deployment to verify all dependencies are correctly configured.

#### Input

None. This is a GET request with no body, no headers, and no query parameters (other than the function key).

#### Process

1. **Environment variable check:** Inspects all 4 required environment variables and reports which are present and which are missing:
   - `cdooaipocdata1_STORAGE` — Azure Blob Storage connection string
   - `PGVECTOR_DATABASE_URL` — PostgreSQL connection string
   - `AZURE_OPENAI_BASE_URL` — Azure OpenAI endpoint URL
   - `AZURE_OPENAI_API_KEY` — Azure OpenAI API key
2. **Optional settings report:** Lists current values (or defaults) for all configurable settings: `OUTPUT_CONTAINER`, `CHUNK_TABLE_NAME`, `COLLECTION_NAME`, `AZURE_OPENAI_EMBEDDING_DEPLOYMENT`, `AZURE_OPENAI_EMBEDDINGS_PATH`, `EMBEDDING_DIMENSIONS`, `REQUEST_TIMEOUT_SECONDS`, `CHUNK_SIZE`, `CHUNK_OVERLAP`
3. **Blob storage connectivity check:** Connects to Azure Blob Storage using `cdooaipocdata1_STORAGE` and verifies both the `gsc` (source) container and `gsc-cleaned` (output) container exist and are accessible
4. **PostgreSQL connectivity check:** Connects to the PostgreSQL database using `PGVECTOR_DATABASE_URL` and executes `SELECT 1` to verify the connection works
5. **Overall health determination:** Returns `"healthy"` only if all required env vars are present AND blob storage is reachable AND PostgreSQL is reachable

#### Output

```json
{
  "status": "healthy",
  "checks": {
    "environment": {
      "required": {
        "cdooaipocdata1_STORAGE": {"present": true},
        "PGVECTOR_DATABASE_URL": {"present": true},
        "AZURE_OPENAI_BASE_URL": {"present": true},
        "AZURE_OPENAI_API_KEY": {"present": true}
      },
      "missing_required": [],
      "defaults": {
        "OUTPUT_CONTAINER": "gsc-cleaned",
        "CHUNK_TABLE_NAME": "gsc_vector_rag",
        "COLLECTION_NAME": "gsc-internal-policies",
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": "text-embedding-ada-002",
        "AZURE_OPENAI_EMBEDDINGS_PATH": "/embeddings",
        "EMBEDDING_DIMENSIONS": "1536",
        "REQUEST_TIMEOUT_SECONDS": "120",
        "CHUNK_SIZE": "120",
        "CHUNK_OVERLAP": "20"
      }
    },
    "blob_storage": {
      "status": "ok",
      "source_container": "gsc",
      "output_container": "gsc-cleaned"
    },
    "postgres": {
      "status": "ok",
      "database": "reachable",
      "chunk_table_name": "gsc_vector_rag",
      "collection_name": "gsc-internal-policies",
      "query_result": {"ok": 1}
    }
  },
  "functions": [
    "clean_one_gsc_blob",
    "embed_one_cleaned_blob",
    "process_one_gsc_blob",
    "clean_gsc_batch",
    "embed_cleaned_batch",
    "process_gsc_batch",
    "health_check"
  ]
}
```

- **HTTP 200** if all checks pass (`"status": "healthy"`)
- **HTTP 503** if any check fails (`"status": "unhealthy"` — check the individual `checks` sections to see what failed)

---

### 2. `clean_one_gsc_blob` — Clean a Single ASPX File

**Route:** `POST /api/gsc/clean-one`
**Auth:** Function key required

#### Purpose

Takes a single raw `.aspx` file from Azure Blob Storage, strips all HTML and ASP.NET markup, extracts the visible text content, and saves the cleaned output as a `.txt` file in the output container.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "blob": "1.-Definitions.aspx"
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `blob` | string | Yes | The blob name within the `gsc` container. Must end with `.aspx`. This is just the filename (or path within the container), NOT a full URL. |

#### Process — Step by Step

1. **Validate request**: Parses the JSON body, checks that the `blob` parameter exists, is a non-empty string, and ends with `.aspx`. Returns HTTP 400 with a descriptive error if validation fails.

2. **Connect to Azure Blob Storage**: Creates a `BlobServiceClient` using the connection string from the `cdooaipocdata1_STORAGE` environment variable.

3. **Download the raw ASPX file**: Reads the entire blob content from the `gsc` container as raw bytes, then decodes as UTF-8 text (with error-ignoring for malformed bytes).

4. **Clean the ASPX content** (handled by `aspx_cleaner.py`):

   **Step 4a — Strip ASP.NET page directives:**
   Removes all `<%@ ... %>` directives at the start of the file. These are ASP.NET server-side directives that have no visible content.

   **Step 4b — Extract SharePoint canvas content:**
   Searches for a `<mso:CanvasContent1>` tag, which is where SharePoint modern pages store their actual page content. If found, extracts only the inner HTML of that tag (and HTML-unescapes it). If not found, uses the entire document. This step is critical because SharePoint `.aspx` files contain a lot of framework markup around the actual content.

   **Step 4c — Parse HTML and remove non-content elements:**
   Parses the HTML using BeautifulSoup with the `html.parser` backend. Then removes all elements with these tags:
   - `<script>` — JavaScript code
   - `<style>` — CSS stylesheets
   - `<noscript>` — No-JavaScript fallback content
   - `<header>` — Page header/navigation
   - `<footer>` — Page footer
   - `<nav>` — Navigation menus
   - `<aside>` — Sidebar content
   - `<svg>` — Vector graphics/icons

   **Step 4d — Extract visible text:**
   Calls `soup.get_text(separator="\n")` to extract all remaining visible text, with newlines between elements.

   **Step 4e — Normalize whitespace:**
   Strips leading/trailing whitespace from each line and removes completely blank lines, producing clean, readable plain text.

5. **Upload cleaned text**: Creates a new blob in the `gsc-cleaned` container with the same name but `.txt` extension (e.g., `1.-Definitions.aspx` becomes `1.-Definitions.txt`). Overwrites if the blob already exists.

6. **Return result**: Returns a JSON response with cleaning statistics.

#### Output

```json
{
  "status": "cleaned",
  "source_container": "gsc",
  "source_blob_name": "1.-Definitions.aspx",
  "output_container": "gsc-cleaned",
  "cleaned_output_blob_name": "1.-Definitions.txt",
  "cleaned_characters": 4523
}
```

#### Error Responses

| HTTP Status | Error Type | When |
|---|---|---|
| 400 | `InvalidJson` | Request body is not valid JSON |
| 400 | `InvalidRequest` | Missing `blob` parameter, empty string, or doesn't end with `.aspx` |
| 404 | `BlobNotFound` | The specified blob does not exist in the `gsc` container |
| 500 | `RuntimeError` | Storage connection failure, missing `cdooaipocdata1_STORAGE` env var, or other runtime error |

---

### 3. `embed_one_cleaned_blob` — Embed a Single Cleaned Text File

**Route:** `POST /api/gsc/embed-one`
**Auth:** Function key required

#### Purpose

Takes an already-cleaned `.txt` file from the `gsc-cleaned` container, parses its clause structure, splits it into overlapping text chunks, generates a vector embedding for each chunk using Azure OpenAI, and upserts the results into the PostgreSQL database for RAG search.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "blob": "1.-Definitions.txt"
}
```

| Parameter | Type | Required | Description |
|---|---|---|---|
| `blob` | string | Yes | The blob name within the `gsc-cleaned` container. Must end with `.txt`. |

#### Process — Step by Step

1. **Validate request**: Checks `blob` parameter exists and ends with `.txt`.

2. **Download cleaned text**: Reads the `.txt` file from the `gsc-cleaned` container.

3. **Parse clause structure** (handled by `pgvector_ingest.py → parse_clause_document_text()`):

   **Step 3a — Clean residual markup:**
   Even in cleaned text, there may be residual HTML entities or tags. This step replaces `\r\n` and `\r` with `\n`, removes any remaining `<script>` and `<style>` blocks, converts `<br>` to newlines and `</p>` to double-newlines, strips all remaining HTML tags, unescapes HTML entities (`&amp;` → `&`, etc.), and normalizes whitespace.

   **Step 3b — Find the clause heading:**
   Scans each line looking for the pattern `<number>. <title>` (e.g., `"1. Definitions"`, `"12. Warranty"`). Extracts the clause number (as a string like `"1"`) and the clause title (like `"Definitions"`). If no heading is found, the file is skipped with an error.

   **Step 3c — Extract applicable term set codes:**
   Looks for an "Applicable For" block followed by term set codes matching the pattern `CTM-P-ST-XXX` (e.g., `CTM-P-ST-001`, `CTM-P-ST-012`). These codes identify which contract term sets this clause applies to. The codes are normalized to 3-digit padded numbers (e.g., `"001"`, `"012"`).

   **Step 3d — Remove boilerplate content:**
   Strips out non-substantive content including:
   - Lines starting with "Repository note"
   - "Table of Contents" sections
   - "Back to homepage" links
   - The "Applicable For" block itself (already extracted)

4. **Segment the clause body** (`segment_clause_body()`):

   Splits the cleaned clause body into logical segments based on section headings. Recognized major headings:
   - **"Intent"** — The purpose/intent of the clause
   - **"Common Exceptions / Suggested Responses"** — Negotiation guidance
   - **"Suggested Responses"** — Alternative heading for the above
   - **"Additional Resources"** — Related references
   - **"References"** — Source references

   Also recognizes subparagraph headings like "Subparagraph A", "Subparagraph B".

   Each segment gets a composite title. Examples:
   - `"Overview"` (text before any heading)
   - `"Intent"`
   - `"Common Exceptions / Suggested Responses"`
   - `"Common Exceptions / Suggested Responses | Subparagraph B"`

5. **Chunk each segment** (`chunk_text()`):

   Each segment's text is split into word-based chunks using a sliding window approach:
   - **Chunk size:** 120 words (configurable via `CHUNK_SIZE` env var)
   - **Overlap:** 20 words (configurable via `CHUNK_OVERLAP` env var)
   - This means each chunk shares 20 words with the previous chunk, ensuring context isn't lost at boundaries

   The segment title is prepended to each chunk so the embedding captures the section context. For example, a chunk might look like:
   ```
   Intent
   This clause defines the key terms used throughout the contract...
   ```

6. **Generate embeddings** (`embed_text()` via Azure OpenAI):

   For each text chunk, sends a POST request to the Azure OpenAI embedding endpoint:
   - **URL:** `{AZURE_OPENAI_BASE_URL}{AZURE_OPENAI_EMBEDDINGS_PATH}` (default path: `/embeddings`)
   - **Headers:** `api-key: {AZURE_OPENAI_API_KEY}`, `Content-Type: application/json`
   - **Body:** `{"model": "text-embedding-ada-002", "input": "<chunk text>"}`
   - **Response:** Extracts the embedding vector from `response.data[0].embedding`
   - **Vector dimensions:** 1536 floats (configurable via `EMBEDDING_DIMENSIONS`)
   - **Timeout:** 120 seconds per request (configurable via `REQUEST_TIMEOUT_SECONDS`)

   The embedding model is configurable via `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` (default: `text-embedding-ada-002`).

7. **Ensure database schema** (`ensure_schema()`):

   On first run, creates the necessary PostgreSQL infrastructure:
   - Enables `vector` extension (for pgvector)
   - Enables `pgcrypto` extension (for `gen_random_uuid()`)
   - Creates the `gsc_vector_rag` table if it doesn't exist (see Database Schema section below)
   - Creates three indexes:
     - **Unique index** on `(collection_name, external_id)` — prevents duplicate rows
     - **Filter index** on `(collection_name, clause_number_norm, tc_number_norm)` — speeds up clause/term set lookups
     - **HNSW vector index** on `embedding` with `vector_cosine_ops` — enables fast approximate nearest-neighbor search

8. **Upsert rows** (`upsert_rows()`):

   For each chunk, and for each applicable term set code, inserts a row into the database. If a clause applies to 3 term sets and produces 4 chunks, that's 12 rows total.

   Uses `INSERT ... ON CONFLICT (collection_name, external_id) DO UPDATE` — so re-processing the same file safely overwrites previous data without creating duplicates.

   The `external_id` is a SHA-256 hash of `"{collection_name}|{source_name}|{clause_number}|{termset_number}|{chunk_index}"`, ensuring deterministic, collision-free identifiers.

   Each row contains:

   | Field | Example Value |
   |---|---|
   | `collection_name` | `"gsc-internal-policies"` |
   | `external_id` | `"a1b2c3d4..."` (SHA-256 hash) |
   | `clause_number` | `"1"` |
   | `clause_number_norm` | `"1"` (uppercase, no spaces) |
   | `tc_number` | `"001"` (nullable) |
   | `tc_number_norm` | `"001"` (nullable) |
   | `topic` | `"definitions"` (from clause title) |
   | `source_doc` | `"1.-Definitions.txt"` |
   | `section_title` | `"Clause 1 - Definitions \| Intent"` |
   | `chunk_text` | The 120-word text chunk |
   | `guidance_text` | First sentence of the chunk (up to 400 chars) |
   | `metadata` | JSON with source_format, source_status, term set info, chunk_index |
   | `embedding` | 1536-float vector |

#### Output

```json
{
  "status": "embedded",
  "source_container": "gsc-cleaned",
  "source_blob_name": "1.-Definitions.txt",
  "embed_executed": true,
  "embedding_report": {
    "source_name": "1.-Definitions.txt",
    "collection_name": "gsc-internal-policies",
    "chunk_table_name": "gsc_vector_rag",
    "rows_prepared": 12,
    "rows_written": 12,
    "skipped": [],
    "skipped_count": 0
  },
  "skip_info": []
}
```

If the clause parsing fails (e.g., no clause heading found), the file is reported in `skipped` with a reason, and `rows_written` will be 0.

#### Error Responses

| HTTP Status | Error Type | When |
|---|---|---|
| 400 | `InvalidJson` | Request body is not valid JSON |
| 400 | `InvalidRequest` | Missing `blob` parameter or doesn't end with `.txt` |
| 404 | `BlobNotFound` | The specified blob does not exist in `gsc-cleaned` container |
| 500 | `RuntimeError` | Database connection failure, embedding API failure, missing env vars |

---

### 4. `process_one_gsc_blob` — Clean + Embed a Single ASPX File

**Route:** `POST /api/gsc/process-one`
**Auth:** Function key required

#### Purpose

Combines `clean_one_gsc_blob` and `embed_one_cleaned_blob` into a single HTTP call. Cleans the raw ASPX file, saves the cleaned text, and then immediately generates embeddings — all without requiring a second request.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "blob": "1.-Definitions.aspx",
  "embed": true
}
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `blob` | string | Yes | — | Blob name in `gsc` container. Must end with `.aspx`. |
| `embed` | boolean | No | `true` | Set to `false` to only clean without generating embeddings. |

#### Process

1. Runs the full **clean pipeline** (identical to `clean_one_gsc_blob` — see Section 2 above)
2. If `embed` is `true`:
   - Takes the cleaned text **directly from memory** (does NOT re-download from blob storage — this is more efficient than calling clean and embed separately)
   - Runs the full **embed pipeline** (identical to `embed_one_cleaned_blob` — see Section 3 above)
3. If `embed` is `false`:
   - Skips the embedding step entirely
   - Only the cleaned `.txt` file is written to blob storage

#### Output

```json
{
  "status": "processed",
  "source_container": "gsc",
  "source_blob_name": "1.-Definitions.aspx",
  "output_container": "gsc-cleaned",
  "cleaned_output_blob_name": "1.-Definitions.txt",
  "cleaned_characters": 4523,
  "embed_requested": true,
  "embed_executed": true,
  "embedding_report": {
    "source_name": "1.-Definitions.txt",
    "collection_name": "gsc-internal-policies",
    "chunk_table_name": "gsc_vector_rag",
    "rows_prepared": 12,
    "rows_written": 12,
    "skipped": [],
    "skipped_count": 0
  },
  "skip_info": []
}
```

When `embed` is `false`, the output will show `"embed_requested": false`, `"embed_executed": false`, and `"embedding_report": null`.

---

### 5. `clean_gsc_batch` — Clean Multiple ASPX Files

**Route:** `POST /api/gsc/clean-batch`
**Auth:** Function key required

#### Purpose

Lists blobs in the `gsc` container and cleans multiple `.aspx` files in a single request. Useful for bulk-processing all files at once.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "limit": 5,
  "prefix": ""
}
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | No | `5` | Maximum number of `.aspx` blobs to process. Must be > 0. |
| `prefix` | string | No | `""` | Only process blobs whose names start with this prefix. Use `""` for all blobs. Example: `"1."` to only process files starting with `1.` |

#### Process

1. **List blobs:** Enumerates all blobs in the `gsc` container, optionally filtered by the `prefix` parameter.
2. **Filter:** Skips any blob that doesn't end with `.aspx` (these appear in the `skipped_items` array with reason `"Not an .aspx blob"`).
3. **Process:** For each `.aspx` blob (up to `limit`), runs the full clean pipeline (same as `clean_one_gsc_blob`).
4. **Error resilience:** If one blob fails, the error is recorded in the `errors` array and processing continues with the next blob. The batch does not abort on individual failures.

#### Output

```json
{
  "status": "completed",
  "stage": "clean",
  "source_container": "gsc",
  "output_container": "gsc-cleaned",
  "prefix": "",
  "limit": 5,
  "processed_count": 5,
  "skipped_count": 0,
  "error_count": 0,
  "processed_items": [
    {
      "status": "cleaned",
      "source_container": "gsc",
      "source_blob_name": "1.-Definitions.aspx",
      "output_container": "gsc-cleaned",
      "cleaned_output_blob_name": "1.-Definitions.txt",
      "cleaned_characters": 4523
    }
  ],
  "skipped_items": [],
  "errors": []
}
```

---

### 6. `embed_cleaned_batch` — Embed Multiple Cleaned Text Files

**Route:** `POST /api/gsc/embed-batch`
**Auth:** Function key required

#### Purpose

Lists blobs in the `gsc-cleaned` container and embeds multiple `.txt` files in a single request. Run this after `clean_gsc_batch` to embed all the cleaned files.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "limit": 5,
  "prefix": "",
  "embed": true
}
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | No | `5` | Maximum number of `.txt` blobs to embed |
| `prefix` | string | No | `""` | Only process blobs whose names start with this prefix |
| `embed` | boolean | No | `true` | Must be `true` for this endpoint to do anything useful |

#### Process

Same structure as `clean_gsc_batch`, but:
- Reads from the `gsc-cleaned` container instead of `gsc`
- Filters for `.txt` files instead of `.aspx`
- Runs the embed pipeline instead of the clean pipeline

#### Output

```json
{
  "status": "completed",
  "stage": "embed",
  "source_container": "gsc-cleaned",
  "prefix": "",
  "limit": 5,
  "processed_count": 5,
  "skipped_count": 0,
  "error_count": 0,
  "processed_items": [...],
  "skipped_items": [],
  "errors": []
}
```

---

### 7. `process_gsc_batch` — Clean + Embed Multiple ASPX Files

**Route:** `POST /api/gsc/process-batch`
**Auth:** Function key required

#### Purpose

Lists blobs in the `gsc` container and runs the full clean+embed pipeline on multiple files in a single request. This is the most convenient endpoint for bulk processing.

#### Input

**Headers:** `Content-Type: application/json`

**Body:**
```json
{
  "limit": 5,
  "prefix": "",
  "embed": true
}
```

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | integer | No | `5` | Maximum number of `.aspx` blobs to process |
| `prefix` | string | No | `""` | Only process blobs whose names start with this prefix |
| `embed` | boolean | No | `true` | Whether to generate embeddings after cleaning. Set `false` to only clean. |

#### Process

For each `.aspx` blob (up to `limit`):
1. Runs the full clean pipeline (writes `.txt` to `gsc-cleaned`)
2. If `embed` is `true`, immediately runs the embed pipeline using the cleaned text from memory

#### Output

```json
{
  "status": "completed",
  "stage": "process",
  "source_container": "gsc",
  "output_container": "gsc-cleaned",
  "prefix": "",
  "limit": 5,
  "embed_requested": true,
  "processed_count": 5,
  "skipped_count": 0,
  "error_count": 0,
  "processed_items": [...],
  "skipped_items": [],
  "errors": []
}
```

---

## Data Transformation Pipeline

This diagram shows how data flows through the entire pipeline from raw ASPX to database rows:

```
Raw ASPX (SharePoint export)
    |
    v
Strip <%@ page directives %>
    |
    v
Extract <mso:CanvasContent1> inner HTML (if present)
    |
    v
BeautifulSoup: remove script/style/nav/header/footer/aside/svg tags
    |
    v
Extract visible text (get_text with newline separator)
    |
    v
Normalize whitespace (strip lines, remove blanks)
    |
    v  SAVED AS .txt in gsc-cleaned container
    |
    v
Parse clause heading (e.g. "1. Definitions")
    |
    v
Extract applicable term set codes (CTM-P-ST-XXX)
    |
    v
Remove boilerplate (TOC, back to homepage, repository notes)
    |
    v
Segment by major headings (Intent, Suggested Responses, etc.)
    |
    v
Chunk each segment (120 words, 20-word overlap)
    |
    v
Generate 1536-dim embedding per chunk via Azure OpenAI
    |
    v
Upsert into PostgreSQL gsc_vector_rag table
    (one row per chunk x term set combination)
```

### Example Transformation

**Input:** `1.-Definitions.aspx` (raw SharePoint export, ~50KB of HTML/ASP.NET markup)

**After cleaning:** `1.-Definitions.txt` (~4,500 characters of plain text)

**After parsing:** Clause number `"1"`, title `"Definitions"`, 2 applicable term sets

**After segmenting:** 3 segments: "Overview", "Intent", "Common Exceptions / Suggested Responses"

**After chunking:** 8 chunks (120 words each, 20-word overlap)

**After embedding:** 8 chunks x 2 term sets = **16 rows** in the database, each with a 1536-dimensional vector

---

## Database Schema

The `gsc_vector_rag` table (name configurable via `CHUNK_TABLE_NAME`) stores all embedded chunks:

| Column | Type | Description |
|---|---|---|
| `id` | `UUID` | Auto-generated primary key (`gen_random_uuid()`) |
| `collection_name` | `TEXT NOT NULL` | Logical collection grouping (default: `gsc-internal-policies`). Allows multiple datasets in one table. |
| `external_id` | `TEXT NOT NULL` | SHA-256 hash for deterministic deduplication. Computed from `{collection}|{source}|{clause}|{termset}|{index}`. |
| `clause_number` | `TEXT NOT NULL` | The clause number as extracted from the heading (e.g., `"1"`, `"12"`) |
| `clause_number_norm` | `TEXT NOT NULL` | Normalized: uppercase, whitespace removed |
| `tc_number` | `TEXT` | Term set number, 3-digit padded (e.g., `"001"`, `"012"`). NULL if no term sets found. |
| `tc_number_norm` | `TEXT` | Normalized term set number. NULL if no term sets found. |
| `topic` | `TEXT` | Auto-detected topic from clause title (lowercase, e.g., `"definitions"`, `"warranty"`) |
| `source_doc` | `TEXT` | Original blob name (e.g., `"1.-Definitions.txt"`) |
| `section_title` | `TEXT` | Full section path (e.g., `"Clause 1 - Definitions \| Intent"`) |
| `chunk_text` | `TEXT NOT NULL` | The actual text chunk (~120 words) |
| `guidance_text` | `TEXT` | First sentence of the chunk (up to 400 characters), useful for quick previews |
| `metadata` | `JSONB NOT NULL` | Rich metadata including: `source_format`, `source_doc`, `clause_title`, `source_status`, `is_placeholder`, `segment_title`, `chunk_index`, `termset_number`, `termset_code_full`, `all_applicable_termsets`, `all_applicable_termset_codes` |
| `embedding` | `VECTOR(1536)` | OpenAI embedding vector for semantic similarity search |
| `created_at` | `TIMESTAMPTZ` | Row creation timestamp (auto-set) |
| `updated_at` | `TIMESTAMPTZ` | Last upsert timestamp (auto-updated on conflict) |

### Indexes

| Index | Type | Columns | Purpose |
|---|---|---|---|
| Unique | `UNIQUE` | `(collection_name, external_id)` | Prevents duplicate chunks; enables safe re-processing |
| Filter | `B-tree` | `(collection_name, clause_number_norm, tc_number_norm)` | Fast lookup by clause number and/or term set |
| Vector | `HNSW` | `embedding` with `vector_cosine_ops` | Fast approximate nearest-neighbor search for RAG queries |

### Source Status Values

The `metadata.source_status` field indicates the quality/origin of the source document:

| Value | Meaning |
|---|---|
| `"authoritative"` | Official, verified content |
| `"synthetic_demo"` | Synthetic demo data (contains "Demo note:" or "synthetic demo clause content") |
| `"template_placeholder"` | Placeholder template (contains "[TODO:" or "Repository note:") |
| `"provided_transcription"` | Best-effort transcription (contains "[unclear]", "[truncated", etc.) |

---

## Azure Portal Test/Run Instructions

When testing functions from the Azure Portal, navigate to:
**Function App** -> **Functions** -> click a function name -> **Code + Test** -> **Test/Run**

A sidebar opens with fields for HTTP method, Key, Query, Headers, and Body. Here are the exact values for each function:

### health_check

| Field | Value |
|---|---|
| HTTP method | `GET` |
| Key | Select `master (Host key)` from dropdown |
| Query | *(leave empty)* |
| Headers | *(leave empty)* |
| Body | *(leave empty)* |

### clean_one_gsc_blob

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"blob": "1.-Definitions.aspx"}` |

### embed_one_cleaned_blob

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"blob": "1.-Definitions.txt"}` |

Note: Use `.txt` extension (not `.aspx`) because this reads from the `gsc-cleaned` container.

### process_one_gsc_blob

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"blob": "1.-Definitions.aspx", "embed": true}` |

### clean_gsc_batch

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"limit": 5, "prefix": ""}` |

### embed_cleaned_batch

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"limit": 5, "prefix": "", "embed": true}` |

### process_gsc_batch

| Field | Value |
|---|---|
| HTTP method | `POST` |
| Key | Select `default (function key)` from dropdown |
| Query | *(leave empty)* |
| Headers | Key: `Content-Type`, Value: `application/json` |
| Body | `{"limit": 5, "prefix": "", "embed": true}` |

### Recommended Test Order

1. **`health_check`** — Verify all env vars, blob storage, and postgres connectivity
2. **`clean_one_gsc_blob`** — Clean one file, then check the `gsc-cleaned` container in Storage Browser for the new `.txt` file
3. **`embed_one_cleaned_blob`** — Embed the cleaned file, verify rows appear in the database
4. **`process_one_gsc_blob`** — Test the combined pipeline end-to-end

---

## Required Environment Variables

| Name | Description | Example |
|---|---|---|
| `cdooaipocdata1_STORAGE` | Azure Blob Storage connection string | `DefaultEndpointsProtocol=https;AccountName=cdooaipocdata1;AccountKey=...;EndpointSuffix=core.usgovcloudapi.net` |
| `PGVECTOR_DATABASE_URL` | PostgreSQL connection string | `postgresql://user:pass@host:5432/dbname` |
| `AZURE_OPENAI_BASE_URL` | Azure OpenAI endpoint URL | `https://your-openai.openai.azure.us` |
| `AZURE_OPENAI_API_KEY` | Azure OpenAI API key | `abc123...` |

## Optional Environment Variables

| Name | Default | Description |
|---|---|---|
| `OUTPUT_CONTAINER` | `gsc-cleaned` | Name of the output blob container for cleaned files |
| `CHUNK_TABLE_NAME` | `gsc_vector_rag` | PostgreSQL table name for storing chunks |
| `COLLECTION_NAME` | `gsc-internal-policies` | Logical collection name for grouping chunks |
| `AZURE_OPENAI_EMBEDDING_DEPLOYMENT` | `text-embedding-ada-002` | Azure OpenAI model deployment name |
| `AZURE_OPENAI_EMBEDDINGS_PATH` | `/embeddings` | Path appended to the base URL for embedding requests |
| `EMBEDDING_DIMENSIONS` | `1536` | Expected embedding vector dimensions |
| `REQUEST_TIMEOUT_SECONDS` | `120` | Timeout for Azure OpenAI API calls |
| `CHUNK_SIZE` | `120` | Number of words per text chunk |
| `CHUNK_OVERLAP` | `20` | Number of overlapping words between consecutive chunks |
