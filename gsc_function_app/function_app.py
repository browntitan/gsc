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

REQUIRED_ENV_VARS = {
    STORAGE_SETTING: (STORAGE_SETTING,),
    "PGVECTOR_DATABASE_URL": ("PGVECTOR_DATABASE_URL", "PG_CONNECTION_STRING"),
    "AZURE_OPENAI_BASE_URL": ("AZURE_OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT"),
    "AZURE_OPENAI_API_KEY": ("AZURE_OPENAI_API_KEY",),
}


class BlobSourceNotFoundError(FileNotFoundError):
    """Raised when a required blob does not exist."""


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


def require_blob_suffix(blob_name: str, suffix: str, container_name: str) -> str:
    if not blob_name.lower().endswith(suffix.lower()):
        raise ValueError(
            f"The 'blob' value must point to a '{suffix}' blob in the '{container_name}' container."
        )
    return blob_name


def cleaned_blob_name(source_blob_name: str) -> str:
    require_blob_suffix(source_blob_name, ".aspx", SOURCE_CONTAINER)
    return re.sub(r"\.aspx$", ".txt", source_blob_name, flags=re.IGNORECASE)


def get_blob_service() -> BlobServiceClient:
    connection_string = os.getenv(STORAGE_SETTING)
    if not connection_string:
        raise RuntimeError(f"Missing required app setting: {STORAGE_SETTING}")
    return BlobServiceClient.from_connection_string(connection_string)


def first_present_env(*names: str) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value and value.strip():
            return name
    return None


def load_cleaner() -> Any:
    from aspx_cleaner import extract_visible_text

    return extract_visible_text


def load_ingest_function() -> Any:
    from pgvector_ingest import ingest_cleaned_text

    return ingest_cleaned_text


def load_database_health_check() -> Any:
    from pgvector_ingest import check_database_connection

    return check_database_connection


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


def parse_blob_request(
    req: func.HttpRequest,
    *,
    expected_suffix: str,
    container_name: str,
) -> Tuple[Optional[str], Optional[func.HttpResponse]]:
    body, body_error = parse_request_json(req)
    if body_error:
        return None, json_response(
            error_payload(message=body_error, error_type="InvalidJson"),
            status_code=400,
        )

    assert body is not None

    try:
        blob_name = require_non_empty_string(get_request_value(req, body, "blob"), "blob")
        require_blob_suffix(blob_name, expected_suffix, container_name)
    except ValueError as exc:
        return None, json_response(
            error_payload(message=str(exc), error_type="InvalidRequest"),
            status_code=400,
        )

    return blob_name, None


def parse_batch_request(
    req: func.HttpRequest,
    *,
    default_limit: int = 5,
) -> Tuple[Optional[Dict[str, Any]], Optional[func.HttpResponse]]:
    body, body_error = parse_request_json(req)
    if body_error:
        return None, json_response(
            error_payload(message=body_error, error_type="InvalidJson"),
            status_code=400,
        )

    assert body is not None

    try:
        limit = parse_limit(get_request_value(req, body, "limit"), default=default_limit)
        prefix = get_request_value(req, body, "prefix", "") or ""
        if not isinstance(prefix, str):
            raise ValueError("The 'prefix' value must be a string.")
        embed_requested = parse_boolean(get_request_value(req, body, "embed"), "embed", default=True)
    except ValueError as exc:
        return None, json_response(
            error_payload(message=str(exc), error_type="InvalidRequest"),
            status_code=400,
        )

    return {
        "limit": limit,
        "prefix": prefix,
        "embed_requested": embed_requested,
    }, None


def download_blob_text(service: BlobServiceClient, container_name: str, blob_name: str) -> str:
    try:
        blob_client = service.get_blob_client(container=container_name, blob=blob_name)
        raw_bytes = blob_client.download_blob().readall()
    except ResourceNotFoundError as exc:
        raise BlobSourceNotFoundError(
            f"Blob not found in container '{container_name}': {blob_name}"
        ) from exc

    return raw_bytes.decode("utf-8", errors="ignore")


def upload_blob_text(service: BlobServiceClient, container_name: str, blob_name: str, text: str) -> None:
    blob_client = service.get_blob_client(container=container_name, blob=blob_name)
    blob_client.upload_blob(text.encode("utf-8"), overwrite=True)


def clean_source_blob(
    service: BlobServiceClient,
    blob_name: str,
) -> Dict[str, Any]:
    output_name = cleaned_blob_name(blob_name)

    logging.info(
        "Starting source clean: source_container=%s blob=%s",
        SOURCE_CONTAINER,
        blob_name,
    )

    raw_text = download_blob_text(service, SOURCE_CONTAINER, blob_name)
    cleaned_text = load_cleaner()(raw_text)
    upload_blob_text(service, CLEANED_CONTAINER, output_name, cleaned_text)

    logging.info(
        "Wrote cleaned output: output_container=%s blob=%s chars=%s",
        CLEANED_CONTAINER,
        output_name,
        len(cleaned_text),
    )

    return {
        "status": "cleaned",
        "source_container": SOURCE_CONTAINER,
        "source_blob_name": blob_name,
        "output_container": CLEANED_CONTAINER,
        "cleaned_output_blob_name": output_name,
        "cleaned_characters": len(cleaned_text),
        "cleaned_text": cleaned_text,
    }


def embed_cleaned_blob(
    service: BlobServiceClient,
    blob_name: str,
) -> Dict[str, Any]:
    require_blob_suffix(blob_name, ".txt", CLEANED_CONTAINER)

    logging.info(
        "Starting cleaned-text embed: source_container=%s blob=%s",
        CLEANED_CONTAINER,
        blob_name,
    )

    cleaned_text = download_blob_text(service, CLEANED_CONTAINER, blob_name)
    embedding_report = load_ingest_function()(source_name=blob_name, raw_text=cleaned_text)

    logging.info(
        "Completed cleaned-text embed: blob=%s rows_written=%s skipped_count=%s",
        blob_name,
        embedding_report.get("rows_written"),
        embedding_report.get("skipped_count"),
    )
    if embedding_report.get("skipped_count"):
        logging.warning("Embedding skip report for %s: %s", blob_name, json.dumps(embedding_report))

    return {
        "status": "embedded",
        "source_container": CLEANED_CONTAINER,
        "source_blob_name": blob_name,
        "embed_executed": True,
        "embedding_report": embedding_report,
        "skip_info": embedding_report.get("skipped", []),
    }


def process_source_blob(
    service: BlobServiceClient,
    blob_name: str,
    embed_requested: bool,
) -> Dict[str, Any]:
    clean_result = clean_source_blob(service, blob_name)
    cleaned_text = clean_result.pop("cleaned_text")

    embedding_report = None
    embed_executed = False
    if embed_requested:
        embed_executed = True
        logging.info("Starting embedding for cleaned output: blob=%s", clean_result["cleaned_output_blob_name"])
        embedding_report = load_ingest_function()(
            source_name=clean_result["cleaned_output_blob_name"],
            raw_text=cleaned_text,
        )
        logging.info(
            "Completed embedding for cleaned output: blob=%s rows_written=%s skipped_count=%s",
            clean_result["cleaned_output_blob_name"],
            embedding_report.get("rows_written"),
            embedding_report.get("skipped_count"),
        )
        if embedding_report.get("skipped_count"):
            logging.warning(
                "Embedding skip report for %s: %s",
                clean_result["cleaned_output_blob_name"],
                json.dumps(embedding_report),
            )

    return {
        **clean_result,
        "status": "processed",
        "embed_requested": embed_requested,
        "embed_executed": embed_executed,
        "embedding_report": embedding_report,
        "skip_info": embedding_report.get("skipped", []) if embedding_report else [],
    }


def build_not_found_response(blob_name: str, container_name: str) -> func.HttpResponse:
    cleaned_output = None
    if container_name == SOURCE_CONTAINER and blob_name.lower().endswith(".aspx"):
        cleaned_output = cleaned_blob_name(blob_name)

    return json_response(
        error_payload(
            message=f"Blob not found in container '{container_name}': {blob_name}",
            error_type="BlobNotFound",
            source_blob_name=blob_name,
            cleaned_output_blob_name=cleaned_output,
        ),
        status_code=404,
    )


def build_failure_response(
    exc: Exception,
    *,
    source_blob_name: Optional[str] = None,
    cleaned_output_blob_name: Optional[str] = None,
) -> func.HttpResponse:
    return json_response(
        error_payload(
            message=str(exc),
            error_type=type(exc).__name__,
            source_blob_name=source_blob_name,
            cleaned_output_blob_name=cleaned_output_blob_name,
        ),
        status_code=500,
    )


@app.function_name(name="clean_one_gsc_blob")
@app.route(route="gsc/clean-one", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def clean_one_gsc_blob(req: func.HttpRequest) -> func.HttpResponse:
    blob_name, error_response = parse_blob_request(
        req,
        expected_suffix=".aspx",
        container_name=SOURCE_CONTAINER,
    )
    if error_response:
        return error_response

    assert blob_name is not None

    try:
        result = clean_source_blob(get_blob_service(), blob_name)
    except BlobSourceNotFoundError:
        return build_not_found_response(blob_name, SOURCE_CONTAINER)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to clean source blob %s", blob_name)
        return build_failure_response(
            exc,
            source_blob_name=blob_name,
            cleaned_output_blob_name=cleaned_blob_name(blob_name),
        )

    result.pop("cleaned_text", None)
    return json_response(result, status_code=200)


@app.function_name(name="embed_one_cleaned_blob")
@app.route(route="gsc/embed-one", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def embed_one_cleaned_blob(req: func.HttpRequest) -> func.HttpResponse:
    blob_name, error_response = parse_blob_request(
        req,
        expected_suffix=".txt",
        container_name=CLEANED_CONTAINER,
    )
    if error_response:
        return error_response

    assert blob_name is not None

    try:
        result = embed_cleaned_blob(get_blob_service(), blob_name)
    except BlobSourceNotFoundError:
        return build_not_found_response(blob_name, CLEANED_CONTAINER)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to embed cleaned blob %s", blob_name)
        return build_failure_response(exc, source_blob_name=blob_name)

    return json_response(result, status_code=200)


@app.function_name(name="process_one_gsc_blob")
@app.route(route="gsc/process-one", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
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
        require_blob_suffix(blob_name, ".aspx", SOURCE_CONTAINER)
        embed_requested = parse_boolean(get_request_value(req, body, "embed"), "embed", default=True)
    except ValueError as exc:
        return json_response(
            error_payload(message=str(exc), error_type="InvalidRequest"),
            status_code=400,
        )

    try:
        result = process_source_blob(get_blob_service(), blob_name, embed_requested)
    except BlobSourceNotFoundError:
        return build_not_found_response(blob_name, SOURCE_CONTAINER)
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to process blob %s", blob_name)
        return build_failure_response(
            exc,
            source_blob_name=blob_name,
            cleaned_output_blob_name=cleaned_blob_name(blob_name),
        )

    return json_response(result, status_code=200)


@app.function_name(name="clean_gsc_batch")
@app.route(route="gsc/clean-batch", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def clean_gsc_batch(req: func.HttpRequest) -> func.HttpResponse:
    options, error_response = parse_batch_request(req, default_limit=5)
    if error_response:
        return error_response

    assert options is not None

    processed_items = []
    skipped_items = []
    errors = []

    try:
        service = get_blob_service()
        source_container = service.get_container_client(SOURCE_CONTAINER)

        for blob in source_container.list_blobs(name_starts_with=options["prefix"]):
            blob_name = blob.name

            if not blob_name.lower().endswith(".aspx"):
                skipped_items.append({"blob": blob_name, "reason": "Not an .aspx blob"})
                continue

            if len(processed_items) >= options["limit"]:
                break

            try:
                item = clean_source_blob(service, blob_name)
                item.pop("cleaned_text", None)
                processed_items.append(item)
            except BlobSourceNotFoundError as exc:
                logging.exception("Source blob disappeared during clean batch: %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "cleaned_output_blob_name": cleaned_blob_name(blob_name),
                        "error_type": "BlobNotFound",
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to clean batch blob %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "cleaned_output_blob_name": cleaned_blob_name(blob_name),
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to initialize clean batch processing")
        return build_failure_response(exc)

    return json_response(
        {
            "status": "completed",
            "stage": "clean",
            "source_container": SOURCE_CONTAINER,
            "output_container": CLEANED_CONTAINER,
            "prefix": options["prefix"],
            "limit": options["limit"],
            "processed_count": len(processed_items),
            "skipped_count": len(skipped_items),
            "error_count": len(errors),
            "processed_items": processed_items,
            "skipped_items": skipped_items,
            "errors": errors,
        },
        status_code=200,
    )


@app.function_name(name="embed_cleaned_batch")
@app.route(route="gsc/embed-batch", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def embed_cleaned_batch(req: func.HttpRequest) -> func.HttpResponse:
    options, error_response = parse_batch_request(req, default_limit=5)
    if error_response:
        return error_response

    assert options is not None

    processed_items = []
    skipped_items = []
    errors = []

    try:
        service = get_blob_service()
        cleaned_container = service.get_container_client(CLEANED_CONTAINER)

        for blob in cleaned_container.list_blobs(name_starts_with=options["prefix"]):
            blob_name = blob.name

            if not blob_name.lower().endswith(".txt"):
                skipped_items.append({"blob": blob_name, "reason": "Not a .txt blob"})
                continue

            if len(processed_items) >= options["limit"]:
                break

            try:
                processed_items.append(embed_cleaned_blob(service, blob_name))
            except BlobSourceNotFoundError as exc:
                logging.exception("Cleaned blob disappeared during embed batch: %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "error_type": "BlobNotFound",
                        "message": str(exc),
                    }
                )
            except Exception as exc:  # noqa: BLE001
                logging.exception("Failed to embed batch blob %s", blob_name)
                errors.append(
                    {
                        "blob": blob_name,
                        "error_type": type(exc).__name__,
                        "message": str(exc),
                    }
                )
    except Exception as exc:  # noqa: BLE001
        logging.exception("Failed to initialize embed batch processing")
        return build_failure_response(exc)

    return json_response(
        {
            "status": "completed",
            "stage": "embed",
            "source_container": CLEANED_CONTAINER,
            "prefix": options["prefix"],
            "limit": options["limit"],
            "processed_count": len(processed_items),
            "skipped_count": len(skipped_items),
            "error_count": len(errors),
            "processed_items": processed_items,
            "skipped_items": skipped_items,
            "errors": errors,
        },
        status_code=200,
    )


@app.function_name(name="process_gsc_batch")
@app.route(route="gsc/process-batch", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def process_gsc_batch(req: func.HttpRequest) -> func.HttpResponse:
    options, error_response = parse_batch_request(req, default_limit=5)
    if error_response:
        return error_response

    assert options is not None

    processed_items = []
    skipped_items = []
    errors = []

    try:
        service = get_blob_service()
        source_container = service.get_container_client(SOURCE_CONTAINER)

        for blob in source_container.list_blobs(name_starts_with=options["prefix"]):
            blob_name = blob.name

            if not blob_name.lower().endswith(".aspx"):
                skipped_items.append({"blob": blob_name, "reason": "Not an .aspx blob"})
                continue

            if len(processed_items) >= options["limit"]:
                break

            try:
                processed_items.append(process_source_blob(service, blob_name, options["embed_requested"]))
            except BlobSourceNotFoundError as exc:
                logging.exception("Source blob disappeared during process batch: %s", blob_name)
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
        logging.exception("Failed to initialize combined batch processing")
        return build_failure_response(exc)

    return json_response(
        {
            "status": "completed",
            "stage": "process",
            "source_container": SOURCE_CONTAINER,
            "output_container": CLEANED_CONTAINER,
            "prefix": options["prefix"],
            "limit": options["limit"],
            "embed_requested": options["embed_requested"],
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
@app.route(route="gsc/health", methods=["GET"], auth_level=func.AuthLevel.FUNCTION)
def health_check(req: func.HttpRequest) -> func.HttpResponse:
    del req

    env_report = {
        "required": {
            canonical_name: {
                "present": first_present_env(*env_names) is not None,
                **(
                    {"resolved_from": resolved_name}
                    if (resolved_name := first_present_env(*env_names)) is not None
                    else {}
                ),
            }
            for canonical_name, env_names in REQUIRED_ENV_VARS.items()
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
            "EMBEDDING_BATCH_SIZE": os.getenv("EMBEDDING_BATCH_SIZE", "16"),
            "EMBEDDING_MAX_RETRIES": os.getenv("EMBEDDING_MAX_RETRIES", "3"),
            "EMBEDDING_RETRY_DELAY_SECONDS": os.getenv("EMBEDDING_RETRY_DELAY_SECONDS", "5"),
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
            "functions": [
                "clean_one_gsc_blob",
                "embed_one_cleaned_blob",
                "process_one_gsc_blob",
                "clean_gsc_batch",
                "embed_cleaned_batch",
                "process_gsc_batch",
                "health_check",
            ],
        },
        status_code=200 if overall_healthy else 503,
    )
