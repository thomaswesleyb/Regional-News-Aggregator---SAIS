"""Turn configured sources into a Digest: fetch, score, scrape, chart."""

import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.utils import make_msgid

import httpx

from news_aggregator.charts import build_chart
from news_aggregator.config import Config
from news_aggregator.datasets import Dataset, DatasetSourceError, fetch_datasets
from news_aggregator.digest import Digest, DigestArticle, DigestDataset
from news_aggregator.feeds import Candidate, FeedError, dedupe, fetch_feed
from news_aggregator.relevance import Match, Scorer
from news_aggregator.scrape import fetch_lead_paragraph

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (compatible; news-aggregator/0.1; "
    "+https://github.com/thomaswesleyb/Regional-News-Aggregator---SAIS)"
)


class AllSourcesFailedError(Exception):
    """No news source could be read, so there is nothing to send."""


@dataclass(frozen=True)
class DigestBuild:
    """A digest plus the chart images its HTML references by content ID."""

    digest: Digest
    images: dict[str, bytes]


def http_client() -> httpx.Client:
    """HTTP client with a browser-compatible User-Agent (some feeds return nothing otherwise)."""
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30.0, follow_redirects=True)


def collect_candidates(
    client: httpx.Client, config: Config, since: datetime, failures: list[str]
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for source in config.news_sources:
        try:
            candidates.extend(fetch_feed(client, source, since))
        except FeedError as error:
            logger.error("%s: %s", source.name, error)
            failures.append(f"{source.name}: {error}")
    if len(failures) == len(config.news_sources):
        raise AllSourcesFailedError(f"all {len(failures)} news sources failed: {failures}")
    return dedupe(candidates)


def select_articles(
    candidates: list[Candidate], scorer: Scorer, config: Config
) -> dict[str, list[tuple[Candidate, Match]]]:
    """Relevant candidates grouped under their strongest region, best first, capped."""
    grouped: dict[str, list[tuple[Candidate, Match]]] = defaultdict(list)
    for candidate in candidates:
        match = scorer.match(candidate.title, candidate.summary, candidate.source.regions)
        if match is not None:
            grouped[match.regions[0]].append((candidate, match))
    limit = config.keywords.scoring.max_articles_per_region
    return {
        region: sorted(items, key=lambda item: -item[1].score)[:limit]
        for region, items in grouped.items()
    }


def _digest_article(client: httpx.Client, candidate: Candidate, match: Match) -> DigestArticle:
    paragraph = fetch_lead_paragraph(client, candidate.url)
    return DigestArticle(
        title=candidate.title,
        url=candidate.url,
        paragraph=paragraph or candidate.summary,
        source=candidate.source.name,
        topics=match.topics,
        from_feed_summary=paragraph is None,
    )


def collect_datasets(
    client: httpx.Client, config: Config, since: datetime, failures: list[str]
) -> list[Dataset]:
    datasets: dict[str, Dataset] = {}
    for source in config.dataset_sources:
        try:
            found = fetch_datasets(client, source, since)
        except DatasetSourceError as error:
            logger.error("%s: %s", source.name, error)
            failures.append(f"{source.name} (datasets): {error}")
            continue
        for dataset in found:
            datasets.setdefault(dataset.url, dataset)
    return list(datasets.values())


def select_datasets(
    datasets: list[Dataset], scorer: Scorer, config: Config
) -> list[tuple[Dataset, Match]]:
    scored = []
    for dataset in datasets:
        match = scorer.match(dataset.title, dataset.description, dataset.source.regions)
        if match is not None:
            scored.append((dataset, match))
    return interleave_by_source(scored, config.keywords.scoring.max_datasets)


def interleave_by_source(
    scored: list[tuple[Dataset, Match]], limit: int
) -> list[tuple[Dataset, Match]]:
    """Best dataset from each source in turn, so one busy portal can't fill every slot."""
    queues: dict[str, list[tuple[Dataset, Match]]] = defaultdict(list)
    for item in sorted(scored, key=lambda item: -item[1].score):
        queues[item[0].source.name].append(item)
    picked: list[tuple[Dataset, Match]] = []
    while len(picked) < limit and any(queues.values()):
        for queue in queues.values():
            if queue and len(picked) < limit:
                picked.append(queue.pop(0))
    return picked


def _digest_dataset(
    client: httpx.Client, dataset: Dataset, match: Match, images: dict[str, bytes]
) -> DigestDataset:
    chart = build_chart(client, dataset)
    cid = None
    if chart is not None:
        cid = make_msgid(domain="news-aggregator")[1:-1]
        images[cid] = chart.png
    return DigestDataset(
        title=dataset.title,
        url=dataset.url,
        publisher=dataset.publisher,
        source=dataset.source.name,
        regions=match.regions,
        topics=match.topics,
        chart_cid=cid,
        chart_caption=chart.caption if chart else None,
    )


def build_digest(config: Config, client: httpx.Client, now: datetime) -> DigestBuild:
    """Run the whole pipeline for the lookback window ending at `now`.

    Raises:
        AllSourcesFailedError: if no news feed could be read.
    """
    since = now - timedelta(hours=config.keywords.scoring.lookback_hours)
    scorer = Scorer(config.keywords)
    failures: list[str] = []

    candidates = collect_candidates(client, config, since, failures)
    selected = select_articles(candidates, scorer, config)
    logger.info("%d candidates, %d relevant", len(candidates), sum(map(len, selected.values())))
    sections = tuple(
        (region, tuple(_digest_article(client, c, m) for c, m in selected[region]))
        for region in config.keywords.regions
        if region in selected
    )

    images: dict[str, bytes] = {}
    relevant = select_datasets(collect_datasets(client, config, since, failures), scorer, config)
    datasets = tuple(_digest_dataset(client, d, m, images) for d, m in relevant)
    return DigestBuild(Digest(now.date(), sections, datasets, tuple(failures)), images)
