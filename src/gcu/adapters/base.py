from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any

from gcu.config import Settings
from gcu.http import NetworkBlockedError, PoliteHttpClient
from gcu.models import (
    DocumentRef,
    EntityRef,
    FilingRef,
    SmokeResult,
    SmokeStatus,
    SourceDefinition,
)


class AdapterError(RuntimeError):
    pass


class CredentialRequiredError(AdapterError):
    def __init__(self, environment_variable: str) -> None:
        super().__init__(f"Credential required: set {environment_variable}")
        self.environment_variable = environment_variable


class BaseAdapter(ABC):
    def __init__(
        self,
        *,
        definition: SourceDefinition,
        settings: Settings,
        client: PoliteHttpClient,
        **_: Any,
    ) -> None:
        self.definition = definition
        self.settings = settings
        self.client = client
        for base_url in (definition.api_base_url, definition.official_url):
            host = getattr(base_url, "host", None)
            if host:
                self.client.rate_limiter.set_host_rate(
                    host,
                    definition.rate_limit_requests_per_second,
                )

    @property
    def source_id(self) -> str:
        return self.definition.source_id

    def require_credential(self, value: str | None, environment_variable: str) -> str:
        if value is None or not value.strip():
            raise CredentialRequiredError(environment_variable)
        return value.strip()

    @abstractmethod
    def smoke(self, *, offline: bool = False) -> SmokeResult:
        """Perform a cheap contract or live endpoint check."""

    def list_entities(self, **_: Any) -> Iterable[EntityRef]:
        raise NotImplementedError(f"{self.source_id} does not implement entity enumeration")

    def list_filings(self, entity: EntityRef, **_: Any) -> Iterable[FilingRef]:
        raise NotImplementedError(f"{self.source_id} does not implement filing enumeration")

    def list_documents(self, filing: FilingRef, **_: Any) -> Iterable[DocumentRef]:
        if filing.primary_document_url:
            yield DocumentRef(
                source_id=self.source_id,
                document_id=f"{filing.filing_id}:primary",
                filing_id=filing.filing_id,
                entity_id=filing.entity_id,
                url=filing.primary_document_url,
                filename=filing.primary_document_url.rsplit("/", 1)[-1] or "primary.bin",
                document_type="primary",
                filed_at=filing.filed_at,
            )

    def network_blocked_result(
        self,
        operation: str,
        endpoint: str,
        exc: NetworkBlockedError,
    ) -> SmokeResult:
        return SmokeResult(
            source_id=self.source_id,
            status=SmokeStatus.NETWORK_BLOCKED,
            operation=operation,
            endpoint=endpoint,
            message=str(exc),
        )

    def credential_result(
        self,
        operation: str,
        endpoint: str,
        exc: CredentialRequiredError,
    ) -> SmokeResult:
        return SmokeResult(
            source_id=self.source_id,
            status=SmokeStatus.CREDENTIAL_REQUIRED,
            operation=operation,
            endpoint=endpoint,
            message=str(exc),
            evidence={"credential_env": exc.environment_variable},
        )
