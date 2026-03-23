import json
import logging
import os
import re

import azure.functions as func
from azure.storage.blob import BlobServiceClient

from aspx_cleaner import extract_visible_text
from pgvector_ingest import ingest_cleaned_text

app = func.FunctionApp()

STORAGE_SETTING = "cdooaipocdata1_STORAGE"
SOURCE_CONTAINER = "gsc"
CLEANED_CONTAINER = os.getenv("OUTPUT_CONTAINER", "gsc-cleaned")


def get_blob_service() -> BlobServiceClient:
    return BlobServiceClient.from_connection_string(os.environ[STORAGE_SETTING])


def upload_text(container_name: str, blob_name: str, text: str) -> None:
    client = get_blob_service().get_blob_client(container=container_name, blob=blob_name)
    client.upload_blob(text.encode("utf-8"), overwrite=True)


def cleaned_blob_name(source_blob_name: str) -> str:
    return re.sub(r"\.aspx$", ".txt", source_blob_name, flags=re.IGNORECASE)


@app.function_name(name="clean_gsc_aspx_on_upload")
@app.blob_trigger(arg_name="myblob", path="gsc/{name}", connection=STORAGE_SETTING)
def clean_gsc_aspx_on_upload(myblob: func.InputStream, name: str) -> None:
    if not name.lower().endswith(".aspx"):
        logging.info("Skipping non-ASPX blob: %s", name)
        return

    raw = myblob.read().decode("utf-8", errors="ignore")
    cleaned = extract_visible_text(raw)
    output_name = cleaned_blob_name(name)
    upload_text(CLEANED_CONTAINER, output_name, cleaned)

    logging.info("Cleaned %s -> %s/%s", name, CLEANED_CONTAINER, output_name)


@app.function_name(name="embed_gsc_cleaned_txt_on_upload")
@app.blob_trigger(arg_name="myblob", path="gsc-cleaned/{name}", connection=STORAGE_SETTING)
def embed_gsc_cleaned_txt_on_upload(myblob: func.InputStream, name: str) -> None:
    if not name.lower().endswith(".txt"):
        logging.info("Skipping non-TXT blob: %s", name)
        return

    raw = myblob.read().decode("utf-8", errors="ignore")
    report = ingest_cleaned_text(source_name=name, raw_text=raw)

    if report.get("skipped_count"):
        logging.warning("Embedding skip report for %s: %s", name, json.dumps(report))
    else:
        logging.info("Embedded %s report=%s", name, json.dumps(report))


@app.function_name(name="backfill_existing_gsc")
@app.route(route="admin/backfill-gsc", methods=["POST"], auth_level=func.AuthLevel.FUNCTION)
def backfill_existing_gsc(req: func.HttpRequest) -> func.HttpResponse:
    """
    Manually process existing ASPX blobs already in gsc and write cleaned TXT to gsc-cleaned.
    Those uploads will then trigger the embedding function automatically.
    """
    try:
        limit = int(req.params.get("limit", "100"))
    except ValueError:
        limit = 100

    prefix = req.params.get("prefix", "")

    service = get_blob_service()
    source = service.get_container_client(SOURCE_CONTAINER)

    processed = 0
    skipped = 0
    errors = []

    for blob in source.list_blobs(name_starts_with=prefix):
        if processed >= limit:
            break

        if not blob.name.lower().endswith(".aspx"):
            skipped += 1
            continue

        try:
            raw = source.download_blob(blob.name).readall().decode("utf-8", errors="ignore")
            cleaned = extract_visible_text(raw)
            upload_text(CLEANED_CONTAINER, cleaned_blob_name(blob.name), cleaned)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            errors.append({"blob": blob.name, "error": str(exc)})

    body = {
        "processed": processed,
        "skipped": skipped,
        "errors": errors,
        "error_count": len(errors),
        "prefix": prefix,
        "limit": limit,
        "output_container": CLEANED_CONTAINER,
    }
    return func.HttpResponse(json.dumps(body, indent=2), mimetype="application/json")
