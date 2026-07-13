from __future__ import annotations

from gcu.adapters.base import BaseAdapter
from gcu.http import NetworkBlockedError
from gcu.models import AccessMode, SmokeResult, SmokeStatus


class OfficialWebAdapter(BaseAdapter):
    """Access classification for official sources without a stable public machine API.

    This adapter deliberately does not scrape around CAPTCHAs, session gates, robots rules,
    or commercial-feed controls. It proves that the source is registered and that its access
    contract is explicit; a market-specific adapter can replace it later without changing the
    global schema.
    """

    def smoke(self, *, offline: bool = False) -> SmokeResult:
        endpoint = str(self.definition.official_url)
        if self.definition.access_mode in {
            AccessMode.OFFICIAL_WEB,
            AccessMode.COMMERCIAL_FEED,
            AccessMode.DIRECT_ISSUER_DOCUMENTS,
            AccessMode.RESEARCH_REQUIRED,
        }:
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.OFFICIAL_WEB_ONLY,
                operation="access_classification",
                endpoint=endpoint,
                message=(
                    "Official source registered, but no stable public bulk API is claimed. "
                    "Use official document seeds, a licensed feed, or a jurisdiction-specific browser workflow."
                ),
                evidence={
                    "access_mode": self.definition.access_mode.value,
                    "status": self.definition.status,
                    "credential_env": self.definition.credential_env,
                },
            )
        if offline:
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.CONTRACT_VALIDATED,
                operation="source_contract",
                endpoint=endpoint,
                message="Source metadata and access contract validated offline.",
                evidence={"access_mode": self.definition.access_mode.value},
            )
        try:
            response = self.client.request("GET", endpoint)
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.PASS,
                operation="official_endpoint_reachability",
                endpoint=str(response.url),
                http_status=response.status_code,
                message="Official endpoint was reachable; this is not a claim of bulk API support.",
            )
        except NetworkBlockedError as exc:
            return self.network_blocked_result("official_endpoint_reachability", endpoint, exc)
        except Exception as exc:  # noqa: BLE001
            return SmokeResult(
                source_id=self.source_id,
                status=SmokeStatus.FAIL,
                operation="official_endpoint_reachability",
                endpoint=endpoint,
                message=f"Official endpoint check failed: {type(exc).__name__}: {exc}",
            )
