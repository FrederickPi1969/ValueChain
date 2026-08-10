"""Policy primitives for compliant, swappable Earnings Call acquisition.

The acquisition pipeline can use these decisions before downloading a source.
They deliberately record provenance and forbid full-text redistribution by
default; parsing/extraction is a separate concern.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from urllib.parse import urlparse


FIRST_PARTY_KINDS = {"issuer_ir", "issuer_or_designated_webcast"}


@dataclass(frozen=True)
class EarningsCallSource:
    issuer: str
    source_url: str
    provider: str
    domain_kind: str
    event_date: str = ""
    fiscal_quarter: str = ""
    content_type: str = ""
    permits_local_processing: bool = True
    permits_redistribution: bool = False

    @property
    def hostname(self) -> str:
        return (urlparse(self.source_url).hostname or "").lower()

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"hostname": self.hostname}


def assess_source(source: EarningsCallSource) -> tuple[bool, str]:
    """Return whether a source may enter the local extraction queue.

    SEC exhibits and issuer-controlled sources have priority. A configured
    provider may be used only when it explicitly allows local processing.
    """

    if urlparse(source.source_url).scheme not in {"https", "http"} or not source.hostname:
        return False, "invalid_source_url"
    if not source.permits_local_processing:
        return False, "local_processing_not_permitted"
    if source.provider == "sec_exhibits":
        return True, "sec_exhibit"
    if source.domain_kind in FIRST_PARTY_KINDS:
        return True, "first_party_source"
    if source.domain_kind == "configured":
        return True, "configured_provider"
    return False, "unapproved_source_kind"


def public_excerpt_limit(source: EarningsCallSource) -> int:
    """Keep the UI evidence-only even when a local parseable copy exists."""

    return 0 if source.permits_redistribution else 280
