import json
import logging
import os
import re
from typing import Any, Dict, Optional, Tuple

import azure.functions as func
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient


app = func.FunctionApp()

STORAGE_SETTING = "cdooaipocdata1_STORAGE"
SOURCE_CONTAINER = "gsc"
CLEANED_CONTAINER = os.getenv("OUTPUT_CONTAINER", "gsc-cleaned")

REQUIRED_ENV_VARS = (
    STORAGE_SETTING,
    "PGVECTOR_DATABASE_URL",
    "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_API_KEY",
)


class BlobSourceNotFoundError(FileNotFoundError):
    """Raised when the source blob does not exist in the source container."""


def json_response(payload: Dict[str, Any], status_code: int = 200) -> func.HttpResponse:
    return func.HttpResponse(
        json.dumps(payload, indent=2, ensure_ascii=False, default=str),
        status_code=status_code,
        mimetype="application/json",
    )


def parse_request_json(req: func.HttpRequest) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    body_bytes = req.get_body() or b""
    if not body_bytes.strip():
        return {}, None

    try:
        body = req.get_json()
    except ValueError:
        return None, "Request body must be valid JSON."

    if body is None:
        return {}, None
    if not isinstance(body, dict):
        return None, "Request body must be a JSON object."
    return body, None


def get_request_value(
    req: func.HttpRequest,
    body: Dict[str, Any],
    name: str,
    default: Any = None,
) -> Any:
    if name in req.params:
        return req.params.get(name)
    return body.get(name, default)


def parse_boolean(value: Any, name: str, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    raise ValueError(f"Invalid boolean value for '{name}': {value!r}")


def parse_limit(value: Any, default: int = 5) -> int:
    if value in (None, ""):
        return default
    if isinstance(value, bool):
        raise ValueError("The 'limit' value must be an integer.")
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value for 'limit': {value!r}") from exc
    if limit <= 0:
        raise ValueError("The 'limit' value must be greater than zero.")
    return limit


def require_non_empty_string(value: Any, name: str) -> str:
    if value is None:
        raise ValueError(f"Missing required parameter: '{name}'.")
    if not isinstance(value, str):
        raise ValueError(f"The '{name}' value must be a string.")
    cleaned = value.strip()
    if not cleaned:
        raise ValueError(f"The '{name}' value must not be empty.")
    return cleaned


def cleaned_blob_name(source_blob_name: str) -> str:
    if not source_blob_name.lower().endswith(".aspx"):
        raise ValueError("Blob name must end with '.aspx'.")
    return re.sub(r"\.aspx$", ".txt", source_blob_name, flags=re.IGNORECASE)


def get_blob_service() -> BlobServiceClient:
    connection_string = os.getenv(STORAGE_SETTING)
    if not connection_string:
        raise RuntimeError(f"Missing required app setting: {STORAGE_SETTING}")
    return BlobServiceClient.from_connection_string(connection_string)


def load_cleaner() -> Any:
    from aspx_cleaner import extract_visible_text

    return extract_visible_text


def load_ingest_function() -> Any:
    from pgvector_ingest import ingest_cleaned_text

    return ingest_cleaned_text


def load_database_health_check() -> Any:
    from pgvector_ingest import check_database_connection

    return check_database_connection


def process_single_blob(
    service: BlobServiceClient,
    blob_name: str,
    embed_requested: bool,
) -> Dict[str, Any]:
    output_name = cleaned_blob_name(blob_name)

    logging.info(
        "Starting GSC blob processing: source_container=%s blob=%s embed=%s",
        SOURCE_CONTAINER,
        blob_name,
        embed_requested,
    )

    try:
        blob_client = service.get_blob_client(container=SOURCE_CONTAINER, blob=blob_name)
        raw_bytes = blob_client.download_blob().readall()
    except ResourceNotFoundError as exc:
        raise BlobSourceNotFoundError(
            f"Blob not found in container '{SOURCE_CONTAINER}': {blob_name}"
        ) from exc

    raw_text = raw_bytes.decode("utf-8", errors="ignore")
    cleaned_text = load_cleaner()(raw_text)

    output_client = service.get_blob_client(container=CLEANED_CONTAINER, blob=output_name)
    output_client.upload_blob(cleaned_text.encode("utf-8"), overwrite=True)
    logging.info(
        "Wrote cleaned output: output_container=%s blob=%s chars=%s",
        CLEANED_CONTAINER,
        output_name,
        len(cleaned_text),
    )

    embedding_report = None
    embed_executed = False

    if embed_requested:
        embed_executed = True
        logging.info("Starting embedding for cleaned output: blob=%s", output_name)
        embedding_report = load_ingest_function()(source_name=output_name, raw_text=cleaned_text)
        logging.info(
            "Completed embedding for cleaned output: blob=%s rows_written=%s skipped_count=%s",
            output_name,
            embedding_report.get("rows_written"),
            embedding_report.get("skipped_count"),
        )
        if embedding_report.get("skipped_count"):
            logging.warning("Embedding skip report for %s: %s", output_name, json.dumps(embedding_report))

    return {
        "status": "processed",
        "source_container": SOURCE_CONTAINER,
        "source_blob_name": blob_name,
        "output_container": CLEANED_CONTAINER,
        "cleaned_output_blob_name": output_name,
        "cleaned_characters": len(cleaned_text),
        "embed_requested": embed_requested,
        "embed_executed": embed_executed,
        "embedding_report": embedding_report,
        "skip_info": embedding_report.get("skipped", []) if embedding_report else [],
    }


def error_payload(
    *,
    message: str,
    error_type: str,
    source_blob_name: Optional[str] = None,
    cleaned_output_blob_name: Optional[str] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "status": "error",
        "error": {
            "type": error_type,
            "message": message,
        },
    }
    if source_blob_name is not None:
        payload["source_blob_name"] = source_blob_name
    if cleaned_output_blob_name is not None:
        payload["cleaned_output_blob_name"] = cleaned_output_blob_name
    return payload


@app.function_name(name="process_one_gsc_blob")
@app.route(route="admin/process-one", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def process_one_gsc_blob(req: func.HttpRequest) -> func.HttpResponse:
    body, body_error = parse_request_json(req)
    if body_error:
        return json_response(
            error_payload(message=body_error, error_type="InvalidJson"),
            status_code=400,
        )

    assert body is not None

    try:
        blob_name = require_non_empty_string(get_request_value(req, body, "blob"), "blob")
        embed_requested = parse_boolean(get_request_value(req, body, "embed"), "embed", default=True)
    except ValueError as exc:
        return json_response(
            error_payload(message=str(exc), error_type="InvalidRequest"),
            status_code=400,
        )

    if not blob_name.lower().endswith(".aspx"):
        return json_response(
            error_payload(
                message="The 'blob' value must point to a '.aspx' blob in the 'gsc' container.",
                error_type="InvalidBlobName",
                source_blob_name=blob_name,
            ),
            status_code=400,
        )

    try:
        service = get_blob_service()
        result = process_single_blob(service, blob_name, embed_requested)
    except BlobSourceNotFoundError as exc:
        return json_response(
            error_payload(
                message=str(exc),
                error_type="BlobNotFound",
                source_blob_name=blob_name,
                cleaned_output_blob_name=cleaned_blob_name(blob_name),
            ),
            status_code=404,
        )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to process blob %s", blob_name)
        return json_response(
            error_payload(
                message=str(exc),
                error_type=type(exc).__name__,
                source_blob_name=blob_name,
                cleaned_output_blob_name=cleaned_blob_name(blob_name),
            ),
            status_code=500,
        )

    return json_response(result, status_code=200)


@app.function_name(name="process_gsc_batch")
@app.route(route="admin/process-batch", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def process_gsc_batch(req: func.HttpRequest) -> func.HttpResponse:
    body, body_error = parse_request_json(req)
    if body_error:
        return json_response(
            error_payload(message=body_error, error_type="InvalidJson"),
            status_code=400,
        )

    assert body is not None

    try:
        limit = parse_limit(get_request_value(req, body, "limit"), default=5)
        prefix = get_request_value(req, body, "prefix", "") or ""
        if not isinstance(prefix, str):
            raise ValueError("The 'prefix' value must be a string.")
        embed_requested = parse_boolean(get_request_value(req, body, "embed"), "embed", default=True)
    except ValueError as exc:
        return json_response(
            error_payload(message=str(exc), error_type="InvalidRequest"),
            status_code=400,
        )

    processed_items = []
    skipped_items = []
    errors = []

    try:
        service = get_blob_service()
        source_container = service.get_container_client(SOURCE_CONTAINER)

        for blob in source_container.list_blobs(name_starts_with=prefix):
            blob_name = blob.name

            if not blob_name.lower().endswith(".aspx"):
                skipped_items.append(
                    {
                        "blob": blob_name,
                        "reason": "Not an .aspx blob",
                    }
                )
                continue

            if len(processed_items) >= limit:
                break

            try:
                processed_items.append(process_single_blob(service, blob_name, embed_requested))
            except BlobSourceNotFoundError as exc:
                logging.exception("Source blob disappeared during batch processing: %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "cleaned_output_blob_name": cleaned_blob_name(blob_name),
                        "error_type": "BlobNotFound",
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to process batch blob %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "cleaned_output_blob_name": cleaned_blob_name(blob_name),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to initialize batch processing")
        return json_response(
            error_payload(message=str(exc), error_type=type(exc).__name__),
            status_code=500,
        )

    return json_response(
        {
            "status": "completed",
            "source_container": SOURCE_CONTAINER,
            "output_container": CLEANED_CONTAINER,
            "prefix": prefix,
            "limit": limit,
            "embed_requested": embed_requested,
            "processed_count": len(processed_items),
            "skipped_count": len(skipped_items),
            "error_count": len(errors),
            "processed_items": processed_items,
            "skipped_items": skipped_items,
            "errors": errors,
        },
        status_code=200,
    )


@app.function_name(name="health_check")
@app.route(route="admin/health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    del req

    env_report = {
        "required": {
            name: {"present": bool(os.getenv(name))}
            for name in REQUIRED_ENV_VARS
        },
        "defaults": {
            "OUTPUT_CONTAINER": CLEANED_CONTAINER,
            "CHUNK_TABLE_NAME": os.getenv("CHUNK_TABLE_NAME", "gsc_vector_rag"),
            "COLLECTION_NAME": os.getenv("COLLECTION_NAME", "gsc-internal-policies"),
            "AZURE_OPENAI_EMBEDDING_DEPLOYMENT": os.getenv(
                "AZURE_OPENAI_EMBEDDING_DEPLOYMENT",
                "text-embedding-ada-002",
            ),
            "AZURE_OPENAI_EMBEDDINGS_PATH": os.getenv("AZURE_OPENAI_EMBEDDINGS_PATH", "/embeddings"),
            "EMBEDDING_DIMENSIONS": os.getenv("EMBEDDING_DIMENSIONS", "1536"),
            "REQUEST_TIMEOUT_SECONDS": os.getenv("REQUEST_TIMEOUT_SECONDS", "120"),
            "CHUNK_SIZE": os.getenv("CHUNK_SIZE", "120"),
            "CHUNK_OVERLAP": os.getenv("CHUNK_OVERLAP", "20"),
        },
    }
    env_report["missing_required"] = [
        name for name, details in env_report["required"].items() if not details["present"]
    ]

    blob_storage_check: Dict[str, Any]
    try:
        service = get_blob_service()
        service.get_container_client(SOURCE_CONTAINER).get_container_properties()
        service.get_container_client(CLEANED_CONTAINER).get_container_properties()
        blob_storage_check = {
            "status": "ok",
            "source_container": SOURCE_CONTAINER,
            "output_container": CLEANED_CONTAINER,
        }
    except Exception as exc:  # noqa: BLE001
        logging.exception("Blob storage health check failed")
        blob_storage_check = {
            "status": "error",
            "message": str(exc),
            "source_container": SOURCE_CONTAINER,
            "output_container": CLEANED_CONTAINER,
        }

    postgres_check: Dict[str, Any]
    try:
        postgres_check = {
            "status": "ok",
            **load_database_health_check()(),
        }
    except Exception as exc:  # noqa: BLE001
        logging.exception("PostgreSQL health check failed")
        postgres_check = {
            "status": "error",
            "message": str(exc),
            "chunk_table_name": os.getenv("CHUNK_TABLE_NAME", "gsc_vector_rag"),
            "collection_name": os.getenv("COLLECTION_NAME", "gsc-internal-policies"),
        }

    overall_healthy = (
        not env_report["missing_required"]
        and blob_storage_check["status"] == "ok"
        and postgres_check["status"] == "ok"
    )

    return json_response(
        {
            "status": "healthy" if overall_healthy else "unhealthy",
            "checks": {
                "environment": env_report,
                "blob_storage": blob_storage_check,
                "postgres": postgres_check,
            },
        },
        status_code=200 if overall_healthy else 503,
    )
