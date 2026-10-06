from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Google Drive
    google_credentials_file: str = "/data/token.json"
    # OAuth client (user sign-in mode, no service account). Used by `python -m app.authorize`.
    google_oauth_client_id: str = ""
    google_oauth_client_secret: str = ""
    # Folder where OCR results are written. Empty = same folder as the source CV.
    output_folder_id: str = ""
    # Recursive folder jobs move each finished subfolder here. Empty = leave them in place.
    processed_folder_id: str = ""
    # JSON file next to each CV with info about the email it came from (matched case-insensitively).
    # Its content is copied into the .ocr.json as "metadata" (null if missing or invalid).
    metadata_filename: str = "metaData.txt"

    # OCR
    ocr_lang: str = "fr"  # French CVs
    pdf_dpi: int = 200
    max_pages: int = 10
    max_file_mb: int = 25
    # oneDNN CPU acceleration. Set false if Paddle crashes with oneDNN errors (slower, but safe).
    ocr_enable_mkldnn: bool = True
    # auto = GPU if the image and host have one, else CPU. Also: cpu, gpu, gpu:1, ...
    ocr_device: str = "auto"

    # Persistent logs (app.log, errors.log, jobs.jsonl). Empty = console only.
    log_dir: str = "/logs"
    log_retention_days: int = 30

    # Optional shared secret; if set, clients must send it in the X-API-Key header.
    api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
