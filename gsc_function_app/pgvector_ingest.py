from __future__ import annotations

import hashlib
import html
import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import error as urlerror
from urllib import request

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


CLAUSE_HEADING_RE = re.compile(r"^\s*(\d{1,3})\.\s+(.+?)\s*$")
TERMSET_CODE_RE = re.compile(r"^CTM-P-ST-(\d{3})$", re.IGNORECASE)
SUBPARAGRAPH_RE = re.compile(r"^subparagraph\s+([a-z0-9]+)$", re.IGNORECASE)

MAJOR_SECTION_TITLES = {
    "intent": "Intent",
    "common exceptions / suggested responses": "Common Exceptions / Suggested Responses",
    "suggested responses": "Suggested Responses",
    "additional resources": "Additional Resources",
    "[additional resources - partially visible]": "Additional Resources",
    "references": "References",
}


@dataclass
class Settings:
    database_url: str
    chunk_table_name: str
    collection_name: str
    embedding_base_url: str
    embedding_api_key: str
    embedding_deployment: str
    embedding_path: str = "/embeddings"
    embedding_dimensions: int = 1536
    request_timeout_seconds: int = 120
    chunk_size: int = 120
    chunk_overlap: int = 20


def clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def require_env(name: str) -> str:
    value = clean_optional(os.getenv(name))
    if value is None:
        raise RuntimeError(f"Missing required app setting: {name}")
    return value


def parse_int_env(name: str, default: int) -> int:
    value = clean_optional(os.getenv(name))
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"App setting {name} must be an integer, got {value!r}") from exc


def load_settings() -> Settings:
    return Settings(
        database_url=require_env("PGVECTOR_DATABASE_URL"),
        chunk_table_name=os.getenv("CHUNK_TABLE_NAME", "gsc_vector_rag"),
        collection_name=os.getenv("COLLECTION_NAME", "gsc-internal-policies"),
        embedding_base_url=require_env("AZURE_OPENAI_BASE_URL"),
        embedding_api_key=require_env("AZURE_OPENAI_API_KEY"),
        embedding_deployment=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT", "text-embedding-ada-002"),
        embedding_path=os.getenv("AZURE_OPENAI_EMBEDDINGS_PATH", "/embeddings"),
        embedding_dimensions=parse_int_env("EMBEDDING_DIMENSIONS", 1536),
        request_timeout_seconds=parse_int_env("REQUEST_TIMEOUT_SECONDS", 120),
        chunk_size=parse_int_env("CHUNK_SIZE", 120),
        chunk_overlap=parse_int_env("CHUNK_OVERLAP", 20),
    )


def normalize_identifier(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip().upper()
    normalized = re.sub(r"\s+", "", normalized)
    return normalized or None


def vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def validate_table_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_]+", value):
        raise ValueError(
            f"Invalid table name: {value}. Use only letters, numbers, and underscores."
        )
    return value


def get_connection(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def check_database_connection() -> Dict[str, Any]:
    database_url = require_env("PGVECTOR_DATABASE_URL")
    chunk_table_name = os.getenv("CHUNK_TABLE_NAME", "gsc_vector_rag")
    collection_name = os.getenv("COLLECTION_NAME", "gsc-internal-policies")

    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 AS ok")
            row = cur.fetchone()

    return {
        "database": "reachable",
        "chunk_table_name": chunk_table_name,
        "collection_name": collection_name,
        "query_result": row,
    }


def ensure_schema(database_url: str, table_name: str, dimensions: int) -> None:
    table_name = validate_table_name(table_name)
    unique_idx = f"gsc_chunks_uq_{hashlib.sha1(table_name.encode()).hexdigest()[:8]}"
    filter_idx = f"gsc_chunks_filter_{hashlib.sha1((table_name + '_f').encode()).hexdigest()[:8]}"
    vector_idx = f"gsc_chunks_hnsw_{hashlib.sha1((table_name + '_v').encode()).hexdigest()[:8]}"

    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {table} (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        collection_name TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        clause_number TEXT NOT NULL,
                        clause_number_norm TEXT NOT NULL,
                        tc_number TEXT,
                        tc_number_norm TEXT,
                        topic TEXT,
                        source_doc TEXT,
                        section_title TEXT,
                        chunk_text TEXT NOT NULL,
                        guidance_text TEXT,
                        metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                        embedding VECTOR({dimensions}) NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                ).format(
                    table=sql.Identifier(table_name),
                    dimensions=sql.SQL(str(dimensions)),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE UNIQUE INDEX IF NOT EXISTS {index_name} ON {table} (collection_name, external_id)"
                ).format(
                    index_name=sql.Identifier(unique_idx),
                    table=sql.Identifier(table_name),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {index_name} ON {table} (collection_name, clause_number_norm, tc_number_norm)"
                ).format(
                    index_name=sql.Identifier(filter_idx),
                    table=sql.Identifier(table_name),
                )
            )
            cur.execute(
                sql.SQL(
                    "CREATE INDEX IF NOT EXISTS {index_name} ON {table} USING hnsw (embedding vector_cosine_ops)"
                ).format(
                    index_name=sql.Identifier(vector_idx),
                    table=sql.Identifier(table_name),
                )
            )
        conn.commit()


def upsert_rows(database_url: str, table_name: str, rows: Iterable[Dict[str, Any]]) -> int:
    table = sql.Identifier(validate_table_name(table_name))
    query = sql.SQL(
        """
        INSERT INTO {table} (
            collection_name,
            external_id,
            clause_number,
            clause_number_norm,
            tc_number,
            tc_number_norm,
            topic,
            source_doc,
            section_title,
            chunk_text,
            guidance_text,
            metadata,
            embedding
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::vector
        )
        ON CONFLICT (collection_name, external_id) DO UPDATE SET
            clause_number = EXCLUDED.clause_number,
            clause_number_norm = EXCLUDED.clause_number_norm,
            tc_number = EXCLUDED.tc_number,
            tc_number_norm = EXCLUDED.tc_number_norm,
            topic = EXCLUDED.topic,
            source_doc = EXCLUDED.source_doc,
            section_title = EXCLUDED.section_title,
            chunk_text = EXCLUDED.chunk_text,
            guidance_text = EXCLUDED.guidance_text,
            metadata = EXCLUDED.metadata,
            embedding = EXCLUDED.embedding,
            updated_at = NOW()
        """
    ).format(table=table)

    count = 0
    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            for row in rows:
                cur.execute(
                    query,
                    (
                        row["collection_name"],
                        row["external_id"],
                        row["clause_number"],
                        normalize_identifier(row["clause_number"]),
                        row.get("tc_number"),
                        normalize_identifier(row.get("tc_number")),
                        row.get("topic"),
                        row.get("source_doc"),
                        row.get("section_title"),
                        row["chunk_text"],
                        row.get("guidance_text"),
                        json.dumps(row.get("metadata", {})),
                        vector_literal(row["embedding"]),
                    ),
                )
                count += 1
        conn.commit()
    return count


def build_embeddings_url(settings: Settings) -> str:
    base_url = settings.embedding_base_url.rstrip("/")
    path = (settings.embedding_path or "/embeddings").strip()
    if not path.startswith("/"):
        path = "/" + path
    return base_url + path


def post_json(
    url: str,
    payload: Dict[str, Any],
    headers: Optional[Dict[str, str]] = None,
    timeout: int = 120,
) -> Dict[str, Any]:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )

    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw_body = response.read().decode("utf-8", errors="replace")
    except urlerror.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(
            f"Embedding request failed with status {exc.code}: {error_body}"
        ) from exc
    except urlerror.URLError as exc:
        raise RuntimeError(f"Embedding request failed: {exc.reason}") from exc

    try:
        payload_json = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Embedding endpoint returned non-JSON response: {raw_body}") from exc

    if not isinstance(payload_json, dict):
        raise RuntimeError(
            f"Embedding endpoint returned unexpected payload shape: {payload_json!r}"
        )
    return payload_json


def extract_embedding_vector(response_payload: Dict[str, Any], expected_dimensions: int) -> List[float]:
    data = response_payload.get("data")
    if not isinstance(data, list) or not data:
        raise RuntimeError(
            f"Embedding endpoint returned unexpected payload shape: {response_payload!r}"
        )

    first_item = data[0]
    if not isinstance(first_item, dict) or "embedding" not in first_item:
        raise RuntimeError(
            f"Embedding endpoint returned unexpected payload shape: {response_payload!r}"
        )

    embedding = first_item["embedding"]
    if not isinstance(embedding, list):
        raise RuntimeError(
            f"Embedding endpoint returned unexpected payload shape: {response_payload!r}"
        )

    try:
        vector = [float(value) for value in embedding]
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Embedding endpoint returned unexpected payload shape: {response_payload!r}"
        ) from exc

    if len(vector) != expected_dimensions:
        raise RuntimeError(
            f"Embedding dimension mismatch: expected {expected_dimensions}, got {len(vector)}"
        )
    return vector


def embed_text(text: str, settings: Settings) -> List[float]:
    response_payload = post_json(
        build_embeddings_url(settings),
        payload={
            "model": settings.embedding_deployment,
            "input": text,
        },
        headers={"api-key": settings.embedding_api_key},
        timeout=settings.request_timeout_seconds,
    )
    return extract_embedding_vector(response_payload, settings.embedding_dimensions)


def clean_text(raw: str) -> str:
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"(?i)</div\s*>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_termset_number(value: str) -> Optional[str]:
    cleaned = clean_optional(value)
    if not cleaned:
        return None
    match = re.search(r"CTM-P-ST-(\d{1,3})", cleaned, flags=re.IGNORECASE)
    if match:
        return match.group(1).zfill(3)
    match = re.search(r"(\d{1,3})", cleaned)
    if match:
        return match.group(1).zfill(3)
    return None


def first_sentence(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return ""
    return re.split(r"(?<=[.!?])\s+", normalized, maxsplit=1)[0][:400]


def detect_topic(clause_title: Optional[str], body: str) -> Optional[str]:
    if clause_title:
        return clause_title.strip().lower()
    sentence = first_sentence(body)
    if not sentence:
        return None
    words = re.findall(r"[A-Za-z0-9']+", sentence.lower())
    if not words:
        return None
    return " ".join(words[:6])


def chunk_text(text: str, chunk_size: int, chunk_overlap: int) -> List[str]:
    words = text.split()
    if not words:
        return []
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")
    if chunk_overlap >= chunk_size:
        raise ValueError("chunk_overlap must be smaller than chunk_size")

    chunks = []
    step = chunk_size - chunk_overlap
    for start in range(0, len(words), step):
        chunk_words = words[start : start + chunk_size]
        if not chunk_words:
            continue
        chunk = " ".join(chunk_words).strip()
        if chunk:
            chunks.append(chunk)
        if start + chunk_size >= len(words):
            break
    return chunks


def chunk_segment_text(
    title: Optional[str],
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> List[str]:
    body_chunks = chunk_text(text, chunk_size, chunk_overlap)
    if not title:
        return body_chunks
    return [f"{title}\n{chunk}".strip() for chunk in body_chunks]


def _normalized_line(line: str) -> str:
    return re.sub(r"\s+", " ", line.strip().lower().rstrip(":"))


def _display_subparagraph(normalized: str) -> Optional[str]:
    match = SUBPARAGRAPH_RE.fullmatch(normalized)
    if not match:
        return None
    return f"Subparagraph {match.group(1).upper()}"


def _display_major_heading(normalized: str) -> Optional[str]:
    return MAJOR_SECTION_TITLES.get(normalized)


def _find_clause_heading(lines: List[str]) -> Optional[Tuple[int, str, str]]:
    for idx, line in enumerate(lines):
        match = CLAUSE_HEADING_RE.match(line.strip())
        if match:
            return idx, str(int(match.group(1))), match.group(2).strip()
    return None


def _find_applicable_block(lines: List[str], start_index: int) -> Tuple[List[str], int, int]:
    block_start = -1
    scan_start = start_index
    for idx in range(start_index, len(lines)):
        normalized = _normalized_line(lines[idx])
        if normalized == "applicable for":
            block_start = idx
            scan_start = idx + 1
            break
        if normalized == "applicable" and idx + 1 < len(lines) and _normalized_line(lines[idx + 1]) == "for":
            block_start = idx
            scan_start = idx + 2
            break

    if block_start < 0:
        return [], -1, -1

    full_codes: List[str] = []
    block_end = scan_start
    for idx in range(scan_start, len(lines)):
        stripped = lines[idx].strip()
        if not stripped:
            if full_codes:
                block_end = idx
                break
            continue

        match = TERMSET_CODE_RE.fullmatch(stripped)
        if match:
            code = f"CTM-P-ST-{match.group(1).zfill(3)}"
            full_codes.append(code)
            block_end = idx + 1
            continue

        if full_codes:
            block_end = idx
            break

    return full_codes, block_start, block_end


def _detect_source_status(text: str) -> str:
    lowered = text.lower()
    if "demo note:" in lowered or "synthetic demo clause content" in lowered:
        return "synthetic_demo"
    if "[todo:" in lowered or "repository note:" in lowered:
        return "template_placeholder"
    if "best-effort combined transcription" in lowered or "[unclear]" in lowered or "[truncated" in lowered:
        return "provided_transcription"
    return "authoritative"


def _clean_clause_body(lines: List[str], heading_idx: int, applicable_start: int, applicable_end: int) -> str:
    body_lines: List[str] = []
    idx = heading_idx + 1
    while idx < len(lines):
        normalized = _normalized_line(lines[idx])

        if normalized.startswith("repository note"):
            idx += 1
            continue

        if normalized == "table of contents":
            idx += 1
            while idx < len(lines):
                if applicable_start >= 0 and idx >= applicable_start:
                    break
                idx += 1
            continue

        if applicable_start >= 0 and applicable_start <= idx < applicable_end:
            idx += 1
            continue

        if normalized == "back to homepage":
            idx += 1
            continue

        line = lines[idx].rstrip()
        if not line.strip():
            if body_lines and body_lines[-1] != "":
                body_lines.append("")
            idx += 1
            continue

        body_lines.append(line.strip())
        idx += 1

    body = "\n".join(body_lines)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def segment_clause_body(body: str) -> List[Dict[str, str]]:
    lines = body.splitlines()
    segments: List[Dict[str, str]] = []
    current_major: Optional[str] = None
    current_sub: Optional[str] = None
    current_lines: List[str] = []

    def flush() -> None:
        nonlocal current_lines

        text = "\n".join(current_lines).strip()
        if not text:
            current_lines = []
            return

        title_parts = []
        if current_major:
            title_parts.append(current_major)
        if current_sub:
            title_parts.append(current_sub)
        title = " | ".join(title_parts) if title_parts else "Overview"
        segments.append({"title": title, "text": text})
        current_lines = []

    for raw_line in lines:
        line = raw_line.strip()
        normalized = _normalized_line(line)

        if not line:
            if current_lines and current_lines[-1] != "":
                current_lines.append("")
            continue

        major_heading = _display_major_heading(normalized)
        if major_heading:
            flush()
            current_major = major_heading
            current_sub = None
            continue

        sub_heading = _display_subparagraph(normalized)
        if sub_heading:
            flush()
            current_sub = sub_heading
            continue

        current_lines.append(line)

    flush()
    return segments


def parse_clause_document_text(raw: str, source_name: str) -> Dict[str, Any]:
    text = clean_text(raw)
    lines = text.splitlines()

    heading = _find_clause_heading(lines)
    if not heading:
        raise ValueError(f"No clause heading like '12. Warranty' was found in {source_name}")

    heading_idx, clause_number, clause_title = heading
    full_codes, applicable_start, applicable_end = _find_applicable_block(lines, heading_idx + 1)
    normalized_termsets = [normalize_termset_number(code) for code in full_codes]
    normalized_termsets = [value for value in normalized_termsets if value]

    body = _clean_clause_body(lines, heading_idx, applicable_start, applicable_end)
    if not body:
        raise ValueError(f"No substantive clause body remained after cleaning {source_name}")

    return {
        "clause_number": clause_number,
        "clause_title": clause_title,
        "full_termset_codes": full_codes,
        "termset_numbers": normalized_termsets,
        "body": body,
        "segments": segment_clause_body(body),
        "source_status": _detect_source_status(text),
    }


def build_rows_for_text(
    source_name: str,
    raw_text: str,
    settings: Settings,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    skips: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    try:
        parsed = parse_clause_document_text(raw_text, source_name)
    except Exception as exc:  # noqa: BLE001
        skips.append({"file": source_name, "reason": str(exc)})
        return rows, skips

    chunk_entries: List[Dict[str, Any]] = []
    for segment in parsed.get("segments") or []:
        segment_title = clean_optional(segment.get("title")) or "Overview"
        segment_text = clean_optional(segment.get("text"))
        if not segment_text:
            continue

        for chunk in chunk_segment_text(segment_title, segment_text, settings.chunk_size, settings.chunk_overlap):
            chunk_entries.append(
                {
                    "section_title": f"Clause {parsed['clause_number']} - {parsed['clause_title']} | {segment_title}",
                    "segment_title": segment_title,
                    "chunk_text": chunk,
                    "guidance_text": first_sentence(chunk),
                }
            )

    if not chunk_entries:
        skips.append(
            {
                "file": source_name,
                "clause_number": parsed["clause_number"],
                "reason": "Clause body produced no chunks",
            }
        )
        return rows, skips

    topic = detect_topic(parsed["clause_title"], parsed["body"])

    termset_pairs: List[Tuple[Optional[str], Optional[str]]]
    if parsed["termset_numbers"]:
        termset_pairs = list(zip(parsed["termset_numbers"], parsed["full_termset_codes"]))
    else:
        termset_pairs = [(None, None)]

    for termset_number, full_code in termset_pairs:
        for idx, chunk_entry in enumerate(chunk_entries, start=1):
            external_id = hashlib.sha256(
                (
                    f"{settings.collection_name}|{source_name}|{parsed['clause_number']}|"
                    f"{termset_number or 'none'}|{idx}"
                ).encode("utf-8")
            ).hexdigest()

            metadata: Dict[str, Any] = {
                "source_format": "clause_repository_txt",
                "source_doc": source_name,
                "clause_title": parsed["clause_title"],
                "source_status": parsed["source_status"],
                "is_placeholder": parsed["source_status"] == "template_placeholder",
                "segment_title": chunk_entry["segment_title"],
                "chunk_index": idx,
            }

            if termset_number:
                metadata["termset_number"] = termset_number
            if full_code:
                metadata["termset_code_full"] = full_code
            if parsed["termset_numbers"]:
                metadata["all_applicable_termsets"] = parsed["termset_numbers"]
            if parsed["full_termset_codes"]:
                metadata["all_applicable_termset_codes"] = parsed["full_termset_codes"]

            rows.append(
                {
                    "collection_name": settings.collection_name,
                    "external_id": external_id,
                    "clause_number": parsed["clause_number"],
                    "tc_number": termset_number,
                    "topic": topic,
                    "source_doc": source_name,
                    "section_title": chunk_entry["section_title"],
                    "chunk_text": chunk_entry["chunk_text"],
                    "guidance_text": chunk_entry["guidance_text"],
                    "metadata": metadata,
                }
            )

    return rows, skips


def ingest_cleaned_text(source_name: str, raw_text: str) -> Dict[str, Any]:
    settings = load_settings()
    rows, skips = build_rows_for_text(source_name, raw_text, settings)

    report: Dict[str, Any] = {
        "source_name": source_name,
        "collection_name": settings.collection_name,
        "chunk_table_name": settings.chunk_table_name,
        "rows_prepared": len(rows),
        "rows_written": 0,
        "skipped": skips,
        "skipped_count": len(skips),
    }

    if not rows:
        return report

    ensure_schema(settings.database_url, settings.chunk_table_name, settings.embedding_dimensions)

    embedded_rows = []
    for row in rows:
        embedding = embed_text(row["chunk_text"], settings)
        embedded_rows.append({**row, "embedding": embedding})

    report["rows_written"] = upsert_rows(
        settings.database_url,
        settings.chunk_table_name,
        embedded_rows,
    )
    return report
