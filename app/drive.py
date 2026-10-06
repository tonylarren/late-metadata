import io

import google.auth
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

SCOPES = ["https://www.googleapis.com/auth/drive"]
FOLDER_MIME = "application/vnd.google-apps.folder"
GOOGLE_DOC_MIME = "application/vnd.google-apps.document"


class DriveClient:
    """Thin wrapper over the Drive v3 API with Shared Drive support enabled on every call."""

    def __init__(self, credentials_file: str):
        # Accepts a service-account key, or keyless gcloud ADC files
        # (impersonated_service_account / authorized_user / external_account).
        self._creds, _ = google.auth.load_credentials_from_file(credentials_file, scopes=SCOPES)

    def _service(self):
        # googleapiclient/httplib2 is not thread-safe, so build a client per call.
        return build("drive", "v3", credentials=self._creds, cache_discovery=False)

    def get_metadata(self, file_id: str) -> dict:
        return (
            self._service()
            .files()
            .get(
                fileId=file_id,
                fields="id, name, mimeType, parents, size",
                supportsAllDrives=True,
            )
            .execute()
        )

    def download(self, file_id: str, mime_type: str) -> tuple[bytes, str]:
        """Return (content, effective_mime). Google Docs are exported as PDF."""
        files = self._service().files()
        if mime_type == GOOGLE_DOC_MIME:
            request = files.export_media(fileId=file_id, mimeType="application/pdf")
            mime_type = "application/pdf"
        else:
            request = files.get_media(fileId=file_id, supportsAllDrives=True)

        buf = io.BytesIO()
        downloader = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
        return buf.getvalue(), mime_type

    def list_folder(self, folder_id: str) -> list[dict]:
        files = self._service().files()
        items, page_token = [], None
        while True:
            resp = (
                files.list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, mimeType, parents, size)",
                    includeItemsFromAllDrives=True,
                    supportsAllDrives=True,
                    pageSize=200,
                    pageToken=page_token,
                )
                .execute()
            )
            items.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                return items

    def walk(self, folder_id: str, prefix: str = "") -> list[tuple[str, dict]]:
        """Every non-folder item under folder_id (any depth), paired with its relative path."""
        found = []
        for item in self.list_folder(folder_id):
            path = prefix + item["name"]
            if item["mimeType"] == FOLDER_MIME:
                found.extend(self.walk(item["id"], path + "/"))
            else:
                found.append((path, item))
        return found

    def move(self, file_id: str, from_folder: str, to_folder: str) -> dict:
        """Move a file or folder. In a Shared Drive this needs Content manager access."""
        return (
            self._service()
            .files()
            .update(
                fileId=file_id,
                addParents=to_folder,
                removeParents=from_folder,
                fields="id, parents",
                supportsAllDrives=True,
            )
            .execute()
        )

    def find_in_folder(self, folder_id: str, name: str) -> dict | None:
        escaped = name.replace("\\", "\\\\").replace("'", "\\'")
        resp = (
            self._service()
            .files()
            .list(
                q=f"'{folder_id}' in parents and name = '{escaped}' and trashed = false",
                fields="files(id, name, webViewLink)",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
                pageSize=1,
            )
            .execute()
        )
        found = resp.get("files", [])
        return found[0] if found else None

    def upsert(self, folder_id: str, name: str, content: bytes, mime_type: str) -> dict:
        """Create the file in the folder, or overwrite it if a file with that name exists."""
        files = self._service().files()
        media = MediaIoBaseUpload(io.BytesIO(content), mimetype=mime_type, resumable=False)
        existing = self.find_in_folder(folder_id, name)
        if existing:
            req = files.update(
                fileId=existing["id"],
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
        else:
            req = files.create(
                body={"name": name, "parents": [folder_id]},
                media_body=media,
                fields="id, name, webViewLink",
                supportsAllDrives=True,
            )
        return req.execute()
