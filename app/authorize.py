"""One-time Google sign-in so the service can act as a regular Drive user (no service account).

Run inside the container:
    docker compose run --rm cv-ocr python -m app.authorize
"""

import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from google_auth_oauthlib.flow import Flow

from app.config import get_settings
from app.drive import SCOPES, DriveClient

REDIRECT_URI = "http://localhost"


def main():
    # Google may return the granted scopes in a different order/set; don't fail on that.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
    settings = get_settings()
    if not (settings.google_oauth_client_id and settings.google_oauth_client_secret):
        raise SystemExit("Set GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET in .env first.")

    flow = Flow.from_client_config(
        {
            "installed": {
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                "token_uri": "https://oauth2.googleapis.com/token",
                "redirect_uris": [REDIRECT_URI],
            }
        },
        scopes=SCOPES,
        redirect_uri=REDIRECT_URI,
    )
    # prompt=consent guarantees a refresh token is issued.
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

    print("\n1. Open this link in a browser on any computer and sign in with the account")
    print("   that has access to the Shared Drive:\n")
    print(f"   {auth_url}\n")
    print("2. After approving, the browser goes to a 'localhost' page that fails to load.")
    print("   That's expected. Copy the FULL address from the address bar and paste it here.\n")
    answer = input("Paste URL: ").strip()

    code = parse_qs(urlparse(answer).query).get("code", [None])[0] if answer.startswith("http") else answer
    if not code:
        raise SystemExit("No 'code=' found in that URL. Run the command again.")
    flow.fetch_token(code=code)

    creds = flow.credentials
    if not creds.refresh_token:
        raise SystemExit("Google did not return a refresh token. Run the command again.")

    path = Path(settings.google_credentials_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "authorized_user",
                "client_id": settings.google_oauth_client_id,
                "client_secret": settings.google_oauth_client_secret,
                "refresh_token": creds.refresh_token,
            }
        )
    )
    path.chmod(0o600)

    about = DriveClient(str(path))._service().about().get(fields="user(emailAddress)").execute()
    print(f"\nSigned in as {about['user']['emailAddress']}. Token saved to {path}.")
    print("Start the service with:  docker compose up -d")


if __name__ == "__main__":
    main()
