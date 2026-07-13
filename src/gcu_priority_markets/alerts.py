from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup

from gcu_priority_markets.models import DisclosureEvent


URL_RE = re.compile(r"https?://[^\s<>\"']+")


def _message_text(message: Message) -> str:
    parts: list[str] = []
    if message.is_multipart():
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            media_type = part.get_content_type()
            if media_type not in {"text/plain", "text/html"}:
                continue
            try:
                content = part.get_content()
            except Exception:  # noqa: BLE001
                continue
            if media_type == "text/html":
                content = BeautifulSoup(str(content), "html.parser").get_text(" ", strip=True)
            parts.append(str(content))
    else:
        try:
            content = message.get_content()
        except Exception:  # noqa: BLE001
            content = ""
        if message.get_content_type() == "text/html":
            content = BeautifulSoup(str(content), "html.parser").get_text(" ", strip=True)
        parts.append(str(content))
    return unescape("\n".join(parts))


def _published_at(message: Message) -> datetime | None:
    raw = message.get("Date")
    if not raw:
        return None
    try:
        parsed = parsedate_to_datetime(raw)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def parse_sedar_alert(message: Message, *, source_path: str | None = None) -> DisclosureEvent:
    subject = str(message.get("Subject") or "SEDAR+ filing alert").strip()
    text = _message_text(message)
    urls = list(dict.fromkeys(URL_RE.findall(text)))
    message_id = str(message.get("Message-ID") or "").strip()
    digest_source = message_id or f"{subject}|{message.get('Date')}|{text}"
    digest = hashlib.sha256(digest_source.encode("utf-8", errors="replace")).hexdigest()[:32]

    issuer_name: str | None = None
    filing_type: str | None = None
    patterns = {
        "issuer": (
            r"(?im)^\s*(?:issuer|profile|company)\s*:\s*(.+?)\s*$",
            r"(?im)^\s*(?:émetteur|société)\s*:\s*(.+?)\s*$",
        ),
        "filing": (
            r"(?im)^\s*(?:document type|filing type|document)\s*:\s*(.+?)\s*$",
            r"(?im)^\s*(?:type de document)\s*:\s*(.+?)\s*$",
        ),
    }
    for pattern in patterns["issuer"]:
        match = re.search(pattern, text)
        if match:
            issuer_name = match.group(1).strip()
            break
    for pattern in patterns["filing"]:
        match = re.search(pattern, text)
        if match:
            filing_type = match.group(1).strip()
            break

    published = _published_at(message)
    detail_url = next((url for url in urls if "sedarplus" in url.lower()), urls[0] if urls else None)
    return DisclosureEvent(
        event_id=f"sedar_plus:email:{digest}",
        source_id="sedar_plus",
        jurisdiction="CA",
        channel="operator_email_alert",
        issuer_name=issuer_name,
        filing_id=f"email:{digest}",
        form=filing_type or "sedar_plus_alert",
        title=subject,
        filed_at=published.date() if published else None,
        published_at=published,
        detail_url=detail_url,
        document_urls=urls,
        metadata={
            "message_id": message_id or None,
            "from": str(message.get("From") or "") or None,
            "to": str(message.get("To") or "") or None,
            "source_path": source_path,
            "body_excerpt": text[:2000],
        },
    )


def iter_email_paths(root: Path) -> Iterable[Path]:
    if root.is_file():
        yield root
        return
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.lower() in {".eml", ".email", ""}:
            yield path


def read_sedar_alerts(root: Path) -> list[DisclosureEvent]:
    events: list[DisclosureEvent] = []
    parser = BytesParser(policy=policy.default)
    for path in iter_email_paths(root):
        try:
            message = parser.parsebytes(path.read_bytes())
            events.append(parse_sedar_alert(message, source_path=str(path)))
        except Exception as exc:  # noqa: BLE001
            digest = hashlib.sha256(str(path).encode("utf-8")).hexdigest()[:20]
            events.append(
                DisclosureEvent(
                    event_id=f"sedar_plus:email_error:{digest}",
                    source_id="sedar_plus",
                    jurisdiction="CA",
                    channel="operator_email_alert_error",
                    filing_id=f"email_error:{digest}",
                    form="parse_error",
                    title=f"Could not parse alert: {path.name}",
                    metadata={"source_path": str(path), "error": f"{type(exc).__name__}: {exc}"},
                )
            )
    return events
