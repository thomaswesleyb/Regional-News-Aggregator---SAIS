"""Load and validate the editable TOML files in the config directory."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast

DatasetKind = Literal["ckan", "europa", "eurostat"]
DATASET_KINDS: tuple[DatasetKind, ...] = ("ckan", "europa", "eurostat")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ConfigError(Exception):
    """A config file is missing or invalid."""


@dataclass(frozen=True)
class Scoring:
    threshold: float
    title_multiplier: float
    lookback_hours: int
    max_articles_per_region: int
    max_datasets: int


@dataclass(frozen=True)
class Keywords:
    scoring: Scoring
    regions: dict[str, dict[str, float]]
    topics: dict[str, dict[str, float]]
    exclude: tuple[str, ...]


@dataclass(frozen=True)
class NewsSource:
    name: str
    url: str
    regions: tuple[str, ...] = ()


@dataclass(frozen=True)
class DatasetSource:
    name: str
    kind: DatasetKind
    url: str
    queries: tuple[str, ...] = ()
    regions: tuple[str, ...] = ()
    page_url: str | None = None


@dataclass(frozen=True)
class Config:
    keywords: Keywords
    news_sources: tuple[NewsSource, ...]
    dataset_sources: tuple[DatasetSource, ...]
    recipients: tuple[str, ...]


class _Table:
    """A TOML table plus the file/key path it came from, for error messages."""

    def __init__(self, data: Any, where: str) -> None:
        if not isinstance(data, dict):
            raise ConfigError(f"{where}: expected a table, got {type(data).__name__}")
        self.data = cast(dict[str, Any], data)
        self.where = where

    def check_keys(self, required: set[str], optional: frozenset[str] = frozenset()) -> None:
        missing = required - self.data.keys()
        if missing:
            raise ConfigError(f"{self.where}: missing key(s) {sorted(missing)}")
        unknown = self.data.keys() - required - optional
        if unknown:
            allowed = sorted(required | optional)
            raise ConfigError(f"{self.where}: unknown key(s) {sorted(unknown)}; allowed: {allowed}")

    def number(self, key: str) -> float:
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
            raise ConfigError(f"{self.where}.{key}: expected a positive number, got {value!r}")
        return float(value)

    def integer(self, key: str, default: int | None = None) -> int:
        value = self.data.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ConfigError(f"{self.where}.{key}: expected a positive integer, got {value!r}")
        return value

    def string(self, key: str) -> str:
        value = self.data[key]
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"{self.where}.{key}: expected a non-empty string, got {value!r}")
        return value.strip()

    def strings(self, key: str, *, allow_empty: bool = True) -> tuple[str, ...]:
        value = self.data.get(key, [])
        where = f"{self.where}.{key}"
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            raise ConfigError(f"{where}: expected a list of non-empty strings, got {value!r}")
        if not value and not allow_empty:
            raise ConfigError(f"{where}: must contain at least one entry")
        return tuple(v.strip() for v in value)

    def sub(self, key: str) -> "_Table":
        return _Table(self.data[key], f"{self.where}.{key}")


def _read_toml(path: Path) -> _Table:
    if not path.is_file():
        raise ConfigError(f"{path}: file not found (expected it in the config directory)")
    try:
        with path.open("rb") as handle:
            return _Table(tomllib.load(handle), path.name)
    except tomllib.TOMLDecodeError as error:
        raise ConfigError(f"{path}: invalid TOML: {error}") from error


def _term_groups(table: _Table) -> dict[str, dict[str, float]]:
    if not table.data:
        raise ConfigError(f"{table.where}: define at least one group")
    groups: dict[str, dict[str, float]] = {}
    for name in table.data:
        terms = table.sub(name)
        if not terms.data:
            raise ConfigError(f"{terms.where}: define at least one term")
        groups[name] = {term: terms.number(term) for term in terms.data}
    return groups


def load_keywords(path: Path) -> Keywords:
    """Parse keywords.toml.

    Raises:
        ConfigError: if the file is missing, malformed, or has invalid values.
    """
    root = _read_toml(path)
    root.check_keys({"scoring", "regions", "topics"}, frozenset({"exclude"}))
    scoring = root.sub("scoring")
    scoring.check_keys(
        {"threshold", "title_multiplier", "lookback_hours"},
        frozenset({"max_articles_per_region", "max_datasets"}),
    )
    return Keywords(
        scoring=Scoring(
            threshold=scoring.number("threshold"),
            title_multiplier=scoring.number("title_multiplier"),
            lookback_hours=scoring.integer("lookback_hours"),
            max_articles_per_region=scoring.integer("max_articles_per_region", default=12),
            max_datasets=scoring.integer("max_datasets", default=8),
        ),
        regions=_term_groups(root.sub("regions")),
        topics=_term_groups(root.sub("topics")),
        exclude=root.strings("exclude"),
    )


def _check_regions(table: _Table, known: dict[str, dict[str, float]]) -> tuple[str, ...]:
    regions = table.strings("regions")
    unknown = [r for r in regions if r not in known]
    if unknown:
        raise ConfigError(
            f"{table.where}.regions: unknown region(s) {unknown}; "
            f"defined in keywords.toml: {sorted(known)}"
        )
    return regions


def _source_tables(path: Path) -> list[_Table]:
    root = _read_toml(path)
    root.check_keys({"source"})
    entries = root.data["source"]
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path.name}: define at least one [[source]] entry")
    return [_Table(entry, f"{path.name}: source #{i + 1}") for i, entry in enumerate(entries)]


def load_news_sources(path: Path, keywords: Keywords) -> tuple[NewsSource, ...]:
    """Parse news_sources.toml, checking region names against keywords."""
    sources = []
    for table in _source_tables(path):
        table.check_keys({"name", "url"}, frozenset({"regions"}))
        sources.append(
            NewsSource(
                name=table.string("name"),
                url=table.string("url"),
                regions=_check_regions(table, keywords.regions),
            )
        )
    return tuple(sources)


def load_dataset_sources(path: Path, keywords: Keywords) -> tuple[DatasetSource, ...]:
    """Parse dataset_sources.toml, checking kinds and region names."""
    sources = []
    for table in _source_tables(path):
        table.check_keys({"name", "kind", "url"}, frozenset({"queries", "regions", "page_url"}))
        kind = table.string("kind")
        if kind not in DATASET_KINDS:
            raise ConfigError(f"{table.where}.kind: {kind!r} is not one of {list(DATASET_KINDS)}")
        queries = table.strings("queries", allow_empty=kind == "eurostat")
        sources.append(
            DatasetSource(
                name=table.string("name"),
                kind=kind,
                url=table.string("url"),
                queries=queries,
                regions=_check_regions(table, keywords.regions),
                page_url=_page_url(table),
            )
        )
    return tuple(sources)


def _page_url(table: _Table) -> str | None:
    if "page_url" not in table.data:
        return None
    page_url = table.string("page_url")
    if "{name}" not in page_url:
        raise ConfigError(f"{table.where}.page_url: must contain the placeholder {{name}}")
    return page_url


def load_recipients(path: Path) -> tuple[str, ...]:
    """Parse recipients.toml, rejecting malformed addresses."""
    root = _read_toml(path)
    root.check_keys({"recipients"})
    recipients = root.strings("recipients", allow_empty=False)
    invalid = [r for r in recipients if not EMAIL_PATTERN.match(r)]
    if invalid:
        raise ConfigError(f"{root.where}.recipients: invalid email address(es) {invalid}")
    return recipients


def load_config(config_dir: Path) -> Config:
    """Load every config file from config_dir.

    Raises:
        ConfigError: naming the file and key that is wrong.
    """
    keywords = load_keywords(config_dir / "keywords.toml")
    return Config(
        keywords=keywords,
        news_sources=load_news_sources(config_dir / "news_sources.toml", keywords),
        dataset_sources=load_dataset_sources(config_dir / "dataset_sources.toml", keywords),
        recipients=load_recipients(config_dir / "recipients.toml"),
    )
