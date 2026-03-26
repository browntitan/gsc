#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib import request

import psycopg
from psycopg import sql
from psycopg.rows import dict_row


CLAUSE_FILE_RE = re.compile(r"^\d{2}_.+\.txt$")
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


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def clean_optional(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def require_config(value: Optional[str], name: str) -> str:
    cleaned = clean_optional(value)
    if not cleaned:
        raise RuntimeError(f"{name} must be set")
    return cleaned


def azure_endpoint_root(value: str) -> str:
    endpoint = require_config(value, "AZURE_OPENAI_ENDPOINT")
    if "/openai/" in endpoint.lower():
        raise RuntimeError(
            "AZURE_OPENAI_ENDPOINT must be the endpoint root, not a pre-expanded /openai/ URL"
        )
    return endpoint.rstrip("/")


def azure_embedding_url(args: argparse.Namespace) -> str:
    deployment = require_config(
        args.azure_openai_embedding_deployment_name,
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME",
    )
    api_version = require_config(args.azure_openai_api_version, "AZURE_OPENAI_API_VERSION")
    return (
        f"{azure_endpoint_root(args.azure_openai_endpoint)}/openai/deployments/{deployment}/embeddings"
        f"?api-version={api_version}"
    )


def normalize_identifier(value: str) -> str:
    value = value.strip().upper()
    value = re.sub(r"\s+", "", value)
    return value


def vector_literal(values: List[float]) -> str:
    return "[" + ",".join(f"{float(v):.8f}" for v in values) + "]"


def validate_table_name(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
        raise ValueError(f"Invalid table name: {value}")
    return value


def get_connection(database_url: str) -> psycopg.Connection:
    return psycopg.connect(database_url, row_factory=dict_row)


def ensure_schema(database_url: str, table_name: str, dimensions: int) -> None:
    table_name = validate_table_name(table_name)
    unique_idx = f"sc_chunks_uq_{hashlib.sha1(table_name.encode()).hexdigest()[:8]}"
    filter_idx = f"sc_chunks_filter_{hashlib.sha1((table_name + '_f').encode()).hexdigest()[:8]}"
    vector_idx = f"sc_chunks_hnsw_{hashlib.sha1((table_name + '_v').encode()).hexdigest()[:8]}"

    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
            cur.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")
            cur.execute(
                sql.SQL(
                    """
                    CREATE TABLE IF NOT EXISTS {table} (
                        id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                        collection_name TEXT NOT NULL,
                        external_id TEXT NOT NULL,
                        clause_number TEXT NOT NULL,
                        clause_number_norm TEXT NOT NULL,
                        tc_number TEXT NOT NULL,
                        tc_number_norm TEXT NOT NULL,
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


def delete_collection(database_url: str, table_name: str, collection_name: str) -> int:
    with get_connection(database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(
                sql.SQL("DELETE FROM {table} WHERE collection_name = %s").format(
                    table=sql.Identifier(validate_table_name(table_name))
                ),
                (collection_name,),
            )
            deleted = cur.rowcount
        conn.commit()
    return deleted


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
                        row["tc_number"],
                        normalize_identifier(row["tc_number"]),
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


def post_json(
    url: str,
    payload: dict,
    headers: Optional[dict] = None,
    timeout: int = 120,
) -> dict:
    req = request.Request(
        url=url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
        method="POST",
    )
    with request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))

def embed_text(text: str, args: argparse.Namespace) -> List[float]:
    response = post_json(
        azure_embedding_url(args),
        {"input": text},
        headers={
            "api-key": require_config(args.azure_openai_api_key, "AZURE_OPENAI_API_KEY"),
        },
        timeout=args.request_timeout_seconds,
    )
    data = response.get("data") or []
    if not data or "embedding" not in data[0]:
        raise RuntimeError("Azure OpenAI returned no embeddings")
    vector = data[0]["embedding"]
    if len(vector) != args.embedding_dimensions:
        raise RuntimeError(
            f"Embedding dimension mismatch: expected {args.embedding_dimensions}, got {len(vector)}"
        )
    return vector


def collect_txt_files(input_path: Path) -> List[Path]:
    if input_path.is_file():
        if input_path.suffix.lower() != ".txt":
            raise ValueError("Only .txt files are supported")
        return [input_path]
    if not input_path.is_dir():
        raise ValueError(f"Input path does not exist: {input_path}")

    all_txt = sorted(path for path in input_path.rglob("*.txt") if path.is_file())
    clause_files = [path for path in all_txt if CLAUSE_FILE_RE.fullmatch(path.name)]
    if clause_files:
        return clause_files
    return all_txt


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
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return ""
    match = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)
    return match[0][:400]


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
    titled_chunks = []
    for chunk in body_chunks:
        titled_chunks.append(f"{title}\n{chunk}".strip())
    return titled_chunks


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
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return body


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
        segments.append(
            {
                "title": title,
                "text": text,
            }
        )
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


def parse_clause_document(path: Path) -> Dict[str, Any]:
    raw = path.read_text(encoding="utf-8")
    text = clean_text(raw)
    lines = text.splitlines()

    heading = _find_clause_heading(lines)
    if not heading:
        raise ValueError("No clause heading like '1. Definitions' was found")
    heading_idx, clause_number, clause_title = heading

    full_codes, applicable_start, applicable_end = _find_applicable_block(lines, heading_idx + 1)
    if not full_codes:
        raise ValueError("No applicable termset codes were found in the Applicable For block")

    body = _clean_clause_body(lines, heading_idx, applicable_start, applicable_end)
    if not body:
        raise ValueError("No substantive clause body remained after cleaning the document")

    source_status = _detect_source_status(text)
    normalized_termsets = [normalize_termset_number(code) for code in full_codes]
    normalized_termsets = [value for value in normalized_termsets if value]

    return {
        "clause_number": clause_number,
        "clause_title": clause_title,
        "full_termset_codes": full_codes,
        "termset_numbers": normalized_termsets,
        "body": body,
        "segments": segment_clause_body(body),
        "source_status": source_status,
    }


def build_rows_for_file(path: Path, args: argparse.Namespace) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    skips: List[Dict[str, Any]] = []
    rows: List[Dict[str, Any]] = []

    try:
        parsed = parse_clause_document(path)
    except Exception as exc:  # noqa: BLE001
        skips.append(
            {
                "file": str(path),
                "reason": str(exc),
            }
        )
        return rows, skips

    segments = parsed.get("segments") or []
    chunk_entries: List[Dict[str, Any]] = []
    for segment in segments:
        segment_title = clean_optional(segment.get("title")) or "Overview"
        segment_text = clean_optional(segment.get("text"))
        if not segment_text:
            continue
        for chunk in chunk_segment_text(segment_title, segment_text, args.chunk_size, args.chunk_overlap):
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
                "file": str(path),
                "clause_number": parsed["clause_number"],
                "reason": "Clause body produced no chunks",
            }
        )
        return rows, skips

    topic = detect_topic(parsed["clause_title"], parsed["body"])

    for termset_number, full_code in zip(parsed["termset_numbers"], parsed["full_termset_codes"]):
        for idx, chunk_entry in enumerate(chunk_entries, start=1):
            external_id = hashlib.sha256(
                f"{args.collection_name}|{path.name}|{parsed['clause_number']}|{termset_number}|{idx}".encode("utf-8")
            ).hexdigest()
            rows.append(
                {
                    "collection_name": args.collection_name,
                    "external_id": external_id,
                    "clause_number": parsed["clause_number"],
                    "tc_number": termset_number,
                    "topic": topic,
                    "source_doc": path.name,
                    "section_title": chunk_entry["section_title"],
                    "chunk_text": chunk_entry["chunk_text"],
                    "guidance_text": chunk_entry["guidance_text"],
                    "metadata": {
                        "source_format": "clause_repository_txt",
                        "source_path": str(path),
                        "clause_title": parsed["clause_title"],
                        "termset_number": termset_number,
                        "termset_code_full": full_code,
                        "all_applicable_termsets": parsed["termset_numbers"],
                        "all_applicable_termset_codes": parsed["full_termset_codes"],
                        "source_status": parsed["source_status"],
                        "is_placeholder": parsed["source_status"] == "template_placeholder",
                        "segment_title": chunk_entry["segment_title"],
                        "chunk_index": idx,
                    },
                }
            )

    return rows, skips


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Seed internal policy clause .txt files into pgvector using Azure OpenAI embeddings."
    )
    parser.add_argument("--input-path", required=True, help="Path to a clause .txt file or directory of clause .txt files.")
    parser.add_argument(
        "--collection-name",
        default=os.getenv("DEFAULT_COLLECTION_NAME", "GSC-Internal-Policy"),
        help="Collection namespace stored in the shared chunk table.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgresql://openwebui:openwebui@localhost:55432/openwebui"),
        help="Postgres connection string for the shared pgvector database.",
    )
    parser.add_argument(
        "--chunk-table-name",
        default=os.getenv("CHUNK_TABLE_NAME", "supply_chain_chunks"),
        help="Chunk table name.",
    )
    parser.add_argument(
        "--embedding-dimensions",
        type=int,
        default=int(os.getenv("EMBEDDING_DIMENSIONS", "1536")),
        help="Embedding dimensions.",
    )
    parser.add_argument(
        "--azure-openai-endpoint",
        default=os.getenv("AZURE_OPENAI_ENDPOINT", ""),
        help="Azure OpenAI endpoint root, for example https://<resource>.openai.azure.us.",
    )
    parser.add_argument(
        "--azure-openai-api-key",
        default=os.getenv("AZURE_OPENAI_API_KEY", ""),
        help="Azure OpenAI API key.",
    )
    parser.add_argument(
        "--azure-openai-embedding-deployment-name",
        default=os.getenv("AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME", ""),
        help="Azure OpenAI embedding deployment name.",
    )
    parser.add_argument(
        "--azure-openai-api-version",
        default=os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01"),
        help="Azure OpenAI API version.",
    )
    parser.add_argument(
        "--request-timeout-seconds",
        type=int,
        default=int(os.getenv("REQUEST_TIMEOUT_SECONDS", "120")),
        help="HTTP timeout for embedding requests.",
    )
    parser.add_argument(
        "--chunk-size",
        type=int,
        default=120,
        help="Chunk size in words.",
    )
    parser.add_argument(
        "--chunk-overlap",
        type=int,
        default=20,
        help="Chunk overlap in words.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=env_bool("DRY_RUN", False),
        help="Parse and report only. Do not embed or write to Postgres.",
    )
    parser.add_argument(
        "--report-file",
        default="",
        help="Optional path for a JSON ingestion report.",
    )
    parser.add_argument(
        "--replace-collection",
        action="store_true",
        help="Delete existing rows for the target collection before inserting new rows.",
    )
    parser.add_argument(
        "--delete-collection",
        action="append",
        default=[],
        help="Delete an additional collection after a successful write. Repeat the flag for multiple collections.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    input_path = Path(args.input_path)
    files = collect_txt_files(input_path)

    all_rows: List[Dict[str, Any]] = []
    all_skips: List[Dict[str, Any]] = []
    for file_path in files:
        rows, skips = build_rows_for_file(file_path, args)
        all_rows.extend(rows)
        all_skips.extend(skips)

    report: Dict[str, Any] = {
        "collection_name": args.collection_name,
        "chunk_table_name": args.chunk_table_name,
        "input_path": str(input_path),
        "files_seen": [str(path) for path in files],
        "files_count": len(files),
        "rows_prepared": len(all_rows),
        "skipped_sections": all_skips,
        "skipped_count": len(all_skips),
        "dry_run": args.dry_run,
        "replace_collection": args.replace_collection,
        "delete_collections": args.delete_collection,
        "rows_written": 0,
        "rows_deleted": 0,
        "additional_rows_deleted": {},
    }

    if args.dry_run or not all_rows:
        print(json.dumps(report, indent=2))
        if args.report_file:
            Path(args.report_file).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        return

    require_config(args.azure_openai_endpoint, "AZURE_OPENAI_ENDPOINT")
    require_config(args.azure_openai_api_key, "AZURE_OPENAI_API_KEY")
    require_config(
        args.azure_openai_embedding_deployment_name,
        "AZURE_OPENAI_EMBEDDING_DEPLOYMENT_NAME",
    )
    require_config(args.azure_openai_api_version, "AZURE_OPENAI_API_VERSION")
    database_url = require_config(args.database_url, "DATABASE_URL")

    ensure_schema(database_url, args.chunk_table_name, args.embedding_dimensions)

    if args.replace_collection:
        report["rows_deleted"] = delete_collection(
            database_url,
            args.chunk_table_name,
            args.collection_name,
        )

    embedded_rows = []
    for row in all_rows:
        embedding = embed_text(row["chunk_text"], args)
        embedded_rows.append({**row, "embedding": embedding})

    report["rows_written"] = upsert_rows(
        database_url,
        args.chunk_table_name,
        embedded_rows,
    )

    for collection_name in args.delete_collection:
        deleted = delete_collection(
            database_url,
            args.chunk_table_name,
            collection_name,
        )
        report["additional_rows_deleted"][collection_name] = deleted

    print(json.dumps(report, indent=2))
    if args.report_file:
        Path(args.report_file).write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
