"""Fetch RSS/Atom feeds and turn entries into article candidates."""

import calendar
import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import httpx

from news_aggregator.config import NewsSource

logger = logging.getLogger(__name__)

TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|ref$|cmpid$)", re.IGNORECASE)
TAG_PATTERN = re.compile(r"<[^>]+>")
WHITESPACE = re.compile(r"\s+")


class FeedError(Exception):
    """A feed could not be fetched or parsed."""


@dataclass(frozen=True)
class Candidate:
    title: str
    url: str
    summary: str
    published: datetime
    source: NewsSource


def html_to_text(markup: str) -> str:
    """Strip tags and entities from a short HTML snippet."""
    text = html.unescape(TAG_PATTERN.sub(" ", markup))
    return WHITESPACE.sub(" ", text).strip()


def canonical_url(url: str) -> str:
    """Drop tracking parameters and fragments so the same story dedupes."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    return urlunsplit((parts.scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def parse_feed_time(entry: feedparser.FeedParserDict) -> datetime | None:
    """Publish (or update) time of a feed entry in UTC, if the feed gives one."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed is None:
        return None
    return datetime.fromtimestamp(calendar.timegm(parsed), tz=UTC)


def parse_feed(content: bytes, source: NewsSource, since: datetime) -> list[Candidate]:
    """Parse feed bytes into candidates published at or after `since`.

    Raises:
        FeedError: if the content is not a feed.
    """
    feed = feedparser.parse(content)
    if feed.bozo and not feed.entries:
        raise FeedError(f"not a valid feed: {feed.get('bozo_exception')}")
    candidates = []
    undated = 0
    for entry in feed.entries:
        published = parse_feed_time(entry)
        link = entry.get("link")
        title = html_to_text(entry.get("title", ""))
        if published is None:
            undated += 1
            continue
        if published < since or not link or not title:
            continue
        candidates.append(
            Candidate(
                title=title,
                url=canonical_url(link),
                summary=html_to_text(entry.get("summary", "")),
                published=published,
                source=source,
            )
        )
    if undated:
        logger.warning("%s: skipped %d entries without a publish date", source.name, undated)
    return candidates


def fetch_feed(client: httpx.Client, source: NewsSource, since: datetime) -> list[Candidate]:
    """Download and parse one feed.

    Raises:
        FeedError: on HTTP errors or unparsable content.
    """
    try:
        response = client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise FeedError(f"could not fetch {source.url}: {error}") from error
    return parse_feed(response.content, source, since)


def dedupe(candidates: list[Candidate]) -> list[Candidate]:
    """Keep the first candidate per URL and per identical headline."""
    seen_urls: set[str] = set()
    seen_titles: set[str] = set()
    unique = []
    for candidate in candidates:
        title_key = candidate.title.casefold()
        if candidate.url in seen_urls or title_key in seen_titles:
            continue
        seen_urls.add(candidate.url)
        seen_titles.add(title_key)
        unique.append(candidate)
    return unique
