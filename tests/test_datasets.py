from datetime import UTC, datetime

import httpx
import pytest
from conftest import MockClientFactory

from news_aggregator.config import DatasetSource
from news_aggregator.datasets import (
    DatasetSourceError,
    fetch_datasets,
    normalize_format,
    parse_date,
)

SINCE = datetime(2026, 10, 21, 12, tzinfo=UTC)
CKAN = DatasetSource(
    name="Portal", kind="ckan", url="https://portal.example", queries=("Serbia", "Kosovo")
)


def _package(name: str, created: str = "2026-10-22T08:00:00.123") -> dict:
    return {
        "name": name,
        "title": f"Title {name}",
        "notes": "<p>Conflict <b>events</b></p>",
        "metadata_created": created,
        "organization": {"title": "ACLED"},
        "resources": [
            {"url": "https://portal.example/d.csv", "format": "CSV", "size": 100},
            {"url": "https://portal.example/d.json", "format": "JSON", "size": None},
            {"format": "CSV"},
        ],
    }


def test_ckan_queries_each_term(mock_client: MockClientFactory) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        name = request.url.params["q"].lower()
        return httpx.Response(200, json={"result": {"results": [_package(name)]}})

    datasets = fetch_datasets(mock_client(handler), CKAN, SINCE)
    assert [d.title for d in datasets] == ["Title serbia", "Title kosovo"]
    assert seen[0].url.path == "/api/3/action/package_search"
    assert seen[0].url.params["fq"] == "metadata_created:[2026-10-21T12:00:00Z TO NOW]"
    first = datasets[0]
    assert first.url == "https://portal.example/dataset/serbia"
    assert first.description == "Conflict events"
    assert first.publisher == "ACLED"
    assert first.released == datetime(2026, 10, 22, 8, 0, 0, 123000, tzinfo=UTC)
    assert [(r.format, r.size) for r in first.resources] == [("CSV", 100), ("JSON", None)]


def test_ckan_custom_page_url_and_bad_date(mock_client: MockClientFactory) -> None:
    source = DatasetSource(
        name="GovData",
        kind="ckan",
        url="https://gov.example/ckan",
        queries=("x",),
        page_url="https://gov.example/suche/{name}",
    )
    packages = [_package("good"), _package("bad", created="sometime")]
    client = mock_client(lambda r: httpx.Response(200, json={"result": {"results": packages}}))
    datasets = fetch_datasets(client, source, SINCE)
    assert [d.url for d in datasets] == ["https://gov.example/suche/good"]


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(500),
        httpx.Response(200, text="<html>maintenance</html>"),
        httpx.Response(200, json={"unexpected": True}),
    ],
)
def test_ckan_errors(mock_client: MockClientFactory, response: httpx.Response) -> None:
    with pytest.raises(DatasetSourceError):
        fetch_datasets(mock_client(lambda request: response), CKAN, SINCE)


def test_europa_filters_by_issued(mock_client: MockClientFactory) -> None:
    source = DatasetSource(
        name="EU", kind="europa", url="https://eu.example/search", queries=("q",)
    )
    items = [
        {
            "id": "new-1",
            "title": {"de": "Haushalt", "en": "Budget"},
            "description": {"sr": "Opis"},
            "issued": "2026-10-22T09:00:00",
            "publisher": {"name": "Statistik Amt"},
            "distributions": [
                {"download_url": ["https://eu.example/a.csv"], "format": {"id": "CSV"}},
                {"access_url": ["https://eu.example/page"], "format": {"id": "HTML"}},
                {"format": {"id": "CSV"}},
            ],
        },
        {"id": "old", "title": {"en": "Old"}, "issued": "2025-01-01"},
        {"id": "year-only", "title": {"en": "Year"}, "issued": "2028"},
    ]
    client = mock_client(lambda r: httpx.Response(200, json={"result": {"results": items}}))
    datasets = fetch_datasets(client, source, SINCE)
    assert len(datasets) == 1
    dataset = datasets[0]
    assert dataset.title == "Budget"
    assert dataset.description == "Opis"
    assert dataset.publisher == "Statistik Amt"
    assert dataset.url == "https://data.europa.eu/data/datasets/new-1"
    assert [r.format for r in dataset.resources] == ["CSV", "HTML"]


EUROSTAT_RSS = b"""<?xml version='1.0' encoding='UTF-8'?><rss version="2.0"><channel>
<title>ESTAT</title>
<item><title>EI_ISBR_M - "Dataset: updated data"</title><category>UPDATED_DATASET_DATA</category>
<pubDate>2026-10-22 11:00:00.0</pubDate>
<link>https://ec.europa.eu/eurostat/databrowser/product/page/EI_ISBR_M</link>
<description>Construction - monthly data</description></item>
<item><title>EI_ISBR_M - "Dataset: updated data"</title><category>UPDATED_DATASET_DATA</category>
<pubDate>2026-10-22 11:00:00.0</pubDate><link>x</link><description>dup</description></item>
<item><title>NAMA_10 - "Data structure updated"</title><category>UPDATED_DSD</category>
<pubDate>2026-10-22 11:00:00.0</pubDate><link>y</link><description>Structure</description></item>
<item><title>OLD_1 - "Dataset: updated data"</title><category>UPDATED_DATASET_DATA</category>
<pubDate>2026-10-01 11:00:00.0</pubDate><link>z</link><description>Old</description></item>
</channel></rss>"""


def test_eurostat_feed(mock_client: MockClientFactory) -> None:
    source = DatasetSource(name="Eurostat", kind="eurostat", url="https://e.example/rss")
    datasets = fetch_datasets(
        mock_client(lambda r: httpx.Response(200, content=EUROSTAT_RSS)), source, SINCE
    )
    assert len(datasets) == 1
    dataset = datasets[0]
    assert dataset.title == "Construction - monthly data (EI_ISBR_M)"
    assert dataset.publisher == "Eurostat"
    assert len(dataset.resources) == 3
    assert "c[geo]=DE,RS," in dataset.resources[0].url
    assert "c[geo]=DE,HR,SI&" in dataset.resources[1].url
    assert dataset.resources[2].url.endswith(
        "/sdmx/2.1/data/EI_ISBR_M?format=SDMX-CSV&compressed=false"
    )


def test_eurostat_http_error(mock_client: MockClientFactory) -> None:
    source = DatasetSource(name="Eurostat", kind="eurostat", url="https://e.example/rss")
    with pytest.raises(DatasetSourceError, match="could not fetch"):
        fetch_datasets(mock_client(lambda r: httpx.Response(502)), source, SINCE)


@pytest.mark.parametrize(
    ("raw", "url", "expected"),
    [
        ("CSV", "https://x/a", "CSV"),
        ("csv", "https://x/a", "CSV"),
        ("http://publications.europa.eu/resource/authority/file-type/XLSX", "u", "XLSX"),
        (None, "https://x/data.csv?download=1", "CSV"),
        ("", "https://x/data.zip", ""),
    ],
)
def test_normalize_format(raw: str | None, url: str, expected: str) -> None:
    assert normalize_format(raw, url) == expected


def test_parse_date() -> None:
    assert parse_date("2026-10-22") == datetime(2026, 10, 22, tzinfo=UTC)
    assert parse_date("2026-10-22T10:00:00Z") == datetime(2026, 10, 22, 10, tzinfo=UTC)
    assert parse_date("2028") is None
    assert parse_date("") is None
