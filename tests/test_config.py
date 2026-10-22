import re
from pathlib import Path

import pytest

from news_aggregator.config import ConfigError, load_config


def test_loads_all_files(config_dir: Path) -> None:
    config = load_config(config_dir)
    assert config.keywords.scoring.threshold == 4
    assert config.keywords.regions["germany"]["German*"] == 3
    assert config.keywords.exclude == ("football",)
    assert [s.name for s in config.news_sources] == ["Wire", "Balkan Desk"]
    assert config.news_sources[1].regions == ("balkans",)
    assert config.dataset_sources[0].kind == "ckan"
    assert config.recipients == ("reader@example.com",)


def test_repository_config_is_valid() -> None:
    config = load_config(Path(__file__).parent.parent / "config")
    assert "twesleybailey@gmail.com" in config.recipients
    assert set(config.keywords.regions) == {"germany", "balkans", "russia"}
    assert set(config.keywords.topics) == {"security", "politics", "economy"}


def _replace(config_dir: Path, name: str, old: str, new: str) -> None:
    path = config_dir / name
    text = path.read_text()
    assert old in text
    path.write_text(text.replace(old, new))


@pytest.mark.parametrize(
    ("name", "old", "new", "message"),
    [
        ("keywords.toml", "threshold = 4", "threshold = 0", "threshold: expected a positive"),
        ("keywords.toml", "threshold = 4", "threshold = true", "threshold: expected a positive"),
        ("keywords.toml", "lookback_hours = 26", "lookback_hours = 2.5", "positive integer"),
        ("keywords.toml", "threshold = 4", "", "missing key(s) ['threshold']"),
        ("keywords.toml", "threshold = 4", "threshold = 4\nthreshhold = 3", "unknown key(s)"),
        ("keywords.toml", '"Berlin" = 2', '"Berlin" = -1', "Berlin: expected a positive"),
        ("keywords.toml", 'exclude = ["football"]', "exclude = [1]", "list of non-empty strings"),
        ("keywords.toml", "lookback_hours = 26", "lookback_hours = [", "invalid TOML"),
        ("news_sources.toml", 'regions = ["balkans"]', 'regions = ["mars"]', "unknown region"),
        ("news_sources.toml", 'name = "Wire"', 'name = ""', "non-empty string"),
        ("dataset_sources.toml", 'kind = "ckan"', 'kind = "ftp"', "'ftp' is not one of"),
        ("dataset_sources.toml", 'queries = ["Germany"]', "queries = []", "at least one entry"),
        (
            "dataset_sources.toml",
            'queries = ["Germany"]',
            'queries = ["Germany"]\npage_url = "https://x.example"',
            "{name}",
        ),
        ("recipients.toml", "reader@example.com", "not-an-email", "invalid email"),
        ("recipients.toml", '["reader@example.com"]', "[]", "at least one entry"),
    ],
)
def test_rejects_invalid_values(
    config_dir: Path, name: str, old: str, new: str, message: str
) -> None:
    _replace(config_dir, name, old, new)
    with pytest.raises(ConfigError, match=re.escape(message)):
        load_config(config_dir)


def test_error_names_the_file(config_dir: Path) -> None:
    _replace(config_dir, "recipients.toml", "reader@example.com", "nope")
    with pytest.raises(ConfigError, match=r"^recipients\.toml\.recipients"):
        load_config(config_dir)


def test_missing_file(config_dir: Path) -> None:
    (config_dir / "recipients.toml").unlink()
    with pytest.raises(ConfigError, match="file not found"):
        load_config(config_dir)


def test_empty_region_group(config_dir: Path) -> None:
    _replace(config_dir, "keywords.toml", '"Serb*" = 3\n"Western Balkans" = 3\n', "")
    with pytest.raises(ConfigError, match=r"regions\.balkans: define at least one term"):
        load_config(config_dir)


def test_eurostat_needs_no_queries(config_dir: Path) -> None:
    (config_dir / "dataset_sources.toml").write_text(
        '[[source]]\nname = "Eurostat"\nkind = "eurostat"\nurl = "https://e.example/rss"\n'
    )
    assert load_config(config_dir).dataset_sources[0].queries == ()
