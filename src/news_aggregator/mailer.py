"""Build and send the digest email through Gmail SMTP."""

import os
import smtplib
from email.message import EmailMessage

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465


class MailerError(Exception):
    """The email could not be built or sent."""


def credentials_from_env() -> tuple[str, str]:
    """Read GMAIL_USER and GMAIL_APP_PASSWORD.

    Raises:
        MailerError: naming whichever variable is missing.
    """
    user = os.environ.get("GMAIL_USER", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").strip()
    missing = [
        name
        for name, value in (("GMAIL_USER", user), ("GMAIL_APP_PASSWORD", password))
        if not value
    ]
    if missing:
        raise MailerError(
            f"missing environment variable(s) {missing}; set them to the sending Gmail "
            "address and a Gmail app password (https://myaccount.google.com/apppasswords)"
        )
    return user, password


def build_message(
    *,
    subject: str,
    sender: str,
    recipients: tuple[str, ...],
    text: str,
    html: str,
    images: dict[str, bytes],
) -> EmailMessage:
    """Assemble a multipart email; `images` maps content IDs used in the HTML to PNG bytes.

    Recipients go in Bcc so they don't see each other's addresses.
    """
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = sender
    message["To"] = sender
    message["Bcc"] = ", ".join(recipients)
    message.set_content(text)
    message.add_alternative(html, subtype="html")
    html_part = message.get_body(preferencelist=("html",))
    if not isinstance(html_part, EmailMessage):
        raise MailerError("could not locate the HTML part to attach chart images to")
    for cid, png in images.items():
        html_part.add_related(png, maintype="image", subtype="png", cid=f"<{cid}>")
    return message


def send(message: EmailMessage, user: str, password: str) -> None:
    """Send via Gmail SMTP over SSL.

    Raises:
        MailerError: if login or delivery fails.
    """
    try:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=60) as smtp:
            smtp.login(user, password)
            smtp.send_message(message)
    except smtplib.SMTPAuthenticationError as error:
        raise MailerError(
            f"Gmail rejected the login for {user}; check GMAIL_APP_PASSWORD is a current app "
            f"password, not the account password ({error.smtp_code})"
        ) from error
    except (smtplib.SMTPException, OSError) as error:
        raise MailerError(f"sending via {SMTP_HOST} failed: {error}") from error
