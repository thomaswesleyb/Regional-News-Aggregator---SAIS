from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from conftest import MockClientFactory

from news_aggregator import cli
from news_aggregator.config import DatasetSource, load_config
from news_aggregator.datasets import Dataset
from news_aggregator.pipeline import AllSourcesFailedError, build_digest, interleave_by_source
from news_aggregator.relevance import Match

NOW = datetime(2026, 10, 22, 12, tzinfo=UTC)


def _rss(*items: tuple[str, str, str]) -> str:
    body = "".join(
        f"<item><title>{title}</title><link>{link}</link><description>{text}</description>"
        "<pubDate>Thu, 22 Oct 2026 09:00:00 +0000</pubDate></item>"
        for title, link, text in items
    )
    return (
        f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{body}</channel></rss>'
    )


WIRE = _rss(
    ("German military budget", "https://wire.example/1", "Berlin approves NATO target"),
    ("German inflation eases", "https://wire.example/2", "economy"),
    ("German army reform", "https://wire.example/3", "military"),
    ("Football results", "https://wire.example/4", "German military football"),
    ("Weather", "https://wire.example/5", "Sunny"),
)
BALKANS = _rss(("NATO drills near border", "https://balkans.example/1", "Exercises"))
ARTICLE = "<html><body><article><p>{}</p></article></body></html>".format(
    "Lead paragraph text. " * 6
)
CSV = "year,value\n2020,1\n2021,3\n2022,2\n"


def _ckan_payload() -> dict:
    return {
        "result": {
            "results": [
                {
                    "name": "ger-def",
                    "title": "German military spending",
                    "notes": "",
                    "metadata_created": "2026-10-22T08:00:00",
                    "resources": [{"url": "https://portal.example/d.csv", "format": "CSV"}],
                },
                {
                    "name": "cats",
                    "title": "Cat census",
                    "notes": "",
                    "metadata_created": "2026-10-22T08:00:00",
                    "resources": [],
                },
            ]
        }
    }


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == "https://wire.example/feed":
        return httpx.Response(200, text=WIRE)
    if url == "https://balkans.example/feed":
        return httpx.Response(200, text=BALKANS)
    if request.url.path == "/api/3/action/package_search":
        return httpx.Response(200, json=_ckan_payload())
    if url == "https://portal.example/d.csv":
        return httpx.Response(200, text=CSV)
    if url == "https://wire.example/2":
        return httpx.Response(403)
    return httpx.Response(200, html=ARTICLE)


def test_build_digest_end_to_end(config_dir: Path, mock_client: MockClientFactory) -> None:
    config = load_config(config_dir)
    build = build_digest(config, mock_client(handler), NOW)
    digest = build.digest

    regions = [region for region, _ in digest.sections]
    assert regions == ["germany", "balkans"]
    germany = dict(digest.sections)["germany"]
    assert len(germany) == 2  # capped at max_articles_per_region
    assert germany[0].title == "German military budget"
    assert germany[0].paragraph.startswith("Lead paragraph text.")
    balkans = dict(digest.sections)["balkans"]
    assert balkans[0].title == "NATO drills near border"  # region from source tag

    assert [d.title for d in digest.datasets] == ["German military spending"]
    cid = digest.datasets[0].chart_cid
    assert cid is not None
    assert build.images[cid].startswith(b"\x89PNG")
    assert digest.failures == ()


def test_blocked_article_falls_back_to_feed_summary(
    config_dir: Path, mock_client: MockClientFactory
) -> None:
    digest = build_digest(load_config(config_dir), mock_client(handler), NOW).digest
    germany = dict(digest.sections)["germany"]
    assert germany[1].title == "German inflation eases"
    assert germany[1].from_feed_summary
    assert germany[1].paragraph == "economy"


def test_failures_are_reported(config_dir: Path, mock_client: MockClientFactory) -> None:
    def flaky(request: httpx.Request) -> httpx.Response:
        if request.url.host in {"balkans.example", "portal.example"}:
            return httpx.Response(500)
        return handler(request)

    digest = build_digest(load_config(config_dir), mock_client(flaky), NOW).digest
    assert len(digest.failures) == 2
    assert digest.failures[0].startswith("Balkan Desk: could not fetch")
    assert digest.failures[1].startswith("Portal (datasets):")
    assert digest.sections


def test_all_news_sources_failing_raises(config_dir: Path, mock_client: MockClientFactory) -> None:
    with pytest.raises(AllSourcesFailedError, match="all 2 news sources failed"):
        build_digest(load_config(config_dir), mock_client(lambda r: httpx.Response(500)), NOW)


def _scored(source: str, score: float) -> tuple[Dataset, Match]:
    dataset_source = DatasetSource(name=source, kind="eurostat", url="https://x")
    dataset = Dataset(
        f"{source}-{score}", f"https://{source}/{score}", "", "", NOW, dataset_source, ()
    )
    return dataset, Match(score, ("germany",), ("economy",))


def test_interleave_by_source() -> None:
    items = [_scored("A", 9), _scored("A", 8), _scored("A", 7), _scored("B", 1), _scored("C", 5)]
    picked = interleave_by_source(items, 4)
    assert [d.title for d, _ in picked] == ["A-9", "C-5", "B-1", "A-8"]
    assert interleave_by_source([], 3) == []


def test_cli_dry_run_writes_preview(
    config_dir: Path,
    tmp_path: Path,
    mock_client: MockClientFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli, "http_client", lambda: mock_client(handler))
    out = tmp_path / "out" / "digest.html"
    code = cli.main(["run", "--config-dir", str(config_dir), "--dry-run", "--out", str(out)])
    assert code == 0
    html = out.read_text()
    assert "German military budget" in html
    assert 'src="digest-chart-1.png"' in html
    assert (out.parent / "digest-chart-1.png").read_bytes().startswith(b"\x89PNG")


def test_cli_send_without_credentials_fails_fast(
    config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)

    def no_network() -> httpx.Client:
        raise AssertionError("must fail before fetching anything")

    monkeypatch.setattr(cli, "http_client", no_network)
    assert cli.main(["run", "--config-dir", str(config_dir)]) == 1


def test_cli_bad_config(tmp_path: Path) -> None:
    assert cli.main(["run", "--config-dir", str(tmp_path), "--dry-run"]) == 1
