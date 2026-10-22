import httpx
from conftest import MockClientFactory

from news_aggregator.scrape import MAX_PARAGRAPH_CHARS, fetch_lead_paragraph, lead_paragraph

LEAD = (
    "Germany's parliament approved a record defence budget on Thursday, "
    "lifting military spending above two percent of economic output."
)
ARTICLE = f"""<html><head><title>Budget | Site</title></head><body>
<nav>Home | World | Business</nav>
<article>
  <h1>Bundestag passes budget</h1>
  <p>By a reporter</p>
  <p>{LEAD}</p>
  <p>The vote followed weeks of debate inside the governing coalition about how to fund it.</p>
</article>
<footer>Copyright</footer>
</body></html>"""


def test_lead_paragraph_skips_short_lines() -> None:
    assert lead_paragraph(ARTICLE, "https://site.example/a") == LEAD


def test_lead_paragraph_none_for_empty_page() -> None:
    assert lead_paragraph("<html><body></body></html>", "https://site.example/a") is None


def test_lead_paragraph_truncates_at_word() -> None:
    long_text = "word " * 400
    page = f"<html><body><article><p>{long_text}</p></article></body></html>"
    paragraph = lead_paragraph(page, "https://site.example/a")
    assert paragraph is not None
    assert len(paragraph) <= MAX_PARAGRAPH_CHARS + 1
    assert paragraph.endswith("word…")


def test_fetch_lead_paragraph(mock_client: MockClientFactory) -> None:
    client = mock_client(lambda request: httpx.Response(200, html=ARTICLE))
    assert fetch_lead_paragraph(client, "https://site.example/a") == LEAD


def test_fetch_lead_paragraph_blocked(mock_client: MockClientFactory) -> None:
    client = mock_client(lambda request: httpx.Response(403))
    assert fetch_lead_paragraph(client, "https://site.example/a") is None


def test_fetch_lead_paragraph_network_error(mock_client: MockClientFactory) -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    assert fetch_lead_paragraph(mock_client(fail), "https://site.example/a") is None
