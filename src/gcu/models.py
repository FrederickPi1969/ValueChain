from __future__ import annotations

from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class AccessMode(StrEnum):
    PUBLIC_API = "public_api"
    PUBLIC_BULK = "public_bulk"
    API_KEY = "api_key"
    SEMI_PUBLIC_WEB_ENDPOINT = "semi_public_web_endpoint"
    OFFICIAL_WEB = "official_web"
    COMMERCIAL_FEED = "commercial_feed"
    DIRECT_ISSUER_DOCUMENTS = "direct_issuer_documents"
    RESEARCH_REQUIRED = "research_required"


class Capability(StrEnum):
    ENTITY_UNIVERSE = "entity_universe"
    FILING_DISCOVERY = "filing_discovery"
    DOCUMENT_INVENTORY = "document_inventory"
    DOCUMENT_DOWNLOAD = "document_download"
    STRUCTURED_FACTS = "structured_facts"
    HISTORICAL_INDEX = "historical_index"
    RECONCILIATION = "reconciliation"
    ENTITY_RESOLUTION = "entity_resolution"
    EVENT_MONITORING = "event_monitoring"
    REGISTRY_DIRECTORY = "registry_directory"
    MARKET_VENUE_DIRECTORY = "market_venue_directory"


class SourceDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    name: str
    jurisdictions: list[str]
    macro_regions: list[str] = Field(default_factory=list)
    adapter: str
    access_mode: AccessMode
    official_url: HttpUrl
    api_base_url: HttpUrl | None = None
    credential_env: str | None = None
    capabilities: list[Capability] = Field(default_factory=list)
    historical_scope: str = "unknown"
    status: str = "implemented"
    notes: str = ""
    rate_limit_requests_per_second: float = 1.0


class EntityRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    entity_id: str
    source_id: str
    source_entity_id: str
    legal_name: str
    jurisdiction: str | None = None
    exchange: str | None = None
    ticker: str | None = None
    lei: str | None = None
    isin: str | None = None
    local_registry_id: str | None = None
    aliases: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SecurityListing(BaseModel):
    listing_id: str
    entity_id: str
    jurisdiction: str
    exchange: str
    local_ticker: str
    isin: str | None = None
    currency: str | None = None
    valid_from: date | None = None
    valid_to: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class FilingRef(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_id: str
    filing_id: str
    entity_id: str
    source_entity_id: str | None = None
    form: str | None = None
    title: str | None = None
    filed_at: date | None = None
    period_end: date | None = None
    detail_url: str | None = None
    primary_document_url: str | None = None
    language: str | None = None
    amendment: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class FilingEvent(BaseModel):
    """Database-free event emitted by a disclosure monitor."""

    model_config = ConfigDict(extra="allow")

    event_id: str
    source_id: str
    channel: str
    cik: str | None = None
    company_name: str | None = None
    form: str | None = None
    accession_number: str | None = None
    filed_at: date | None = None
    accepted_at: datetime | None = None
    detail_url: str | None = None
    primary_document_url: str | None = None
    amendment: bool = False
    discovered_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = Field(default_factory=dict)


class MonitorRun(BaseModel):
    source_id: str
    started_at: datetime
    completed_at: datetime
    channels: list[str]
    forms: list[str]
    watchlist_ciks: list[str]
    events_observed: int
    new_events: int
    events_suppressed: int = 0
    events_file: str | None = None
    state_file: str | None = None
    channel_counts: dict[str, int] = Field(default_factory=dict)
    errors: list[dict[str, Any]] = Field(default_factory=list)


class CredentialTransport(StrEnum):
    QUERY = "query"
    HEADER = "header"
    BASIC_USERNAME = "basic_username"


class DocumentRef(BaseModel):
    """A document request without embedding secret credential values in manifests."""

    model_config = ConfigDict(extra="allow")

    source_id: str
    document_id: str
    url: str
    filename: str
    filing_id: str | None = None
    entity_id: str | None = None
    document_type: str | None = None
    expected_media_type: str | None = None
    filed_at: date | None = None
    request_params: dict[str, Any] = Field(default_factory=dict)
    request_headers: dict[str, str] = Field(default_factory=dict)
    credential_env: str | None = None
    credential_transport: CredentialTransport | None = None
    credential_name: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("filename")
    @classmethod
    def safe_filename(cls, value: str) -> str:
        cleaned = value.strip().replace("\\", "_").replace("/", "_")
        if not cleaned or cleaned in {".", ".."}:
            raise ValueError("filename must be non-empty and safe")
        return cleaned

    @field_validator("url")
    @classmethod
    def forbid_known_secrets_in_url(cls, value: str) -> str:
        lower = value.lower()
        sensitive_names = ("subscription-key=", "crtfc_key=", "api_key=", "apikey=")
        if any(name in lower for name in sensitive_names):
            raise ValueError("DocumentRef.url must not contain API credentials")
        return value


class DownloadStatus(StrEnum):
    DOWNLOADED = "downloaded"
    ALREADY_PRESENT = "already_present"
    SKIPPED = "skipped"
    FAILED = "failed"
    CREDENTIAL_REQUIRED = "credential_required"
    NOT_MACHINE_ACCESSIBLE = "not_machine_accessible"
    NETWORK_BLOCKED = "network_blocked"
    VALIDATION_FAILED = "validation_failed"


class DownloadRecord(BaseModel):
    model_config = ConfigDict(extra="allow")

    source_id: str
    document_id: str
    url: str
    filename: str
    status: DownloadStatus
    attempted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    http_status: int | None = None
    media_type: str | None = None
    content_length: int | None = None
    sha256: str | None = None
    local_path: str | None = None
    error_type: str | None = None
    error_message: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SmokeStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    SKIP = "skip"
    CREDENTIAL_REQUIRED = "credential_required"
    NETWORK_BLOCKED = "network_blocked"
    CONTRACT_VALIDATED = "contract_validated"
    OFFICIAL_WEB_ONLY = "official_web_only"


class SmokeResult(BaseModel):
    source_id: str
    status: SmokeStatus
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    operation: str
    message: str
    endpoint: str | None = None
    http_status: int | None = None
    records_observed: int | None = None
    evidence: dict[str, Any] = Field(default_factory=dict)
