from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd
import pytest
from conftest import MockClientFactory

from news_aggregator import charts
from news_aggregator.charts import (
    MAX_DOWNLOAD_BYTES,
    ChartError,
    ChartSpec,
    as_time,
    build_chart,
    chartable_resources,
    choose_spec,
    load_table,
    plot,
)
from news_aggregator.config import DatasetSource
from news_aggregator.datasets import Dataset, Resource

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"

EUROSTAT_CSV = "\n".join(
    ["STRUCTURE,freq,unit,geo,TIME_PERIOD,OBS_VALUE,OBS_FLAG,LAST UPDATE"]
    + [
        f"dataflow,M,{unit},{geo},2025-{month:02d},{value},,22/10/26 11:00:00"
        for unit in ("PCH", "I15")
        for geo, base in (("DE", 1.0), ("RS", 2.0), ("FR", 3.0))
        for month, value in ((m, base + m / 10) for m in range(1, 7))
    ]
)

SEMICOLON_CSV = "Land;Anzahl;Kennung\n" + "\n".join(
    f"{land};{n},5;{i}"
    for i, (land, n) in enumerate([("Bayern", 10), ("Berlin", 4), ("Hessen", 7)] * 2)
)


def _dataset(*resources: Resource) -> Dataset:
    source = DatasetSource(name="Portal", kind="ckan", url="https://p.example", queries=("q",))
    return Dataset(
        title="Test dataset",
        url="https://p.example/d",
        description="",
        publisher="P",
        released=datetime(2026, 10, 22, tzinfo=UTC),
        source=source,
        resources=resources,
    )


def _frame(text: str, tmp_path: Path, name: str = "t.csv") -> pd.DataFrame:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return load_table(path, "CSV")


def test_time_series_spec_prefers_focus_countries(tmp_path: Path) -> None:
    frame = _frame(EUROSTAT_CSV, tmp_path)
    spec = choose_spec(frame)
    assert spec == ChartSpec("line", "TIME_PERIOD", "OBS_VALUE", "geo", {"unit": "I15"})
    chart = plot(frame, "Construction")
    assert chart.png.startswith(PNG_MAGIC)
    assert chart.caption == (
        "Sum of “OBS_VALUE” by “TIME_PERIOD”; geo: DE, RS; filtered to unit = I15."
    )


def test_semicolon_csv_with_decimal_comma_makes_bar_chart(tmp_path: Path) -> None:
    frame = _frame(SEMICOLON_CSV, tmp_path)
    assert frame["Anzahl"].tolist()[:2] == [10.5, 4.5]
    spec = choose_spec(frame)
    assert spec.kind == "bar"
    assert spec.x == "Land"
    assert spec.value == "Anzahl"
    assert plot(frame, "Länder").png.startswith(PNG_MAGIC)


def test_flat_series_falls_back_to_row_counts(tmp_path: Path) -> None:
    rows = ["year,killed"] + [f"{year},0" for year in (2020, 2020, 2021, 2023, 2023, 2023)]
    chart = plot(_frame("\n".join(rows), tmp_path), "Events")
    assert chart.caption == "Number of rows by “year”."


def test_unplottable_table(tmp_path: Path) -> None:
    with pytest.raises(ChartError):
        plot(_frame("name,comment\na,x\nb,y\nc,z", tmp_path), "Text only")


def test_empty_table(tmp_path: Path) -> None:
    with pytest.raises(ChartError, match=r"could not parse|empty"):
        _frame("", tmp_path)


@pytest.mark.parametrize(
    ("values", "parsed"),
    [
        ([2019, 2020, 2021], True),
        (["2024-Q1", "2024-Q2", "2024Q3"], True),
        (["2025-01", "2025-02", "2025-03"], True),
        ([5, 6, 7], False),
        (["a", "b", "c"], False),
    ],
)
def test_as_time(values: list, parsed: bool) -> None:
    assert (as_time(pd.Series(values)) is not None) is parsed


def test_chartable_resources_order_and_size() -> None:
    resources = (
        Resource("https://x/a.xlsx", "XLSX"),
        Resource("https://x/b.json", "JSON"),
        Resource("https://x/c.csv", "CSV", size=MAX_DOWNLOAD_BYTES + 1),
        Resource("https://x/d.csv", "CSV"),
    )
    assert [r.url for r in chartable_resources(resources)] == [
        "https://x/d.csv",
        "https://x/a.xlsx",
    ]


def test_build_chart_deletes_downloaded_file(
    tmp_path: Path, mock_client: MockClientFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    downloaded: list[Path] = []
    real_download = charts.download

    def recording_download(client: httpx.Client, resource: Resource, directory: Path) -> Path:
        path = real_download(client, resource, directory)
        downloaded.append(path)
        assert path.exists()
        return path

    monkeypatch.setattr(charts, "download", recording_download)
    client = mock_client(lambda request: httpx.Response(200, text=EUROSTAT_CSV))
    chart = build_chart(client, _dataset(Resource("https://x/data.csv", "CSV")))
    assert chart is not None
    assert len(downloaded) == 1
    assert not downloaded[0].exists()
    assert not downloaded[0].parent.exists()


def test_build_chart_tries_next_resource(mock_client: MockClientFactory) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/broken.csv":
            return httpx.Response(400)
        return httpx.Response(200, text=EUROSTAT_CSV)

    dataset = _dataset(Resource("https://x/broken.csv", "CSV"), Resource("https://x/ok.csv", "CSV"))
    assert build_chart(mock_client(handler), dataset) is not None


def test_build_chart_refuses_oversized_download(mock_client: MockClientFactory) -> None:
    big = httpx.Response(200, headers={"content-length": str(MAX_DOWNLOAD_BYTES + 1)}, text="a")
    assert (
        build_chart(mock_client(lambda r: big), _dataset(Resource("https://x/a.csv", "CSV")))
        is None
    )


def test_build_chart_without_tabular_resources(mock_client: MockClientFactory) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise AssertionError("nothing should be downloaded")

    assert build_chart(mock_client(fail), _dataset(Resource("https://x/a.pdf", "PDF"))) is None
