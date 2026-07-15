from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from valuechain.universe_policy import (
    REGIONAL_ALLOCATIONS,
    SECTOR_ALLOCATIONS,
    SELECTION_FACTORS,
    build_universe_policy,
)
from valuechain.universe_policy_api import router


def build_test_app(root: Path, *, token: str = "") -> FastAPI:
    app = FastAPI()
    app.state.acquisition_file_roots = (root,)
    app.state.file_api_token = token
    app.include_router(router)
    return app


def test_universe_policy_allocations_are_complete() -> None:
    assert sum(count for _, count in REGIONAL_ALLOCATIONS) == 1000
    assert sum(count for _, count in SECTOR_ALLOCATIONS) == 1000
    assert sum(weight for _, weight, _ in SELECTION_FACTORS) == 100

    policy = build_universe_policy()
    assert sum(item.target_issuer_groups for item in policy.monitoring_tiers) == 1000
    assert policy.coverage[-1].target_issuer_groups == 1000


def test_universe_policy_endpoint_is_machine_readable(tmp_path: Path) -> None:
    client = TestClient(build_test_app(tmp_path, token="secret-token"))

    unauthorized = client.get("/api/acquisition/universe-policy")
    assert unauthorized.status_code == 401

    response = client.get(
        "/api/acquisition/universe-policy",
        headers={"X-API-Key": "secret-token"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["policy_version"] == "2026-Q3"
    assert payload["selection_unit"] == "issuer_group"
    assert payload["storage_estimate"]["raw_gb_per_year_high"] == 600


def test_openapi_exposes_detailed_universe_policy_schema(tmp_path: Path) -> None:
    client = TestClient(build_test_app(tmp_path))
    schema = client.get("/openapi.json").json()

    operation = schema["paths"]["/api/acquisition/universe-policy"]["get"]
    assert operation["tags"] == ["universe-policy"]
    assert operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "/UniversePolicyResponse"
    )

