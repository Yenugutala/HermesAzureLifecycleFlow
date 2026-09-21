"""
Confluence → vector index builder.

Run once before starting hermes-service, or whenever Confluence content changes:
    cd hermes-service
    source .venv311/bin/activate
    python scripts/build_index.py

Fetches all pages from CONFLUENCE_SPACE_KEY via Confluence REST API,
generates sentence-transformer embeddings (all-MiniLM-L6-v2, runs on CPU),
and writes data/confluence_index.json.

No API keys required beyond the existing JIRA_EMAIL + JIRA_API_TOKEN in .env.
"""
import json
import logging
import os
import sys
from pathlib import Path

# Allow imports from hermes-service root
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv, find_dotenv
load_dotenv(find_dotenv(usecwd=False))

import requests
from bs4 import BeautifulSoup
from sentence_transformers import SentenceTransformer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")
log = logging.getLogger(__name__)

JIRA_BASE_URL  = os.environ["JIRA_BASE_URL"]
JIRA_EMAIL     = os.environ["JIRA_EMAIL"]
JIRA_API_TOKEN = os.environ["JIRA_API_TOKEN"]
SPACE_KEY      = os.environ.get("CONFLUENCE_SPACE_KEY", "")
OUT_PATH       = Path(__file__).parent.parent / "data" / "confluence_index.json"


def _html_to_text(html: str) -> str:
    """Strip Confluence storage-format HTML tags → plain text."""
    return BeautifulSoup(html, "html.parser").get_text(" ", strip=True)


def fetch_pages() -> list[dict]:
    """
    Fetch all pages from the configured Confluence space using REST API.
    Paginates automatically until all pages are retrieved.
    """
    auth   = (JIRA_EMAIL, JIRA_API_TOKEN)
    base   = JIRA_BASE_URL.rstrip("/")
    url    = f"{base}/wiki/rest/api/content"
    params = {
        "spaceKey": SPACE_KEY,
        "type":     "page",
        "limit":    50,
        "expand":   "body.storage,_links",
    }
    pages = []
    page_num = 1

    while url:
        log.info("Fetching page batch %d from Confluence...", page_num)
        r = requests.get(url, auth=auth, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()

        for item in data.get("results", []):
            html    = item.get("body", {}).get("storage", {}).get("value", "")
            text    = _html_to_text(html)
            snippet = text[:500]
            web_ui  = item.get("_links", {}).get("webui", "")
            pages.append({
                "page_id":         item["id"],
                "page_title":      item["title"],
                "space_key":       SPACE_KEY,
                "page_url":        f"{base}/wiki{web_ui}" if web_ui else "",
                "content_snippet": snippet,
                "full_text":       text,
            })

        next_link = data.get("_links", {}).get("next")
        url    = f"{base}/wiki{next_link}" if next_link else None
        params = {}   # params are baked into next_link URL
        page_num += 1

    return pages


def main() -> None:
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)

    if not SPACE_KEY:
        log.error("CONFLUENCE_SPACE_KEY is not set in .env — cannot fetch pages")
        sys.exit(1)

    log.info("Fetching Confluence pages from space '%s'...", SPACE_KEY)
    pages = fetch_pages()
    log.info("Found %d pages", len(pages))

    if not pages:
        log.warning("No pages found. Check CONFLUENCE_SPACE_KEY and API credentials.")
        sys.exit(0)

    log.info("Loading sentence-transformers model (all-MiniLM-L6-v2)...")
    model = SentenceTransformer("all-MiniLM-L6-v2")

    # Embed title + first 1000 chars of content for each page
    texts = [
        f"{p['page_title']} {p['full_text']}"[:1000]
        for p in pages
    ]
    log.info("Generating embeddings for %d pages...", len(texts))
    embeddings = model.encode(texts, show_progress_bar=True, batch_size=32).tolist()

    # Build index — exclude full_text (too large), include embedding
    index = []
    for page, emb in zip(pages, embeddings):
        doc = {k: v for k, v in page.items() if k != "full_text"}
        doc["embedding"] = emb
        index.append(doc)

    with open(OUT_PATH, "w") as f:
        json.dump(index, f)

    log.info("Saved %d pages to %s", len(index), OUT_PATH)
    log.info("Done. Start hermes-service and it will load this index automatically.")


if __name__ == "__main__":
    main()
