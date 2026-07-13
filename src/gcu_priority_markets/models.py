from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DisclosureEvent(BaseModel):
    """Database-free normalized event emitted by every patch monitor."""

    event_id: str
    source_id: str
    jurisdiction: str | None = None
    channel: str
    issuer_id: str | None = None
    issuer_name: str | None = None
    security_code: str | None = None
    filing_id: str
    form: str | None = None
    title: str | None = None
    filed_at: date | None = None
    published_at: datetime | None = None
    detail_url: str | None = None
    document_urls: list[str] = Field(default_factory=list)
    amendment: bool = False
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class MonitorState(BaseModel):
    version: int = 1
    source_id: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    seen_event_ids: list[str] = Field(default_factory=list)
    cursor: dict[str, Any] = Field(default_factory=dict)


class MonitorReport(BaseModel):
    source_id: str
    started_at: datetime
    completed_at: datetime
    observed: int
    emitted: int
    suppressed: int
    primed: bool
    state_file: str
    events_file: str
    errors: list[dict[str, Any]] = Field(default_factory=list)


class FirdsFileRef(BaseModel):
    file_type: str
    file_name: str
    publication_date: date
    download_link: str
    last_refreshed: datetime | None = None


class FirdsListing(BaseModel):
    action: Literal["new", "modified", "terminated", "cancelled", "full"] = "full"
    isin: str
    mic: str
    issuer_lei: str | None = None
    full_name: str | None = None
    short_name: str | None = None
    cfi: str | None = None
    currency: str | None = None
    first_trade_date: date | None = None
    termination_date: date | None = None
    source_file: str | None = None


class TierInputs(BaseModel):
    market_cap_usd: float | None = None
    median_daily_value_usd: float | None = None
    etf_core_weight: float | None = None
    index_membership_count: int | None = None
    value_chain_score: float | None = None
    critical_supply_chain: bool = False
