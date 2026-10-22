"""Download a dataset, draw a preliminary chart, and discard the data.

The raw file lives only inside a TemporaryDirectory that is removed before
build_chart returns; only the PNG bytes leave this module.
"""

import csv
import io
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import httpx
import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt
import pandas as pd

from news_aggregator.datasets import FOCUS_GEO_CODES, Dataset, Resource

logger = logging.getLogger(__name__)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_ROWS = 500_000
MAX_SERIES = 4
MAX_BARS = 12
LINE_WINDOW = pd.DateOffset(years=10)
CHARTABLE_FORMATS = ("CSV", "XLSX")

VALUE_NAMES = re.compile(r"^(obs_value|value|values|wert|anzahl|count|amount|total)$", re.I)
TIME_NAMES = re.compile(r"date|time|year|period|month|jahr|datum|monat|stichtag|zeit", re.I)
NOT_VALUE_NAMES = re.compile(
    r"lat|lon|lng|(^|_)id$|id$|code|type|year|month|day|number|^no$|zip|plz", re.I
)
QUARTER = re.compile(r"^(\d{4})-?Q([1-4])$")
SERIES_NAMES = re.compile(r"^(geo|country|countries|region|land|bundesland|state|admin1)", re.I)

# Reference categorical palette (light mode), slots in fixed order.
SERIES_COLORS = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100")
SURFACE = "#fcfcfb"
TEXT_PRIMARY = "#0b0b0b"
TEXT_SECONDARY = "#52514e"
GRID = "#e4e3df"


class ChartError(Exception):
    """A resource could not be turned into a chart."""


@dataclass(frozen=True)
class Chart:
    png: bytes
    caption: str


@dataclass(frozen=True)
class ChartSpec:
    kind: Literal["line", "bar"]
    x: str
    value: str | None
    series: str | None = None
    fixed: dict[str, str] = field(default_factory=dict)


def chartable_resources(resources: tuple[Resource, ...]) -> list[Resource]:
    """Resources pandas can read, CSV first, skipping any known to be too large."""
    usable = [
        r
        for r in resources
        if r.format in CHARTABLE_FORMATS and (r.size is None or r.size <= MAX_DOWNLOAD_BYTES)
    ]
    return sorted(usable, key=lambda r: CHARTABLE_FORMATS.index(r.format))


def download(client: httpx.Client, resource: Resource, directory: Path) -> Path:
    """Stream a resource to disk, aborting past MAX_DOWNLOAD_BYTES."""
    path = directory / f"data.{resource.format.lower()}"
    try:
        with client.stream("GET", resource.url) as response:
            response.raise_for_status()
            declared = int(response.headers.get("content-length") or 0)
            if declared > MAX_DOWNLOAD_BYTES:
                raise ChartError(f"file is {declared} bytes, limit {MAX_DOWNLOAD_BYTES}")
            received = 0
            with path.open("wb") as handle:
                for chunk in response.iter_bytes():
                    received += len(chunk)
                    if received > MAX_DOWNLOAD_BYTES:
                        raise ChartError(f"file exceeds {MAX_DOWNLOAD_BYTES} bytes")
                    handle.write(chunk)
    except httpx.HTTPError as error:
        raise ChartError(f"download failed: {error}") from error
    return path


def _read_csv(path: Path) -> pd.DataFrame:
    raw = path.read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    decimal = "," if delimiter == ";" else "."
    return pd.read_csv(io.StringIO(text), sep=delimiter, decimal=decimal, nrows=MAX_ROWS)


def load_table(path: Path, file_format: str) -> pd.DataFrame:
    """Read a CSV or XLSX file into a DataFrame."""
    try:
        frame = _read_csv(path) if file_format == "CSV" else pd.read_excel(path, nrows=MAX_ROWS)
    except (ValueError, pd.errors.ParserError, OSError) as error:
        raise ChartError(f"could not parse {file_format}: {error}") from error
    if frame.empty:
        raise ChartError("table is empty")
    return frame


def _mostly(parsed: pd.Series, original: pd.Series) -> bool:
    return bool(original.notna().sum()) and parsed.notna().sum() >= 0.8 * original.notna().sum()


def _quarter_start(match: re.Match[str]) -> str:
    year, quarter = match.groups()
    return f"{year}-{(int(quarter) - 1) * 3 + 1:02d}"


def as_time(column: pd.Series) -> pd.Series | None:
    """Parse a column as datetimes (years, dates, 2024-Q1 quarters), or None."""
    if pd.api.types.is_numeric_dtype(column):
        if not column.dropna().between(1800, 2100).all():
            return None
        parsed = pd.to_datetime(column.astype("Int64").astype(str), format="%Y", errors="coerce")
    else:
        text = column.astype(str).str.replace(QUARTER, _quarter_start, regex=True)
        parsed = pd.to_datetime(text, errors="coerce", format="mixed")
    return parsed if _mostly(parsed, column) else None


def as_number(column: pd.Series) -> pd.Series | None:
    """Parse a column as numbers, or None if most values aren't numeric."""
    parsed = pd.to_numeric(column, errors="coerce")
    return parsed if _mostly(parsed, column) else None


def _find_time(frame: pd.DataFrame) -> str | None:
    """The time-like column with the most distinct values (so "last update" stamps lose)."""
    best, best_count = None, 1
    for name in frame.columns:
        if not TIME_NAMES.search(str(name)):
            continue
        parsed = as_time(frame[name])
        count = parsed.nunique() if parsed is not None else 0
        if count > best_count:
            best, best_count = name, count
    return best


def _find_value(frame: pd.DataFrame, exclude: set[str]) -> str | None:
    candidates = [c for c in frame.columns if c not in exclude and as_number(frame[c]) is not None]
    named = [c for c in candidates if VALUE_NAMES.match(str(c))]
    if named:
        return named[0]
    unnamed = [
        c for c in candidates if frame[c].nunique() > 1 and not NOT_VALUE_NAMES.search(str(c))
    ]
    return unnamed[0] if unnamed else None


def _categorical(frame: pd.DataFrame, exclude: set[str], max_unique: int) -> list[str]:
    """Fully populated text columns whose values repeat, e.g. dimensions like "unit"."""
    return [
        c
        for c in frame.columns
        if c not in exclude
        and not pd.api.types.is_numeric_dtype(frame[c])
        and frame[c].notna().all()
        and 1 < frame[c].nunique() <= min(max_unique, len(frame) // 2)
    ]


def choose_spec(frame: pd.DataFrame) -> ChartSpec:
    """Decide what to plot: a time series if there's a time column, else a ranked bar chart."""
    time_col = _find_time(frame)
    value_col = _find_value(frame, {time_col} if time_col else set())
    used = {c for c in (time_col, value_col) if c}
    series = [c for c in _categorical(frame, used, 60) if SERIES_NAMES.match(str(c))]
    series_col = series[0] if series else None
    if series_col:
        used.add(series_col)
    fixed = {c: str(frame[c].mode().iloc[0]) for c in _categorical(frame, used, 50)}
    if time_col:
        return ChartSpec("line", time_col, value_col, series_col, fixed)
    if value_col is None:
        raise ChartError("no time column and no numeric column to plot")
    bars = series_col or next(iter(_categorical(frame, used, 500)), None)
    if bars is None:
        raise ChartError("no category column for a bar chart")
    fixed.pop(bars, None)
    return ChartSpec("bar", bars, value_col, None, fixed)


def _slice(frame: pd.DataFrame, spec: ChartSpec) -> pd.DataFrame:
    for column, value in spec.fixed.items():
        frame = frame[frame[column].astype(str) == value]
    return frame


def _top_series(frame: pd.DataFrame, column: str) -> list[str]:
    present = frame[column].astype(str)
    focus = [code for code in FOCUS_GEO_CODES if (present == code).any()]
    ranked = focus or list(present.value_counts().index)
    return ranked[:MAX_SERIES]


def line_table(frame: pd.DataFrame, spec: ChartSpec) -> pd.DataFrame:
    """Rows = time, columns = series (or one "value" column), summed per cell."""
    data = _slice(frame, spec).copy()
    data["_x"] = as_time(data[spec.x])
    data["_y"] = as_number(data[spec.value]) if spec.value else 1
    if spec.value is None:
        data["_x"] = data["_x"].dt.to_period("M").dt.to_timestamp()
    data = data.dropna(subset=["_x", "_y"])
    data = data[data["_x"] >= data["_x"].max() - LINE_WINDOW]
    if spec.series:
        keep = _top_series(data, spec.series)
        data = data[data[spec.series].astype(str).isin(keep)]
        table = data.pivot_table(index="_x", columns=spec.series, values="_y", aggfunc="sum")
        return table[[c for c in keep if c in table.columns]]
    return data.groupby("_x")[["_y"]].sum().rename(columns={"_y": spec.value or "rows"})


def bar_table(frame: pd.DataFrame, spec: ChartSpec) -> pd.Series:
    """Top categories by summed value, largest last (so it draws at the top)."""
    data = _slice(frame, spec)
    values = as_number(data[spec.value]) if spec.value else None
    if values is None:
        raise ChartError("value column has no numbers after slicing")
    totals = values.groupby(data[spec.x].astype(str)).sum()
    return totals.nlargest(MAX_BARS).sort_values()


def _style(figure: plt.Figure, axes: plt.Axes, title: str, subtitle: str) -> None:
    figure.suptitle(title, x=0.01, ha="left", fontsize=11, color=TEXT_PRIMARY, fontweight="bold")
    axes.set_title(subtitle, loc="left", fontsize=8, color=TEXT_SECONDARY)
    axes.set_facecolor(SURFACE)
    axes.grid(axis="y", color=GRID, linewidth=1)
    axes.set_axisbelow(True)
    for side in ("top", "right", "left"):
        axes.spines[side].set_visible(False)
    axes.spines["bottom"].set_color(GRID)
    axes.tick_params(colors=TEXT_SECONDARY, labelsize=9, length=0)


def _draw_line(axes: plt.Axes, table: pd.DataFrame) -> None:
    for color, column in zip(SERIES_COLORS, table.columns, strict=False):
        series = table[column].dropna()
        axes.plot(series.index, series.values, color=color, linewidth=2, label=str(column))
        axes.plot(
            series.index[-1:],
            series.values[-1:],
            "o",
            color=color,
            markersize=8,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
        )
    if len(table.columns) > 1:
        axes.legend(frameon=False, fontsize=9, labelcolor=TEXT_SECONDARY, loc="upper left")


def _draw_bar(axes: plt.Axes, totals: pd.Series) -> None:
    labels = [label if len(label) <= 32 else f"{label[:31]}…" for label in totals.index]
    bars = axes.barh(labels, totals.values, color=SERIES_COLORS[0], height=0.6)
    axes.bar_label(bars, fmt="{:,.0f}", padding=4, fontsize=8, color=TEXT_SECONDARY)
    axes.grid(axis="y", visible=False)
    axes.grid(axis="x", color=GRID, linewidth=1)


def _describe(spec: ChartSpec, columns: list[str]) -> str:
    measure = f"Sum of “{spec.value}”" if spec.value else "Number of rows"
    parts = [f"{measure} by “{spec.x}”"]
    if spec.series and columns:
        parts.append(f"{spec.series}: {', '.join(columns)}")
    if spec.fixed:
        parts.append("filtered to " + ", ".join(f"{k} = {v}" for k, v in spec.fixed.items()))
    return "; ".join(parts) + "."


def _plot_data(frame: pd.DataFrame, spec: ChartSpec) -> pd.DataFrame | pd.Series:
    data = line_table(frame, spec) if spec.kind == "line" else bar_table(frame, spec)
    if len(data) < 2:
        raise ChartError("not enough data points to plot")
    distinct = data.nunique() if isinstance(data, pd.Series) else data.nunique().max()
    if distinct < 2:
        raise ChartError("every value is the same")
    return data


def plot(frame: pd.DataFrame, title: str) -> Chart:
    """Render the frame as a PNG chart with a caption saying what was plotted.

    A time series whose chosen slice is empty or flat falls back to counting rows per month.
    """
    spec = choose_spec(frame)
    try:
        data = _plot_data(frame, spec)
    except ChartError:
        if spec.kind != "line" or spec.value is None:
            raise
        spec = ChartSpec("line", spec.x, None)
        data = _plot_data(frame, spec)
    columns = [str(c) for c in data.columns] if isinstance(data, pd.DataFrame) else []
    caption = _describe(spec, columns)
    figure, axes = plt.subplots(figsize=(7, 3.6), dpi=144, facecolor=SURFACE)
    try:
        if isinstance(data, pd.DataFrame):
            _draw_line(axes, data)
        else:
            _draw_bar(axes, data)
        short_title = title if len(title) <= 72 else f"{title[:71]}…"
        subtitle = ", ".join(f"{k} = {v}" for k, v in spec.fixed.items())
        _style(figure, axes, short_title, subtitle[:120])
        figure.tight_layout()
        buffer = io.BytesIO()
        figure.savefig(buffer, format="png", facecolor=SURFACE)
    finally:
        plt.close(figure)
    return Chart(png=buffer.getvalue(), caption=caption)


def _chart_from_resource(client: httpx.Client, resource: Resource, title: str) -> Chart:
    with tempfile.TemporaryDirectory(prefix="news-aggregator-") as directory:
        path = download(client, resource, Path(directory))
        return plot(load_table(path, resource.format), title)


def build_chart(client: httpx.Client, dataset: Dataset) -> Chart | None:
    """Chart the first downloadable resource that yields a plot; None if none do."""
    for resource in chartable_resources(dataset.resources):
        try:
            return _chart_from_resource(client, resource, dataset.title)
        except ChartError as error:
            logger.info("no chart for %s from %s: %s", dataset.title, resource.url, error)
    return None
