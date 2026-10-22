from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from news_aggregator.config import Keywords, NewsSource, Scoring

KEYWORDS_TOML = """
exclude = ["football"]

[scoring]
threshold = 4
title_multiplier = 2
lookback_hours = 26
max_articles_per_region = 2
max_datasets = 3

[regions.germany]
"German*" = 3
"Berlin" = 2

[regions.balkans]
"Serb*" = 3
"Western Balkans" = 3

[topics.security]
"NATO" = 3
"military" = 2

[topics.economy]
"inflation" = 3
"econom*" = 2
"""

NEWS_SOURCES_TOML = """
[[source]]
name = "Wire"
url = "https://wire.example/feed"

[[source]]
name = "Balkan Desk"
url = "https://balkans.example/feed"
regions = ["balkans"]
"""

DATASET_SOURCES_TOML = """
[[source]]
name = "Portal"
kind = "ckan"
url = "https://portal.example"
queries = ["Germany"]
"""

RECIPIENTS_TOML = """
recipients = ["reader@example.com"]
"""


@pytest.fixture
def keywords() -> Keywords:
    return Keywords(
        scoring=Scoring(
            threshold=4,
            title_multiplier=2,
            lookback_hours=26,
            max_articles_per_region=2,
            max_datasets=3,
        ),
        regions={"germany": {"German*": 3, "Berlin": 2}, "balkans": {"Serb*": 3}},
        topics={"security": {"NATO": 3, "military": 2}, "economy": {"inflation": 3}},
        exclude=("football",),
    )


@pytest.fixture
def news_source() -> NewsSource:
    return NewsSource(name="Wire", url="https://wire.example/feed")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    files = {
        "keywords.toml": KEYWORDS_TOML,
        "news_sources.toml": NEWS_SOURCES_TOML,
        "dataset_sources.toml": DATASET_SOURCES_TOML,
        "recipients.toml": RECIPIENTS_TOML,
    }
    for name, content in files.items():
        (tmp_path / name).write_text(content, encoding="utf-8")
    return tmp_path


MockClientFactory = Callable[[Callable[[httpx.Request], httpx.Response]], httpx.Client]


@pytest.fixture
def mock_client() -> MockClientFactory:
    """Build an httpx client whose requests are answered by `handler`."""

    def factory(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    return factory
