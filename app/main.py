import json
import logging
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import PurePath

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from googleapiclient.errors import HttpError

from app.config import Settings, get_settings
from app.drive import FOLDER_MIME, GOOGLE_DOC_MIME, DriveClient
from app.ocr import SUPPORTED_MIMES, OCRService
from app.logging_setup import record_job, setup_logging
from app.schemas import FolderRequest, JobStatus, OCRRequest, OCRResponse

setup_logging(get_settings().log_dir, get_settings().log_retention_days)
log = logging.getLogger("cv-ocr")

TXT_SUFFIX = ".ocr.txt"
JSON_SUFFIX = ".ocr.json"
PROCESSABLE_MIMES = SUPPORTED_MIMES | {GOOGLE_DOC_MIME}

# In-memory job registry; resets on restart (fine for a single-instance service).
jobs: dict[str, JobStatus] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    if not os.path.isfile(settings.google_credentials_file):
        raise RuntimeError(
            f"Google credentials not found at {settings.google_credentials_file}. "
            "Sign in first: docker compose run --rm cv-ocr python -m app.authorize"
        )
    log.info("Loading PaddleOCR (lang=%s)...", settings.ocr_lang)
    app.state.ocr = OCRService(
        settings.ocr_lang,
        settings.pdf_dpi,
        settings.max_pages,
        settings.ocr_enable_mkldnn,
        settings.ocr_device,
    )
    app.state.drive = DriveClient(settings.google_credentials_file)
    log.info("Ready")
    yield


app = FastAPI(title="CV OCR Service", version="1.0.0", lifespan=lifespan)


def require_api_key(
    x_api_key: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
):
    if settings.api_key and x_api_key != settings.api_key:
        raise HTTPException(status_code=401, detail="Invalid or missing X-API-Key")


class PipelineError(Exception):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail


@app.exception_handler(PipelineError)
async def pipeline_error_handler(request: Request, exc: PipelineError):
    log.warning("%s %s -> %d: %s", request.method, request.url.path, exc.status_code, exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(HttpError)
async def drive_error_handler(request: Request, exc: HttpError):
    status = exc.resp.status
    detail = exc.reason or str(exc)
    log.warning("%s %s -> Drive error %d: %s", request.method, request.url.path, status, detail)
    if status in (403, 404):
        detail += (
            " — check the ID and that the signed-in account (or service account) is a member"
            " of the Shared Drive with at least Contributor access."
        )
    return JSONResponse(status_code=status, content={"detail": detail})


def output_names(source_name: str) -> tuple[str, str]:
    stem = PurePath(source_name).stem or source_name
    return stem + TXT_SUFFIX, stem + JSON_SUFFIX


def is_metadata_file(name: str) -> bool:
    return name.lower() == get_settings().metadata_filename.lower()


def load_metadata(folder_id: str | None) -> tuple[dict | None, str | None]:
    """Read the folder's metaData.txt as JSON. Returns (metadata, warning).

    Never fails the CV: a missing, unreadable or non-JSON file gives (None, reason).
    """
    filename = get_settings().metadata_filename
    if not folder_id:
        return None, f"{filename}: CV has no parent folder"
    drive: DriveClient = app.state.drive
    try:
        match = next(
            (
                i
                for i in drive.list_folder(folder_id)
                if is_metadata_file(i["name"]) and i["mimeType"] != FOLDER_MIME
            ),
            None,
        )
        if not match:
            return None, f"{filename} not found"
        data, _ = drive.download(match["id"], match["mimeType"])
        metadata = json.loads(data.decode("utf-8-sig"))  # tolerate a BOM from Windows editors
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, f"{filename} is not valid JSON: {exc}"
    except Exception as exc:
        log.warning("Could not read %s in %s: %s", filename, folder_id, exc)
        return None, f"{filename} could not be read: {exc}"
    if not isinstance(metadata, dict):
        return None, f"{filename} must contain a JSON object, got {type(metadata).__name__}"
    return metadata, None


def process_file(file_id: str, output_folder_id: str | None, meta: dict | None = None) -> dict:
    """Download a CV from Drive, OCR it, and write .ocr.txt + .ocr.json back to Drive."""
    settings = get_settings()
    drive: DriveClient = app.state.drive
    ocr: OCRService = app.state.ocr

    meta = meta or drive.get_metadata(file_id)
    name, mime = meta["name"], meta["mimeType"]
    if mime == FOLDER_MIME:
        raise PipelineError(
            400, f"'{name}' is a folder. Use POST /ocr/folder/{file_id} to process the CVs inside it."
        )
    if mime not in PROCESSABLE_MIMES:
        raise PipelineError(415, f"'{name}' has unsupported type {mime}")
    if int(meta.get("size") or 0) > settings.max_file_mb * 1024 * 1024:
        raise PipelineError(413, f"'{name}' exceeds {settings.max_file_mb} MB")

    target_folder = output_folder_id or settings.output_folder_id or (meta.get("parents") or [None])[0]
    if not target_folder:
        raise PipelineError(400, "No output folder: set OUTPUT_FOLDER_ID or pass output_folder_id")

    log.info("OCR start: %s (%s)", name, file_id)
    data, effective_mime = drive.download(file_id, mime)
    try:
        pages = ocr.process(data, effective_mime)
    except Exception as exc:
        raise PipelineError(422, f"Could not OCR '{name}': {exc}") from exc

    full_text = "\n\n".join(
        (f"--- Page {p['page']} ---\n" if len(pages) > 1 else "") + p["text"] for p in pages
    )
    metadata, warning = load_metadata((meta.get("parents") or [None])[0])
    if warning:
        log.warning("%s: %s", name, warning)

    txt_name, json_name = output_names(name)
    # Text only (no per-page copy): this file is meant for LLM extraction.
    payload = {
        "source": {"id": file_id, "name": name, "mimeType": mime},
        "metadata": metadata,
        "page_count": len(pages),
        "text": full_text,
    }
    outputs = [
        drive.upsert(target_folder, txt_name, full_text.encode("utf-8"), "text/plain"),
        drive.upsert(
            target_folder,
            json_name,
            json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
            "application/json",
        ),
    ]
    log.info("OCR done: %s -> %d page(s)", name, len(pages))
    return {
        **payload,
        "file_id": file_id,
        "file_name": name,
        "outputs": outputs,
        "warnings": [warning] if warning else [],
    }


def is_result_file(name: str) -> bool:
    return name.endswith((TXT_SUFFIX, JSON_SUFFIX))


def process_items(
    job: JobStatus,
    items: list[tuple[str, dict]],
    out_folder: str | None,
    overwrite: bool,
    existing: dict[str, set[str]],
) -> tuple[int, int]:
    """OCR (path, item) pairs. out_folder=None writes next to each CV.

    `existing` maps folder id -> file names already in it (used to skip done CVs).
    Returns (cvs_done, errors), where cvs_done includes already-processed CVs.
    """
    drive: DriveClient = app.state.drive
    done = errors = 0
    for path, item in items:
        if is_result_file(item["name"]) or is_metadata_file(item["name"]):
            continue
        if item["mimeType"] not in PROCESSABLE_MIMES:
            job.skipped.append(f"{path} (unsupported type)")
            continue
        target = out_folder or item["parents"][0]
        if target not in existing:
            existing[target] = {i["name"] for i in drive.list_folder(target)}
        if not overwrite and output_names(item["name"])[0] in existing[target]:
            job.skipped.append(f"{path} (already processed)")
            done += 1
            continue
        try:
            result = process_file(item["id"], target, meta=item)
            job.processed.append(path)
            if result["warnings"]:
                job.warnings[path] = "; ".join(result["warnings"])
            done += 1
        except PipelineError as exc:
            job.errors[path] = exc.detail
            errors += 1
        except Exception as exc:  # keep going on per-file failures
            log.exception("Failed on %s", path)
            job.errors[path] = str(exc)
            errors += 1
    return done, errors


def names_by_parent(items: list[tuple[str, dict]]) -> dict[str, set[str]]:
    names: dict[str, set[str]] = {}
    for _, item in items:
        for parent in item.get("parents", []):
            names.setdefault(parent, set()).add(item["name"])
    return names


def count_candidates(items: list[tuple[str, dict]]) -> int:
    return sum(
        1
        for _, item in items
        if not is_result_file(item["name"]) and not is_metadata_file(item["name"])
    )


def run_folder_job(
    job_id: str,
    folder_id: str,
    output_folder_id: str | None,
    overwrite: bool,
    recursive: bool,
    processed_folder_id: str | None,
):
    job = jobs[job_id]
    job.status = "running"
    job.started_at = now_iso()
    drive: DriveClient = app.state.drive
    settings = get_settings()
    try:
        top = drive.list_folder(folder_id)
        loose = [(i["name"], i) for i in top if i["mimeType"] != FOLDER_MIME]

        if not recursive:
            out_folder = output_folder_id or settings.output_folder_id or None
            existing = names_by_parent(loose)
            job.total = count_candidates(loose)
            process_items(job, loose, out_folder, overwrite, existing)
        else:
            # One subfolder = one candidate. Results go next to each CV so they
            # move together with it, and same-named CVs in different folders don't collide.
            move_to = processed_folder_id or settings.processed_folder_id or None
            if move_to == folder_id:  # re-running on Processed itself: nothing to move
                move_to = None
            subfolders = [i for i in top if i["mimeType"] == FOLDER_MIME and i["id"] != move_to]
            groups = [(None, loose)] + [(sub, drive.walk(sub["id"], sub["name"] + "/")) for sub in subfolders]
            job.total = sum(count_candidates(items) for _, items in groups)

            for sub, items in groups:
                done, errors = process_items(job, items, None, overwrite, names_by_parent(items))
                # Only move folders that actually contained a CV and had no failures;
                # empty folders may still be receiving uploads.
                if sub and move_to and done and not errors:
                    try:
                        drive.move(sub["id"], folder_id, move_to)
                        job.moved_folders.append(sub["name"])
                    except Exception as exc:
                        log.exception("Could not move %s", sub["name"])
                        job.errors[sub["name"] + "/"] = f"OCR done but move failed: {exc}"
        job.status = "completed"
        log.info(
            "Folder job %s done: %d processed, %d skipped, %d errors, %d moved",
            job_id, len(job.processed), len(job.skipped), len(job.errors), len(job.moved_folders),
        )
    except Exception as exc:
        log.exception("Folder job %s failed", job_id)
        job.status = "failed"
        job.detail = str(exc)
    finally:
        job.finished_at = now_iso()
        if job.errors:
            log.error("Folder job %s had %d error(s): %s", job_id, len(job.errors), job.errors)
        if job.warnings:
            log.warning("Folder job %s had %d warning(s): %s", job_id, len(job.warnings), job.warnings)
        record_job(job.model_dump())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/health")
def health():
    return {"status": "ok", "device": app.state.ocr.device}


@app.post(
    "/ocr/file/{file_id}",
    response_model=OCRResponse,
    dependencies=[Depends(require_api_key)],
)
def ocr_file(file_id: str, body: OCRRequest | None = None):
    """OCR a single CV (PDF, image or Google Doc) and write results back to the drive."""
    body = body or OCRRequest()
    return process_file(file_id, body.output_folder_id)


@app.post(
    "/ocr/folder/{folder_id}",
    response_model=JobStatus,
    status_code=202,
    dependencies=[Depends(require_api_key)],
)
def ocr_folder(folder_id: str, background: BackgroundTasks, body: FolderRequest | None = None):
    """Queue OCR for every unprocessed CV in a folder. Poll GET /jobs/{job_id} for progress.

    With recursive=true, each subfolder is scanned (any depth) and, if a processed
    folder is configured, moved there once its CV is done.
    """
    body = body or FolderRequest()
    running = next(
        (j for j in jobs.values() if j.folder_id == folder_id and j.status in ("queued", "running")),
        None,
    )
    if running:
        raise HTTPException(
            status_code=409, detail=f"Job {running.job_id} is already processing this folder"
        )
    job_id = uuid.uuid4().hex
    jobs[job_id] = JobStatus(
        job_id=job_id, folder_id=folder_id, status="queued", recursive=body.recursive
    )
    background.add_task(
        run_folder_job,
        job_id,
        folder_id,
        body.output_folder_id,
        body.overwrite,
        body.recursive,
        body.processed_folder_id,
    )
    return jobs[job_id]


@app.get("/jobs/{job_id}", response_model=JobStatus, dependencies=[Depends(require_api_key)])
def get_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return jobs[job_id]
