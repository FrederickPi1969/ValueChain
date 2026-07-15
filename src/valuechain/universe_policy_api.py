from __future__ import annotations

from fastapi import APIRouter, Depends

from valuechain.acquisition_api import require_file_api_access
from valuechain.universe_policy import UniversePolicyResponse, build_universe_policy


router = APIRouter(
    prefix="/api/acquisition",
    tags=["universe-policy"],
    dependencies=[Depends(require_file_api_access)],
)


@router.get(
    "/universe-policy",
    response_model=UniversePolicyResponse,
    summary="Inspect company coverage and monitoring policy",
    description=(
        "Returns the machine-readable coverage contract for complete US and "
        "mainland-China universes plus the Global Strategic 1000. It includes "
        "regional and sector allocations, scoring, mandatory overrides, monitoring "
        "tiers, refresh cadence, deduplication, retention, and storage assumptions."
    ),
    responses={401: {"description": "Missing or invalid API token."}},
)
async def universe_policy() -> UniversePolicyResponse:
    return build_universe_policy()

