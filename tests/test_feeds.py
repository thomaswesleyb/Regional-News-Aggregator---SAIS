from datetime import UTC, datetime

import httpx
import pytest
from conftest import MockClientFactory

from news_aggregator.config import NewsSource
from news_aggregator.feeds import (
    Candidate,
    FeedError,
    canonical_url,
    dedupe,
    fetch_feed,
    html_to_text,
    parse_feed,
)

SINCE = datetime(2026, 10, 21, 12, tzinfo=UTC)

RSS = b"""<?xml version="1.0"?>
<rss version="2.0"><channel><title>Wire</title>
<item>
  <title>Germany &amp; NATO</title>
  <link>https://wire.example/a?utm_source=rss&amp;id=7#top</link>
  <description>&lt;p&gt;Berlin &lt;b&gt;boosts&lt;/b&gt; spending&lt;/p&gt;</description>
  <pubDate>Thu, 22 Oct 2026 09:00:00 +0000</pubDate>
</item>
<item>
  <title>Old story</title>
  <link>https://wire.example/old</link>
  <pubDate>Mon, 19 Oct 2026 09:00:00 +0000</pubDate>
</item>
<item>
  <title>No date</title>
  <link>https://wire.example/undated</link>
</item>
<item>
  <title></title>
  <link>https://wire.example/untitled</link>
  <pubDate>Thu, 22 Oct 2026 09:00:00 +0000</pubDate>
</item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>Atom</title>
<entry><title>Serbia votes</title><link href="https://atom.example/s"/>
<updated>2026-10-22T08:00:00Z</updated><summary>Snap election</summary></entry>
</feed>"""


def test_parse_rss_filters_and_cleans(news_source: NewsSource) -> None:
    candidates = parse_feed(RSS, news_source, SINCE)
    assert len(candidates) == 1
    item = candidates[0]
    assert item.title == "Germany & NATO"
    assert item.url == "https://wire.example/a?id=7"
    assert item.summary == "Berlin boosts spending"
    assert item.published == datetime(2026, 10, 22, 9, tzinfo=UTC)


def test_parse_atom(news_source: NewsSource) -> None:
    candidates = parse_feed(ATOM, news_source, SINCE)
    assert [c.title for c in candidates] == ["Serbia votes"]
    assert candidates[0].summary == "Snap election"


def test_rejects_non_feed(news_source: NewsSource) -> None:
    with pytest.raises(FeedError, match="not a valid feed"):
        parse_feed(b"<html><body>Access denied</body>", news_source, SINCE)


def test_fetch_http_error(news_source: NewsSource, mock_client: MockClientFactory) -> None:
    client = mock_client(lambda request: httpx.Response(503))
    with pytest.raises(FeedError, match=r"could not fetch https://wire\.example/feed"):
        fetch_feed(client, news_source, SINCE)


def test_fetch_ok(news_source: NewsSource, mock_client: MockClientFactory) -> None:
    client = mock_client(lambda request: httpx.Response(200, content=ATOM))
    assert len(fetch_feed(client, news_source, SINCE)) == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://Site.example/a?utm_medium=x&p=1#frag", "https://site.example/a?p=1"),
        ("https://site.example/a?fbclid=abc", "https://site.example/a"),
        ("https://site.example/a", "https://site.example/a"),
    ],
)
def test_canonical_url(url: str, expected: str) -> None:
    assert canonical_url(url) == expected


def test_html_to_text() -> None:
    assert html_to_text("<p>A&nbsp;<i>b</i></p>\n\n c") == "A b c"
    assert html_to_text("") == ""


def test_dedupe_by_url_and_title(news_source: NewsSource) -> None:
    def candidate(title: str, url: str) -> Candidate:
        return Candidate(title, url, "", SINCE, news_source)

    items = [
        candidate("A", "https://x/1"),
        candidate("B", "https://x/1"),
        candidate("a", "https://x/2"),
        candidate("C", "https://x/3"),
    ]
    assert [c.title for c in dedupe(items)] == ["A", "C"]
