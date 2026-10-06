#!/usr/bin/env bash
# Nightly OCR of every candidate folder in Input/, run by cron.
#
#   Input/<candidate>/CV.pdf + metaData.txt  ->  OCR'd, then moved to Processed/
#
# Reads from the project's .env (no secrets in this file):
#   API_KEY              same key the service uses
#   INPUT_FOLDER_ID      Drive ID of the Input folder
#   PROCESSED_FOLDER_ID  used by the service itself to move finished folders
#
# Starts one recursive folder job, waits for it to finish and prints a summary.
# Exit code: 0 = all good, 1 = some CVs failed, 2 = job failed / service unreachable.
# The full job result is also appended to logs/jobs.jsonl by the service.
set -u

cd "$(dirname "$0")/.." || exit 2
API="${API:-http://localhost:8001}"
POLL_SECONDS="${POLL_SECONDS:-30}"
MAX_WAIT_SECONDS=$((6 * 3600))

ts() { date '+%F %T'; }
env_value() { grep -E "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- | tr -d '\r"'"'"; }
json_field() { grep -o "\"$1\":\"[^\"]*\"" | head -1 | cut -d'"' -f4; }

API_KEY="$(env_value API_KEY)"
INPUT_FOLDER_ID="$(env_value INPUT_FOLDER_ID)"
if [ -z "$INPUT_FOLDER_ID" ]; then
  echo "$(ts) ERROR INPUT_FOLDER_ID is not set in .env"; exit 2
fi
if [ -z "$(env_value PROCESSED_FOLDER_ID)" ]; then
  echo "$(ts) WARNING PROCESSED_FOLDER_ID is empty: finished folders will stay in Input"
fi

if ! curl -sf "$API/health" > /dev/null; then
  echo "$(ts) ERROR service not reachable at $API (is the cv-ocr-metadata container running?)"; exit 2
fi

echo "$(ts) starting OCR job on Input ($INPUT_FOLDER_ID)"
response="$(curl -s -w '\n%{http_code}' -X POST "$API/ocr/folder/$INPUT_FOLDER_ID" \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" -d '{"recursive": true}')"
code="$(echo "$response" | tail -1)"
body="$(echo "$response" | sed '$d')"
if [ "$code" = "409" ]; then
  echo "$(ts) SKIPPED a job is already running on Input: $body"; exit 0
fi
if [ "$code" != "202" ]; then
  echo "$(ts) ERROR could not start job (HTTP $code): $body"; exit 2
fi
job_id="$(echo "$body" | json_field job_id)"
echo "$(ts) job $job_id started"

waited=0
while :; do
  sleep "$POLL_SECONDS"; waited=$((waited + POLL_SECONDS))
  job="$(curl -s "$API/jobs/$job_id" -H "X-API-Key: $API_KEY")"
  status="$(echo "$job" | json_field status)"
  case "$status" in
    completed|failed) break ;;
    "") echo "$(ts) ERROR lost track of job $job_id (service restarted?): $job"; exit 2 ;;
  esac
  if [ "$waited" -ge "$MAX_WAIT_SECONDS" ]; then
    echo "$(ts) ERROR job $job_id still $status after $((waited / 60)) min; giving up waiting"; exit 2
  fi
done

if [ "$status" = "failed" ]; then
  echo "$(ts) ERROR job $job_id failed: $job"; exit 2
fi
count() { echo "$job" | grep -o "\"$1\":\[[^]]*\]" | grep -o '","\|":\["' | wc -l; }
errors="$(echo "$job" | grep -o '"errors":{[^}]*}')"
warnings="$(echo "$job" | grep -o '"warnings":{[^}]*}')"
echo "$(ts) job $job_id completed: $(count processed) processed, $(count skipped) skipped," \
  "$(count moved_folders) moved"
[ "$warnings" != '"warnings":{}' ] && echo "$(ts) WARNINGS $warnings"
if [ "$errors" != '"errors":{}' ]; then
  echo "$(ts) ERRORS $errors"; exit 1
fi
exit 0
