"""Logging to the console (docker logs) and to persistent files in LOG_DIR.

LOG_DIR (mounted from ./logs on the host) gets:
  app.log     everything, rotated at midnight, LOG_RETENTION_DAYS days kept
  errors.log  warnings and errors only, same rotation
  jobs.jsonl  one JSON line per finished folder job (see record_job)
"""

import json
import logging
import os
import sys
import threading
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
JOBS_FILE = "jobs.jsonl"

_jobs_lock = threading.Lock()
_file_logging_dir: str | None = None


def setup_logging(log_dir: str, retention_days: int) -> None:
    """Configure console + file logging. A log folder that can't be written never stops the app."""
    global _file_logging_dir
    formatter = logging.Formatter(FORMAT)
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    handlers: list[logging.Handler] = [console]

    if log_dir:
        try:
            os.makedirs(log_dir, exist_ok=True)
            for filename, level in (("app.log", logging.INFO), ("errors.log", logging.WARNING)):
                handler = TimedRotatingFileHandler(
                    os.path.join(log_dir, filename),
                    when="midnight",
                    backupCount=retention_days,
                    encoding="utf-8",
                )
                handler.setLevel(level)
                handler.setFormatter(formatter)
                handlers.append(handler)
            _file_logging_dir = log_dir
        except OSError as exc:
            print(
                f"WARNING: cannot write logs to {log_dir} ({exc}); logging to console only. "
                "On the server run: sudo chown -R 1000:1000 logs",
                file=sys.stderr,
            )

    root.handlers = handlers
    # Uvicorn logs unhandled request errors under "uvicorn.error" without passing them
    # to the root logger, so give it the file handlers too (not the console: it has its own).
    uvicorn_logger = logging.getLogger("uvicorn")
    for handler in handlers[1:]:
        if handler not in uvicorn_logger.handlers:
            uvicorn_logger.addHandler(handler)


def record_job(job: dict) -> None:
    """Append a finished folder job to jobs.jsonl so results survive restarts."""
    if not _file_logging_dir:
        return
    line = json.dumps(
        {"logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), **job},
        ensure_ascii=False,
    )
    try:
        with _jobs_lock, open(
            os.path.join(_file_logging_dir, JOBS_FILE), "a", encoding="utf-8"
        ) as f:
            f.write(line + "\n")
    except OSError:
        logging.getLogger("cv-ocr").exception("Could not write %s", JOBS_FILE)
