"""The digest data model and its HTML / plain-text rendering."""

from dataclasses import dataclass
from datetime import date

from jinja2 import Environment, PackageLoader, StrictUndefined, select_autoescape

_environment = Environment(
    loader=PackageLoader("news_aggregator", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)


@dataclass(frozen=True)
class DigestArticle:
    title: str
    url: str
    paragraph: str
    source: str
    topics: tuple[str, ...]
    from_feed_summary: bool


@dataclass(frozen=True)
class DigestDataset:
    title: str
    url: str
    publisher: str
    source: str
    regions: tuple[str, ...]
    topics: tuple[str, ...]
    chart_cid: str | None = None
    chart_caption: str | None = None


@dataclass(frozen=True)
class Digest:
    day: date
    sections: tuple[tuple[str, tuple[DigestArticle, ...]], ...]
    datasets: tuple[DigestDataset, ...]
    failures: tuple[str, ...]

    @property
    def article_count(self) -> int:
        return sum(len(articles) for _, articles in self.sections)

    @property
    def subject(self) -> str:
        return (
            f"Regional digest {self.day:%d %b %Y}: "
            f"{self.article_count} articles, {len(self.datasets)} datasets"
        )


def render_html(digest: Digest) -> str:
    """Render the email body as HTML; charts are referenced as cid: images."""
    return _environment.get_template("digest.html.j2").render(digest=digest)


def render_text(digest: Digest) -> str:
    """Render a plain-text alternative for mail clients that don't show HTML."""
    lines = [digest.subject, ""]
    for region, articles in digest.sections:
        lines += [region.title().upper(), ""]
        for article in articles:
            lines += [
                f"* {article.title} ({article.source}; {', '.join(article.topics)})",
                f"  {article.paragraph}",
                f"  {article.url}",
                "",
            ]
    if digest.datasets:
        lines += ["NEW DATASETS", ""]
        for dataset in digest.datasets:
            lines += [f"* {dataset.title} ({dataset.publisher})", f"  {dataset.url}", ""]
    if digest.failures:
        lines += ["Sources that failed today:"] + [f"- {failure}" for failure in digest.failures]
    return "\n".join(lines)
