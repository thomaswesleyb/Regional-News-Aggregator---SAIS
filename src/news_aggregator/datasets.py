"""Find newly released datasets on open-data portals."""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import feedparser
import httpx

from news_aggregator.config import DatasetSource
from news_aggregator.feeds import html_to_text, parse_feed_time

logger = logging.getLogger(__name__)

# ISO codes Eurostat uses for the countries this digest follows (Eurostat writes Kosovo as XK).
FOCUS_GEO_CODES = ("DE", "RS", "BA", "ME", "MK", "AL", "XK", "HR", "SI", "RU")
EU_FOCUS_GEO_CODES = ("DE", "HR", "SI")
TABULAR_FORMATS = ("CSV", "XLSX", "XLS")
EUROSTAT_DATA_URL = "https://ec.europa.eu/eurostat/api/dissemination"
CKAN_ROWS = 100


class DatasetSourceError(Exception):
    """A dataset portal could not be queried."""


@dataclass(frozen=True)
class Resource:
    url: str
    format: str
    size: int | None = None


@dataclass(frozen=True)
class Dataset:
    title: str
    url: str
    description: str
    publisher: str
    released: datetime
    source: DatasetSource
    resources: tuple[Resource, ...]


def normalize_format(raw: str | None, url: str) -> str:
    """Map portal format labels (incl. EU file-type URIs) to e.g. "CSV"."""
    label = (raw or "").rstrip("/").rsplit("/", 1)[-1].upper()
    if label:
        return label
    suffix = url.split("?", 1)[0].rsplit(".", 1)[-1].upper()
    return suffix if suffix in TABULAR_FORMATS else ""


def parse_date(value: str) -> datetime | None:
    """Parse an ISO date/datetime from a portal, or None for values like a bare "2028"."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _get_json(client: httpx.Client, url: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        response = client.get(url, params=params)
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as error:
        raise DatasetSourceError(f"query to {url} failed: {error}") from error


def _ckan_dataset(package: dict[str, Any], source: DatasetSource) -> Dataset | None:
    resources = tuple(
        Resource(
            url=r["url"],
            format=normalize_format(r.get("format"), r["url"]),
            size=r.get("size") if isinstance(r.get("size"), int) else None,
        )
        for r in package.get("resources", [])
        if r.get("url")
    )
    organization = package.get("organization") or {}
    page = source.page_url or f"{source.url.rstrip('/')}/dataset/{{name}}"
    released = parse_date(package.get("metadata_created") or "")
    if released is None:
        logger.warning("%s: skipping %s, unreadable creation date", source.name, package["name"])
        return None
    return Dataset(
        title=package.get("title") or package["name"],
        url=page.format(name=quote(package["name"])),
        description=html_to_text(package.get("notes") or ""),
        publisher=organization.get("title") or source.name,
        released=released,
        source=source,
        resources=resources,
    )


def fetch_ckan(client: httpx.Client, source: DatasetSource, since: datetime) -> list[Dataset]:
    """Datasets created since `since` matching any of the source's queries."""
    endpoint = f"{source.url.rstrip('/')}/api/3/action/package_search"
    window = f"metadata_created:[{since.astimezone(UTC):%Y-%m-%dT%H:%M:%SZ} TO NOW]"
    datasets = []
    for query in source.queries:
        params = {"q": query, "fq": window, "sort": "metadata_created desc", "rows": CKAN_ROWS}
        payload = _get_json(client, endpoint, params)
        for package in payload["result"]["results"]:
            dataset = _ckan_dataset(package, source)
            if dataset is not None:
                datasets.append(dataset)
    return datasets


def _pick_language(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value:
        return value.get("en") or next(iter(value.values()))
    return ""


def _europa_dataset(item: dict[str, Any], source: DatasetSource, released: datetime) -> Dataset:
    resources = []
    for distribution in item.get("distributions", []):
        urls = distribution.get("download_url") or distribution.get("access_url") or []
        if not urls:
            continue
        fmt = distribution.get("format") or {}
        resources.append(
            Resource(
                url=urls[0],
                format=normalize_format(fmt.get("id"), urls[0]),
                size=distribution.get("byte_size"),
            )
        )
    publisher = _pick_language((item.get("publisher") or {}).get("name"))
    catalog = _pick_language((item.get("catalog") or {}).get("title"))
    return Dataset(
        title=_pick_language(item.get("title")) or item["id"],
        url=f"https://data.europa.eu/data/datasets/{quote(item['id'])}",
        description=html_to_text(_pick_language(item.get("description"))),
        publisher=publisher or catalog or source.name,
        released=released,
        source=source,
        resources=tuple(resources),
    )


def fetch_europa(client: httpx.Client, source: DatasetSource, since: datetime) -> list[Dataset]:
    """Datasets issued since `since` on data.europa.eu matching the source's queries."""
    datasets = []
    for query in source.queries:
        params = {"q": query, "filter": "dataset", "sort": "issued+desc", "limit": 100}
        payload = _get_json(client, source.url, params)
        for item in payload["result"]["results"]:
            released = parse_date(item.get("issued") or "")
            if released is not None and released >= since:
                datasets.append(_europa_dataset(item, source, released))
    return datasets


def eurostat_resources(code: str) -> tuple[Resource, ...]:
    """CSV downloads for a Eurostat dataset, smallest first.

    Eurostat rejects a filter naming any country the dataset doesn't cover, so try all
    focus countries, then EU members only, then the full table.
    """
    filtered = [
        f"{EUROSTAT_DATA_URL}/sdmx/3.0/data/dataflow/ESTAT/{code}/1.0/*"
        f"?c[geo]={','.join(geos)}&format=csvdata&formatVersion=2.0&compress=false"
        for geos in (FOCUS_GEO_CODES, EU_FOCUS_GEO_CODES)
    ]
    full = f"{EUROSTAT_DATA_URL}/sdmx/2.1/data/{code}?format=SDMX-CSV&compressed=false"
    return tuple(Resource(url, "CSV") for url in [*filtered, full])


def fetch_eurostat(client: httpx.Client, source: DatasetSource, since: datetime) -> list[Dataset]:
    """Datasets whose data Eurostat updated since `since`, one entry per dataset code."""
    try:
        response = client.get(source.url)
        response.raise_for_status()
    except httpx.HTTPError as error:
        raise DatasetSourceError(f"could not fetch {source.url}: {error}") from error
    feed = feedparser.parse(response.content)
    datasets: dict[str, Dataset] = {}
    for entry in feed.entries:
        if "data" not in entry.get("category", "").lower():
            continue
        released = parse_feed_time(entry)
        code = entry.get("title", "").split(" ", 1)[0]
        if released is None or released < since or not code or code in datasets:
            continue
        datasets[code] = Dataset(
            title=f"{html_to_text(entry.get('summary', ''))} ({code})",
            url=entry.get("link", ""),
            description=html_to_text(entry.get("summary", "")),
            publisher="Eurostat",
            released=released,
            source=source,
            resources=eurostat_resources(code),
        )
    return list(datasets.values())


def fetch_datasets(client: httpx.Client, source: DatasetSource, since: datetime) -> list[Dataset]:
    """Dispatch to the adapter for the source's kind.

    Raises:
        DatasetSourceError: if the portal can't be queried or returns unexpected data.
    """
    adapters = {"ckan": fetch_ckan, "europa": fetch_europa, "eurostat": fetch_eurostat}
    try:
        return adapters[source.kind](client, source, since)
    except (KeyError, TypeError, ValueError) as error:
        raise DatasetSourceError(f"unexpected response shape: {error!r}") from error
