# Regional News Aggregator

A daily email digest of **security, politics and economy** news for **Germany, the Balkans and
Russia**, plus newly released datasets on those topics with preliminary charts.

Every morning a GitHub Actions job:

1. Reads the RSS feeds in `config/news_sources.toml`.
2. Scores each article against `config/keywords.toml` and keeps the relevant ones.
3. Scrapes each kept article for its lead paragraph (falling back to the feed summary when a site
   blocks scraping or is paywalled).
4. Searches the open-data portals in `config/dataset_sources.toml` for datasets released in the
   same window and scores them with the same keywords.
5. For each relevant dataset with a downloadable CSV/XLSX file (≤ 20 MB), downloads it to a
   temporary folder, draws a chart, and deletes the folder. Only the chart image is kept.
6. Emails the digest to everyone in `config/recipients.toml`.

## Editing what you get

Everything you'd normally change lives in `config/`. Each file explains its own format at the top.

| file | what it controls |
|---|---|
| `keywords.toml` | region and topic keywords and their weights, the relevance threshold, how many items to include, excluded words |
| `news_sources.toml` | RSS feeds to read |
| `dataset_sources.toml` | data portals to watch (CKAN portals, data.europa.eu, Eurostat) |
| `recipients.toml` | who receives the email |

### How relevance works

An article is included when it matches **at least one region term and one topic term**, its
score reaches `scoring.threshold`, and its title contains no `exclude` term. The score is the sum
of the weights of each distinct term found; a term in the title counts `title_multiplier` times.
Terms match whole words, case-insensitively, and a trailing `*` matches word endings (`Serb*`
matches Serbia, Serbian, Serbs).

Sources tagged with `regions = [...]` (e.g. Balkan Insight → `balkans`) count as matching those
regions, so their articles only need a topic match. Each article appears under its strongest
region.

To tune: run a dry run (below), look at what got in or was missed, and adjust weights or the
threshold.

> **Note:** if this repository is public, the addresses in `recipients.toml` are public too.

## Setup

1. Create a Gmail app password for the sending account at
   <https://myaccount.google.com/apppasswords> (requires 2-Step Verification).
2. In the GitHub repository go to *Settings → Secrets and variables → Actions* and add:
   - `GMAIL_USER`: the sending Gmail address
   - `GMAIL_APP_PASSWORD`: the app password
3. The **Daily digest** workflow runs at 11:00 UTC. To send one immediately, open
   *Actions → Daily digest → Run workflow*.

## Running locally

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync

# Build the digest without sending: writes digest.html and its chart PNGs
uv run news-aggregator run --dry-run --out digest.html

# Build and send
GMAIL_USER=you@gmail.com GMAIL_APP_PASSWORD='xxxx xxxx xxxx xxxx' uv run news-aggregator run
```

If a feed or portal fails, the run continues and the email lists the failed sources at the
bottom. The command exits non-zero only when the config is invalid, credentials are missing,
sending fails, or every news feed fails.

## Development

```sh
uv run ruff format . && uv run ruff check . && uv run ty check && uv run pytest
prek install   # runs the same checks plus actionlint and zizmor on commit
```

Code is in `src/news_aggregator/`:

| module | role |
|---|---|
| `config.py` | loads and validates the TOML files |
| `feeds.py` | fetches and parses RSS/Atom feeds |
| `relevance.py` | keyword scoring |
| `scrape.py` | lead-paragraph extraction (trafilatura) |
| `datasets.py` | portal adapters: CKAN, data.europa.eu, Eurostat |
| `charts.py` | download → chart → delete |
| `digest.py`, `templates/` | email content and HTML template |
| `mailer.py` | Gmail SMTP |
| `pipeline.py`, `cli.py` | orchestration and command line |
