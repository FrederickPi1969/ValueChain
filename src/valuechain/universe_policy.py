from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


POLICY_VERSION = "2026-Q3"
POLICY_AS_OF_DATE = date(2026, 7, 14)


class CoverageBlock(BaseModel):
    universe_id: str = Field(description="Stable identifier for the coverage block.")
    scope: str = Field(description="Jurisdiction or selection scope.")
    coverage_mode: str = Field(description="Complete or strategically selected coverage.")
    target_issuer_groups: int | None = Field(
        description="Target issuer-group count; null means the complete source denominator."
    )
    consumes_global_strategic_cap: bool
    notes: str


class Allocation(BaseModel):
    name: str
    target_issuer_groups: int
    notes: str = ""


class SelectionFactor(BaseModel):
    name: str
    weight_percent: int
    question: str


class MonitoringTier(BaseModel):
    tier: str
    target_issuer_groups: int
    discovery_policy: str
    retention_policy: str


class CadencePolicy(BaseModel):
    process: str
    cadence: str
    behavior: str


class StorageEstimate(BaseModel):
    raw_gb_per_year_low: int
    raw_gb_per_year_high: int
    assumptions: list[str]


class UniversePolicyResponse(BaseModel):
    policy_version: str
    as_of_date: date
    status: str
    objective: str
    selection_unit: str
    coverage: list[CoverageBlock]
    regional_allocations: list[Allocation]
    primary_sector_allocations: list[Allocation]
    selection_factors: list[SelectionFactor]
    default_entry_score: int
    mandatory_override_rules: list[str]
    monitoring_tiers: list[MonitoringTier]
    update_cadence: list[CadencePolicy]
    update_rules: list[str]
    identity_and_deduplication_rules: list[str]
    retention_rules: list[str]
    storage_estimate: StorageEstimate
    related_endpoints: dict[str, str]
    current_limitations: list[str]


REGIONAL_ALLOCATIONS = (
    ("Europe, United Kingdom, Switzerland, and Nordics", 300),
    ("Japan", 92),
    ("South Korea", 89),
    ("Taiwan", 75),
    ("India", 75),
    ("Canada", 55),
    ("Australia and New Zealand", 50),
    ("Hong Kong and non-A-share Hong Kong issuers", 40),
    ("Singapore", 20),
    ("Brazil", 35),
    ("Middle East, Israel, and Turkey", 60),
    ("Southeast Asia excluding Singapore", 50),
    ("Mexico and Latin America excluding Brazil", 35),
    ("Africa", 24),
)


SECTOR_ALLOCATIONS = (
    ("Digital technology and semiconductors", 150),
    ("Energy and power", 160),
    ("Industrial and capital equipment", 120),
    ("Transportation and logistics", 110),
    ("Mining, materials, and chemicals", 100),
    ("Financial infrastructure", 110),
    ("Telecommunications and networks", 70),
    ("Healthcare and pharmaceuticals", 65),
    ("Food, agriculture, and consumer supply chains", 55),
    ("Defense and security", 30),
    ("Water, waste, and physical infrastructure", 30),
)


SELECTION_FACTORS = (
    ("strategic_centrality", 25, "Does the group sit on important upstream or downstream paths?"),
    ("global_revenue_or_export_exposure", 15, "Is its activity materially cross-border?"),
    ("critical_infrastructure_role", 15, "Does it operate essential physical or financial infrastructure?"),
    ("substitution_difficulty", 10, "How quickly could customers or markets replace it?"),
    ("scarce_capacity_or_resource_control", 10, "Does it control constrained capacity, resources, or routes?"),
    ("market_cap_and_etf_relevance", 10, "Is it investable and material to institutional portfolios?"),
    ("cross_border_operating_footprint", 10, "Does it connect several markets or jurisdictions?"),
    ("disclosure_accessibility", 5, "Can authoritative disclosures be monitored reproducibly?"),
)


def build_universe_policy() -> UniversePolicyResponse:
    return UniversePolicyResponse(
        policy_version=POLICY_VERSION,
        as_of_date=POLICY_AS_OF_DATE,
        status="methodology_defined_component_selection_in_progress",
        objective=(
            "Complete United States and mainland-China issuer coverage plus a "
            "versioned Global Strategic 1000 for institutionally relevant companies."
        ),
        selection_unit="issuer_group",
        coverage=[
            CoverageBlock(
                universe_id="us_complete",
                scope="United States SEC issuer denominator",
                coverage_mode="complete",
                target_issuer_groups=None,
                consumes_global_strategic_cap=False,
                notes="SEC source-local issuers remain complete; filing-form retention is policy filtered.",
            ),
            CoverageBlock(
                universe_id="cn_mainland_complete",
                scope="Mainland China CNINFO issuer denominator",
                coverage_mode="complete",
                target_issuer_groups=None,
                consumes_global_strategic_cap=False,
                notes="Full annual, semiannual, Q1, and Q3 reports are retained; summaries are excluded.",
            ),
            CoverageBlock(
                universe_id="global_strategic_1000",
                scope="Major non-US and non-mainland-China markets",
                coverage_mode="strategically_selected",
                target_issuer_groups=1000,
                consumes_global_strategic_cap=True,
                notes="Includes the existing curated Japan 92 and South Korea 89 issuer groups.",
            ),
        ],
        regional_allocations=[
            Allocation(name=name, target_issuer_groups=count)
            for name, count in REGIONAL_ALLOCATIONS
        ],
        primary_sector_allocations=[
            Allocation(name=name, target_issuer_groups=count)
            for name, count in SECTOR_ALLOCATIONS
        ],
        selection_factors=[
            SelectionFactor(name=name, weight_percent=weight, question=question)
            for name, weight, question in SELECTION_FACTORS
        ],
        default_entry_score=60,
        mandatory_override_rules=[
            "Include strategically important grid, port, railway, exchange, and telecom operators.",
            "Consider national oil, gas, mining, fertilizer, and critical chemical companies.",
            "Include difficult-to-substitute semiconductor, industrial, aerospace, defense, and pharmaceutical chokepoints.",
            "Consider systemic banks, insurers, payment networks, clearing houses, and securities exchanges.",
            "Include groups controlling critical minerals, production capacity, transport routes, data centers, or power infrastructure.",
            "Document a reviewer-approved reason when a mandatory override enters below the default score.",
        ],
        monitoring_tiers=[
            MonitoringTier(
                tier="S",
                target_issuer_groups=200,
                discovery_policy="Monitor source indexes continuously or at the fastest lawful source cadence.",
                retention_policy="Retain periodic reports, material announcements, contracts, capex disclosures, and event filings.",
            ),
            MonitoringTier(
                tier="A",
                target_issuer_groups=400,
                discovery_policy="Run at least daily metadata discovery where the source supports it.",
                retention_policy="Retain all periodic reports and selected material announcements.",
            ),
            MonitoringTier(
                tier="B",
                target_issuer_groups=400,
                discovery_policy="Refresh filing metadata on the source schedule; do not bulk-download low-value events.",
                retention_policy="Retain annual and interim reports; acquire other documents on demand.",
            ),
        ],
        update_cadence=[
            CadencePolicy(
                process="new_filing_discovery",
                cadence="continuous, hourly, or daily according to the lawful source contract",
                behavior="Discover recent filings first, persist source-native ids, and never exceed source quotas.",
            ),
            CadencePolicy(
                process="issuer_registry_refresh",
                cadence="daily for active machine sources; otherwise on each official snapshot release",
                behavior="Track additions, delistings, identifier changes, and row-count drift without deleting history.",
            ),
            CadencePolicy(
                process="global_strategic_rebalance",
                cadence="quarterly",
                behavior="Recompute scores, apply benchmark and strategic overrides, and publish immutable changes.",
            ),
            CadencePolicy(
                process="methodology_review",
                cadence="annually",
                behavior="Review sector and regional quotas, source rights, storage cost, and observed coverage gaps.",
            ),
            CadencePolicy(
                process="corporate_action_reconciliation",
                cadence="event driven with quarterly completeness reconciliation",
                behavior="Resolve mergers, spin-offs, renames, listing moves, and parent-child changes.",
            ),
        ],
        update_rules=[
            "Publish each quarterly universe as an immutable version with effective_from and effective_to dates.",
            "Record additions, removals, score changes, tier changes, and a human-readable reason.",
            "A company leaving the universe stops future default acquisition but does not delete archived evidence.",
            "Keep a buffer candidate queue so replacements do not require an ad hoc research cycle.",
            "Use source-listener health separately from universe methodology; inspect /api/acquisition/sources for live coverage.",
        ],
        identity_and_deduplication_rules=[
            "Count ADRs, dual listings, depositary receipts, and share classes once under issuer_group_id.",
            "Assign one primary disclosure source and any number of secondary sources.",
            "Use SEC as primary where it provides the authoritative filing; use native sources for missing local disclosures.",
            "Deduplicate bytes by SHA-256 and reports by issuer group, period, canonical type, and source-native filing id.",
            "Never overwrite source-local issuer and filing identifiers during entity normalization.",
        ],
        retention_rules=[
            "Store authoritative raw documents and manifests on HDD; store metadata, queue state, and provenance in PostgreSQL.",
            "An official document acquired through ad hoc fallback is persisted and is not downloaded again.",
            "Do not blanket-download low-value daily announcements for Tier B groups.",
            "Do not treat vendor or search results as canonical evidence without an official source record.",
            "Preserve hashes, parser versions, accepted timestamps, URLs, and correction/amendment relationships.",
        ],
        storage_estimate=StorageEstimate(
            raw_gb_per_year_low=470,
            raw_gb_per_year_high=600,
            assumptions=[
                "Complete US and mainland-China target forms plus tiered Global Strategic 1000 retention.",
                "SEC/native duplicate reports and exact document bytes are deduplicated.",
                "Low-value daily announcements and temporary extracted package contents are not retained by default.",
                "The estimate is recalculated from measured bytes after every quarterly release.",
            ],
        ),
        related_endpoints={
            "live_source_coverage": "/api/acquisition/sources",
            "source_issuer_registry": "/api/acquisition/issuers",
            "filing_inventory": "/api/acquisition/filings",
            "document_inventory": "/api/acquisition/documents",
            "document_type_schema": "/api/acquisition/schema",
            "local_first_resolver": "/api/acquisition/resolve",
        },
        current_limitations=[
            "The methodology is defined, but the complete versioned Global Strategic 1000 component CSV is still being curated.",
            "Authorized or licensed disclosure delivery remains required for some markets, including HKEX, SEDAR+, and ASX.",
            "The current service searches filing metadata and extracted evidence; production full-document text search is not yet exposed.",
        ],
    )

