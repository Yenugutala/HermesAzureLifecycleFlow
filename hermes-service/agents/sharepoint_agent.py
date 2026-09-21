"""
SharePoint Agent — reads BRD files from OneDrive/SharePoint via Microsoft Graph API.
"""
import os
import httpx

AZURE_TENANT_ID = os.environ.get("MICROSOFT_APP_TENANT", "")
AZURE_CLIENT_ID = os.environ.get("MICROSOFT_APP_ID", "")
AZURE_CLIENT_SECRET = os.environ.get("MICROSOFT_APP_SECRET", "")

_token_cache: dict = {}


async def _get_access_token() -> str:
    """Get Microsoft Graph API access token using client credentials."""
    if _token_cache.get("token"):
        return _token_cache["token"]

    url = f"https://login.microsoftonline.com/{AZURE_TENANT_ID}/oauth2/v2.0/token"
    data = {
        "grant_type": "client_credentials",
        "client_id": AZURE_CLIENT_ID,
        "client_secret": AZURE_CLIENT_SECRET,
        "scope": "https://graph.microsoft.com/.default",
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(url, data=data)
        resp.raise_for_status()
        token = resp.json()["access_token"]
        _token_cache["token"] = token
        return token


async def search_brd_files(query: str) -> list[dict]:
    """Search OneDrive for BRD files matching query."""
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://graph.microsoft.com/v1.0/me/drive/root/search(q='{query}')"
    async with httpx.AsyncClient() as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        items = resp.json().get("value", [])
        return [{"id": i["id"], "name": i["name"], "size": i.get("size", 0)} for i in items]


async def download_file_text(file_id: str) -> str:
    """Download file content and return as plain text."""
    token = await _get_access_token()
    headers = {"Authorization": f"Bearer {token}"}

    # Get download URL
    url = f"https://graph.microsoft.com/v1.0/me/drive/items/{file_id}/content"
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        # Return raw text (works for .txt files; Word docs return binary)
        try:
            return resp.text
        except Exception:
            return resp.content.decode("utf-8", errors="ignore")
