import re
import smtplib
from dataclasses import replace
from datetime import date
from email.message import EmailMessage
from typing import Any
from unittest.mock import MagicMock

import pytest

from news_aggregator import mailer
from news_aggregator.digest import Digest, DigestArticle, DigestDataset, render_html, render_text
from news_aggregator.mailer import MailerError, build_message, credentials_from_env, send


def _digest(**overrides: Any) -> Digest:
    article = DigestArticle(
        title="Serbia <votes> & protests",
        url="https://news.example/a?x=1&y=2",
        paragraph="Students marched.",
        source="Balkan Insight",
        topics=("politics", "security"),
        from_feed_summary=True,
    )
    dataset = DigestDataset(
        title="GDP growth",
        url="https://data.example/gdp",
        publisher="Eurostat",
        source="Eurostat",
        regions=("germany",),
        topics=("economy",),
        chart_cid="chart1@news-aggregator",
        chart_caption="Sum of “OBS_VALUE”.",
    )
    digest = Digest(
        day=date(2026, 10, 22),
        sections=(("balkans", (article,)),),
        datasets=(dataset,),
        failures=("Meduza: could not fetch",),
    )
    return replace(digest, **overrides)


def test_html_escapes_and_links() -> None:
    html = render_html(_digest())
    assert "Serbia &lt;votes&gt; &amp; protests" in html
    assert 'href="https://news.example/a?x=1&amp;y=2"' in html
    assert 'src="cid:chart1@news-aggregator"' in html
    assert "summary from feed" in html
    assert "Meduza: could not fetch" in html
    assert "Balkans (1)" in html


def test_html_empty_states() -> None:
    html = render_html(_digest(sections=(), datasets=(), failures=()))
    assert "No articles passed the relevance threshold today." in html
    assert "No relevant datasets were released today." in html
    assert "Sources that failed today" not in html


def test_dataset_without_chart() -> None:
    dataset = DigestDataset("T", "https://d", "P", "S", (), ())
    html = render_html(_digest(datasets=(dataset,)))
    assert "No chart: no downloadable table" in html
    assert "cid:" not in html


def test_text_alternative_and_subject() -> None:
    digest = _digest()
    text = render_text(digest)
    assert "BALKANS" in text
    assert "https://news.example/a?x=1&y=2" in text
    assert "GDP growth (Eurostat)" in text
    assert digest.subject == "Regional digest 22 Oct 2026: 1 articles, 1 datasets"


def test_build_message_embeds_images_and_hides_recipients() -> None:
    message = build_message(
        subject="S",
        sender="me@gmail.com",
        recipients=("a@example.com", "b@example.com"),
        text="plain",
        html='<img src="cid:c1@x">',
        images={"c1@x": b"\x89PNG fake"},
    )
    assert message["To"] == "me@gmail.com"
    assert message["Bcc"] == "a@example.com, b@example.com"
    images = [part for part in message.walk() if part.get_content_type() == "image/png"]
    assert len(images) == 1
    assert images[0]["Content-ID"] == "<c1@x>"
    html_part = message.get_body(preferencelist=("html",))
    assert html_part is not None
    assert html_part.get_content_type() == "text/html"


@pytest.mark.parametrize(
    ("env", "missing"),
    [
        ({}, "['GMAIL_USER', 'GMAIL_APP_PASSWORD']"),
        ({"GMAIL_USER": "me@gmail.com"}, "['GMAIL_APP_PASSWORD']"),
        ({"GMAIL_USER": " ", "GMAIL_APP_PASSWORD": "pw"}, "['GMAIL_USER']"),
    ],
)
def test_missing_credentials(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], missing: str
) -> None:
    monkeypatch.delenv("GMAIL_USER", raising=False)
    monkeypatch.delenv("GMAIL_APP_PASSWORD", raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    with pytest.raises(MailerError, match=re.escape(missing)):
        credentials_from_env()


def test_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMAIL_USER", "me@gmail.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh")
    assert credentials_from_env() == ("me@gmail.com", "abcd efgh")


def _fake_smtp(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    smtp = MagicMock()
    factory = MagicMock(return_value=smtp)
    smtp.__enter__.return_value = smtp
    monkeypatch.setattr(mailer.smtplib, "SMTP_SSL", factory)
    return smtp


def test_send(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = _fake_smtp(monkeypatch)
    message = EmailMessage()
    send(message, "me@gmail.com", "pw")
    smtp.login.assert_called_once_with("me@gmail.com", "pw")
    smtp.send_message.assert_called_once_with(message)


def test_send_bad_password(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = _fake_smtp(monkeypatch)
    smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
    with pytest.raises(MailerError, match="app password"):
        send(EmailMessage(), "me@gmail.com", "pw")


def test_send_network_error(monkeypatch: pytest.MonkeyPatch) -> None:
    smtp = _fake_smtp(monkeypatch)
    smtp.send_message.side_effect = OSError("connection reset")
    with pytest.raises(MailerError, match="connection reset"):
        send(EmailMessage(), "me@gmail.com", "pw")
