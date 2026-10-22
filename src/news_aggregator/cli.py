"""Command-line entry point: `news-aggregator run`."""

import argparse
import logging
import sys
from datetime import UTC, datetime
from pathlib import Path

from news_aggregator.config import ConfigError, load_config
from news_aggregator.digest import render_html, render_text
from news_aggregator.mailer import MailerError, build_message, credentials_from_env, send
from news_aggregator.pipeline import AllSourcesFailedError, build_digest, http_client

logger = logging.getLogger("news_aggregator")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="news-aggregator", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="build today's digest and email it")
    run.add_argument(
        "--config-dir", type=Path, default=Path("config"), help="folder with the TOML files"
    )
    run.add_argument(
        "--dry-run", action="store_true", help="write the email to --out instead of sending it"
    )
    run.add_argument(
        "--out", type=Path, default=Path("digest.html"), help="HTML output path for --dry-run"
    )
    return parser


def _write_preview(out: Path, html: str, images: dict[str, bytes]) -> None:
    """Save the HTML with charts as sibling PNGs so it opens in a browser."""
    out.parent.mkdir(parents=True, exist_ok=True)
    for index, (cid, png) in enumerate(images.items()):
        image_path = out.with_name(f"{out.stem}-chart-{index + 1}.png")
        image_path.write_bytes(png)
        html = html.replace(f"cid:{cid}", image_path.name)
    out.write_text(html, encoding="utf-8")


def run(config_dir: Path, *, dry_run: bool, out: Path) -> None:
    """Build the digest and send (or preview) it.

    Raises:
        ConfigError, MailerError, AllSourcesFailedError: with a message saying what to fix.
    """
    config = load_config(config_dir)
    credentials = None if dry_run else credentials_from_env()
    with http_client() as client:
        build = build_digest(config, client, datetime.now(UTC))
    digest = build.digest
    html = render_html(digest)
    if credentials is None:
        _write_preview(out, html, build.images)
        logger.info("dry run: wrote %s (%s)", out, digest.subject)
        return
    user, password = credentials
    message = build_message(
        subject=digest.subject,
        sender=user,
        recipients=config.recipients,
        text=render_text(digest),
        html=html,
        images=build.images,
    )
    send(message, user, password)
    logger.info("sent '%s' to %d recipient(s)", digest.subject, len(config.recipients))


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    for noisy in ("httpx", "trafilatura"):
        logging.getLogger(noisy).setLevel(logging.ERROR)
    try:
        run(args.config_dir, dry_run=args.dry_run, out=args.out)
    except (ConfigError, MailerError, AllSourcesFailedError) as error:
        logger.error("%s", error)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
