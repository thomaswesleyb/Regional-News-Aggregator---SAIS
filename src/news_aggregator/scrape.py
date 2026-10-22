"""Pull the lead paragraph from an article page."""

import logging

import httpx
import trafilatura

logger = logging.getLogger(__name__)

MIN_PARAGRAPH_CHARS = 80
MAX_PARAGRAPH_CHARS = 700


def lead_paragraph(page_html: str, url: str) -> str | None:
    """Return the first substantive paragraph of an article, or None if none is found."""
    document = trafilatura.bare_extraction(page_html, url=url, include_comments=False)
    if document is None or isinstance(document, dict) or not document.text:
        return None
    paragraphs = [line.strip() for line in document.text.split("\n") if line.strip()]
    substantive = [p for p in paragraphs if len(p) >= MIN_PARAGRAPH_CHARS]
    if not substantive:
        return None
    return _truncate(substantive[0])


def _truncate(text: str) -> str:
    if len(text) <= MAX_PARAGRAPH_CHARS:
        return text
    cut = text[:MAX_PARAGRAPH_CHARS].rsplit(" ", 1)[0]
    return f"{cut}…"


def fetch_lead_paragraph(client: httpx.Client, url: str) -> str | None:
    """Download an article and extract its lead paragraph.

    Returns None (and logs why) when the page can't be fetched or has no
    extractable text, e.g. paywalls or bot blocks; callers fall back to the
    feed summary.
    """
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError as error:
        logger.warning("could not scrape %s: %s", url, error)
        return None
    paragraph = lead_paragraph(response.text, url)
    if paragraph is None:
        logger.warning("no article text found at %s", url)
    return paragraph
