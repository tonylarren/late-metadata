# CV OCR Service

A FastAPI microservice. It downloads CVs from a Google Shared Drive, runs PaddleOCR on them, and writes the results back to the drive.

For each CV `Jane_Doe.pdf` it writes:
- `Jane_Doe.ocr.txt`: the plain extracted text
- `Jane_Doe.ocr.json`: `source`, `metadata` (the content of the folder's `metaData.txt`, or `null` if it's missing or not valid JSON), `page_count` and `text`. It's built for LLM extraction, so there are no per-line boxes.

It accepts PDFs, images (PNG, JPEG, TIFF, BMP, WEBP, GIF) and native Google Docs, which it exports as PDF first.

## Running next to the original service

This copy is the **metadata version**. It runs as its own container next to the original `cv-ocr` one on the same server:

| | Original (`CV-HR`) | This copy (`CV-HR-metadata`) |
|---|---|---|
| Container | `cv-ocr` | `cv-ocr-metadata` |
| Port | 8000 | **8001** |
| Image | `cv-ocr:cpu` | `cv-ocr-metadata:cpu` |
| Google login volume | `cv-hr_cv-ocr-data` | `cv-ocr-metadata_cv-ocr-data` (sign in once) |

Both write `.ocr.txt` / `.ocr.json` with the same names and can move the same folders. **Don't point both at the same `Input` folder**, and only schedule the nightly cron job for one of them.

## 1. Google setup (one time)

The service signs in as a regular Google user who already has access to the Shared Drive. It doesn't use a service account.

1. In [Google Cloud Console](https://console.cloud.google.com/), create or pick a project and **enable the Google Drive API**.
2. Open **Google Auth Platform → Branding** (older consoles call it the OAuth consent screen). Fill in the app name and support email, and set the **Audience** to **Internal**.
3. Open **Clients → Create client**, choose **Desktop app**, and copy the **Client ID** and **Client secret**.

Folder and file IDs come from the Drive URL: `https://drive.google.com/drive/folders/<FOLDER_ID>`.

## 2. Run on your server

```bash
cp .env.example .env        # set GOOGLE_OAUTH_CLIENT_ID/SECRET, API_KEY, OUTPUT_FOLDER_ID
mkdir -p logs && sudo chown 1000:1000 logs   # the container (uid 1000) writes its logs here
docker compose build
docker compose run --rm cv-ocr python -m app.authorize   # one-time Google sign-in
docker compose up -d
docker compose logs -f
```

The sign-in command prints a link. Open it on any computer and sign in with an account that can edit the Shared Drive. After you approve, the browser lands on a `localhost` page that fails to load. Copy that page's full URL and paste it back into the terminal. The resulting token is stored in the `cv-ocr-data` Docker volume.

A service-account key or a gcloud credentials file still works too: put it in `./secrets/` and set `GOOGLE_CREDENTIALS_FILE=/secrets/<file>.json`.

The first build takes several minutes because it downloads Paddle and bakes the OCR models into the image. After that the container starts quickly and needs no internet access for OCR.

## Logs and troubleshooting

Everything is logged to `./logs/` on the server. The folder survives rebuilds and restarts:

| File | Contains |
|---|---|
| `logs/errors.log` | **Only warnings and errors**: failed CVs, missing or invalid `metaData.txt`, Drive errors, failed folder moves, crashes. Check this one first. |
| `logs/app.log` | Everything: each CV's OCR start and finish, job summaries, startup. |
| `logs/jobs.jsonl` | One JSON line per finished folder job: `started_at`, `finished_at`, `processed`, `skipped`, `moved_folders`, `errors`, `warnings`. |

`app.log` and `errors.log` rotate at midnight; older days get a date suffix (`errors.log.2026-10-06`) and are deleted after `LOG_RETENTION_DAYS` (30). `jobs.jsonl` is small (one line per job) and is never rotated.

```bash
tail -50 logs/errors.log                    # what went wrong recently
grep -h "ERROR" logs/errors.log*            # every error still kept
tail -1 logs/jobs.jsonl                     # result of the last folder job
tail -f logs/app.log                        # watch live
```

If `logs/` isn't writable, the service still runs and prints `cannot write logs to /logs` at startup; fix it with `sudo chown -R 1000:1000 logs` and restart. `docker compose logs` still works too, capped at 3 × 10 MB.

## 3. API

Interactive docs are at `http://<server>:8001/docs`. Every endpoint except `/health` requires the `X-API-Key` header when `API_KEY` is set.

### OCR a single CV (synchronous)
```bash
curl -X POST http://localhost:8001/ocr/file/<FILE_ID> \
  -H "X-API-Key: change-me" -H "Content-Type: application/json" \
  -d '{"output_folder_id": null}'
```
The response contains the full text, the text for each page, and links to the files it wrote to Drive.

### OCR a whole folder (background job)
```bash
curl -X POST http://localhost:8001/ocr/folder/<FOLDER_ID> \
  -H "X-API-Key: change-me" -H "Content-Type: application/json" -d '{"overwrite": false}'
# -> {"job_id": "...", "status": "queued", ...}

curl http://localhost:8001/jobs/<JOB_ID> -H "X-API-Key: change-me"
```
The job skips CVs that already have an `.ocr.txt` result unless you pass `"overwrite": true`. You can safely run it again, for example from cron.

### Automatic nightly run (10 pm)
`scripts/nightly.sh` OCRs every candidate folder in `Input` and moves finished ones to `Processed`. It reads `API_KEY`, `INPUT_FOLDER_ID` and `PROCESSED_FOLDER_ID` from `.env`, waits for the job to finish, and prints a one-line summary plus any warnings and errors. Exit codes: `0` OK, `1` some CVs failed, `2` the job couldn't run.

Cron uses the **server's** clock, so check its timezone with `timedatectl` and pick the hour that is 22:00 for you. For example, `0 22` if the server is on your local time, or `0 19` if it's on UTC and you're on UTC+3:
```bash
(crontab -l 2>/dev/null; echo '0 22 * * * bash /srv/apps/CV-HR-metadata/scripts/nightly.sh >> /srv/apps/CV-HR-metadata/cron.log 2>&1') | crontab -
```
Results: `tail -20 cron.log` (one summary per night) and `logs/jobs.jsonl` (full detail).

### Output location
The service picks the output folder in this order:
1. `output_folder_id` in the request body
2. `OUTPUT_FOLDER_ID` in `.env`
3. the folder that contains the CV

## Configuration (`.env`)

| Var | Default | Notes |
|---|---|---|
| `GOOGLE_CREDENTIALS_FILE` | `/secrets/service-account.json` | path inside the container |
| `OUTPUT_FOLDER_ID` | *(empty)* | empty = write next to each CV |
| `OCR_LANG` | `fr` | PaddleOCR language code (French CVs); **rebuild** after changing it |
| `PDF_DPI` | `200` | higher is more accurate but slower |
| `MAX_PAGES` | `10` | pages OCR'd per file |
| `MAX_FILE_MB` | `25` | larger files are rejected |
| `API_KEY` | *(empty)* | empty disables auth; don't leave it empty on a public server |

## Notes
- **CPU only.** The service handles one OCR at a time and a one-page CV takes a few seconds. Requests arriving meanwhile wait in a queue. For more throughput, run more containers behind a load balancer.
- **Jobs are in memory.** Job status is lost on restart. The CVs already processed stay done, so you can just resend the folder request.
- **Word files (.docx) are not handled**, because OCR is for images. Convert them to PDF or Google Docs first.
