from __future__ import annotations

import csv
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

from valuechain.io_utils import read_jsonl, write_json


OPERATING_RELATION_TYPES = {
    "supplier_dependency",
    "customer_dependency",
    "manufacturing_dependency",
    "foundry_dependency",
    "packaging_or_assembly_dependency",
    "cloud_or_hosting_dependency",
    "data_center_dependency",
    "power_or_utility_dependency",
    "network_or_interconnection_dependency",
    "distribution_or_channel_dependency",
    "licensing_dependency",
}
RISK_RELATION_TYPES = {
    "concentration_risk",
    "facility_or_geographic_exposure",
    "power_or_utility_dependency",
    "data_center_dependency",
}
STRATEGIC_RELATION_TYPES = {"strategic_partner", "co_investment"}
GENERIC_OBJECT_TERMS = {
    "dependency class",
    "supply class",
    "capacity class",
    "exposure class",
    "risk class",
    "industry risks",
    "risk factors",
    "suppliers",
    "customers",
    "providers",
    "partners",
    "facilities",
    "manufacturing facilities",
}
GEOGRAPHY_OBJECTS = {
    "asia",
    "australia",
    "canada",
    "china",
    "europe",
    "germany",
    "hong kong",
    "india",
    "ireland",
    "israel",
    "japan",
    "korea",
    "macau",
    "malaysia",
    "netherlands",
    "russia",
    "singapore",
    "south korea",
    "taiwan",
    "united kingdom",
    "united states",
}
HEADING_OBJECTS = {
    "business risks",
    "general risks",
    "industry risks",
    "item 1a",
    "risk factors",
}
PLACEHOLDER_OBJECTS = {
    "aibs",
    "competitors",
    "compute and networking segment",
    "compute & networking segment",
    "csps",
    "customer a",
    "customer b",
    "customer c",
    "direct customer a",
    "direct customer b",
    "direct customer c",
    "dpus",
    "direct, indirect and cloud service purchasers",
    "enterprises and governments",
    "euv systems components",
    "fpgas",
    "gpu accelerators",
    "neocloud builders",
    "odms",
    "one customer",
    "server cpus",
    "supply chain",
    "adaptive socs",
    "ai nics",
    "designated non-423 corporation",
    "ip-related costs",
    "related corporation",
}
WEAK_OBJECT_PATTERNS = {
    "alternative production capacity",
    "another direct customer",
    "direct customer",
    "largest customer",
    "limited number of vendors",
    "limited or single-source suppliers",
    "limited group of suppliers",
    "partner network",
    "partner and retail network",
    "services. customers",
    "single-sourced",
    "sole supplier",
    "third parties",
    "third-party manufacturers",
    "third-party vendors",
    "through direct customer",
}
RELATION_LOCAL_CUES = {
    "supplier_dependency": {
        "component",
        "components",
        "manufactur",
        "procure",
        "production",
        "sourc",
        "suppl",
    },
    "customer_dependency": {
        "customer",
        "customers",
        "revenue",
        "sales",
        "sell",
        "signed an agreement",
    },
    "manufacturing_dependency": {
        "contract manufacturer",
        "fabricat",
        "manufactur",
        "production",
    },
    "foundry_dependency": {
        "fabricat",
        "foundr",
        "production",
        "wafer",
    },
    "packaging_or_assembly_dependency": {
        "assembly",
        "packag",
        "subcontractor",
        "test",
    },
    "cloud_or_hosting_dependency": {
        "aws",
        "azure",
        "cloud",
        "hosting",
        "hosted",
        "infrastructure services",
    },
    "data_center_dependency": {
        "colocation",
        "data center",
        "datacenter",
        "facility",
    },
    "power_or_utility_dependency": {
        "electricity",
        "energy",
        "grid",
        "power",
        "renewable",
        "utility",
    },
    "network_or_interconnection_dependency": {
        "connectivity",
        "interconnection",
        "network",
        "switch",
    },
    "distribution_or_channel_dependency": {
        "channel",
        "distributor",
        "partner",
        "reseller",
    },
    "licensing_dependency": {
        "intellectual property",
        "ip ",
        "license",
        "licensing",
        "patent",
    },
    "strategic_partner": {
        "collaboration",
        "partnership",
        "strategic",
        "strategic partner",
    },
    "co_investment": {
        "co-invest",
        "investment",
        "joint investment",
        "joint venture",
    },
}
RISK_CONTEXT_CUES = {
    "adverse",
    "availability",
    "bankruptcies",
    "constraint",
    "constraints",
    "credit risk",
    "delay",
    "delays",
    "depend",
    "disrupt",
    "disruption",
    "fail",
    "geopolitical",
    "labor",
    "loss",
    "material adverse",
    "natural disaster",
    "not available",
    "political",
    "procure alternatives",
    "regulatory",
    "restriction",
    "risk",
    "shortage",
    "supply constraints",
    "tariff",
    "unable",
}


@dataclass(frozen=True)
class BriefOptions:
    max_claims_per_section: int = 8
    max_evidence_table_rows: int = 28
    max_evidence_chars: int = 520
    min_current_fact_confidence: float = 0.72
    analyst_max_tokens: int = 1600


@dataclass(frozen=True)
class BriefLLMConfig:
    base_url: str
    api_key: str
    model: str
    proxy_url: str = ""
    timeout_s: int = 180


class BriefReportLLMClient:
    """Report-only OpenAI-compatible client with tolerant JSON parsing."""

    def __init__(self, config: BriefLLMConfig) -> None:
        self.config = config

    def chat_json(self, system: str, user: str, max_tokens: int = 1200) -> Any:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        request_kwargs: dict[str, Any] = {
            "headers": {"Authorization": f"Bearer {self.config.api_key}"},
            "json": payload,
            "timeout": self.config.timeout_s,
        }
        if self.config.proxy_url:
            request_kwargs["proxy"] = self.config.proxy_url
        response = httpx.post(
            f"{self.config.base_url.rstrip('/')}/chat/completions",
            **request_kwargs,
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        return parse_lenient_json_content(content)


@dataclass(frozen=True)
class BriefClaim:
    claim_id: str
    category: str
    relation_type: str
    object: str
    canonical_object: str
    object_lei: str
    modality_mix: str
    evidence_count: int
    avg_confidence: float
    forms: str
    accessions: str
    first_seen: str
    last_seen: str
    representative_evidence_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    claim_id: str
    relation_type: str
    modality: str
    subject: str
    object: str
    canonical_object: str
    confidence_score: float
    certainty: str
    form: str
    filing_date: str
    accession_number: str
    section: str
    paragraph_offset: int
    source_document_url: str
    evidence_text: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompanyDependencyBrief:
    company: dict[str, Any]
    run_dir: str
    model_version: str
    company_role: dict[str, Any]
    top_operating_dependencies: list[BriefClaim]
    top_risk_exposures: list[BriefClaim]
    current_fact_edges: list[BriefClaim]
    strategic_relations: list[BriefClaim]
    evidence_table: list[EvidenceRow]
    analyst_interpretation: dict[str, Any]
    diagnostics: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "run_dir": self.run_dir,
            "model_version": self.model_version,
            "company_role": self.company_role,
            "top_operating_dependencies": [claim.to_dict() for claim in self.top_operating_dependencies],
            "top_risk_exposures": [claim.to_dict() for claim in self.top_risk_exposures],
            "current_fact_edges": [claim.to_dict() for claim in self.current_fact_edges],
            "strategic_relations": [claim.to_dict() for claim in self.strategic_relations],
            "evidence_table": [row.to_dict() for row in self.evidence_table],
            "analyst_interpretation": self.analyst_interpretation,
            "diagnostics": self.diagnostics,
        }


def generate_company_dependency_brief(
    run_dir: Path,
    company_query: str,
    llm_client: Any | None = None,
    model_version: str = "",
    options: BriefOptions | None = None,
) -> CompanyDependencyBrief:
    options = options or BriefOptions()
    inputs = load_brief_inputs(run_dir)
    company = find_company(inputs["companies"], company_query)
    evidence = filter_company_evidence(inputs["evidence"], company)
    selected_entities = load_selected_entity_map(inputs["llm_selected_entities"])
    enriched_evidence = [enrich_evidence_object(row, selected_entities) for row in evidence]

    claims = build_claims(enriched_evidence)
    current_fact_evidence = [
        row
        for row in enriched_evidence
        if row.get("relation_type") != "subsidiary_or_control"
        and row.get("modality") == "current_fact"
        and safe_float(row.get("confidence_score")) >= options.min_current_fact_confidence
    ]
    display_current_fact_evidence = filter_supported_current_fact_evidence(current_fact_evidence)
    top_operating = select_top_operating_claims(display_current_fact_evidence, options)
    risk_evidence = filter_display_risk_evidence([
        row
        for row in enriched_evidence
        if row.get("relation_type") != "subsidiary_or_control"
        and (
            row.get("relation_type") in RISK_RELATION_TYPES
            or "risk" in str(row.get("modality", ""))
            or "hypothetical" in str(row.get("modality", ""))
        )
    ])
    strategic_evidence = select_strategic_evidence(enriched_evidence)
    top_risk = select_top_risk_claims(risk_evidence, options)
    current_fact = top_claims(
        build_claims(filter_display_current_fact_evidence(display_current_fact_evidence), id_prefix="F"),
        options.max_claims_per_section,
    )
    strategic = top_claims(
        build_claims(strategic_evidence, id_prefix="S"),
        options.max_claims_per_section,
    )
    display_evidence = dedupe_evidence(display_current_fact_evidence + risk_evidence + strategic_evidence)
    evidence_table = build_evidence_table(
        display_evidence,
        top_operating + top_risk + current_fact + strategic,
        max_rows=options.max_evidence_table_rows,
        max_chars=options.max_evidence_chars,
    )
    role = infer_company_role(company, claims)
    interpretation = generate_analyst_interpretation(
        company=company,
        role=role,
        top_operating=top_operating,
        top_risk=top_risk,
        current_fact=current_fact,
        strategic=strategic,
        evidence_table=evidence_table,
        llm_client=llm_client,
        model_version=model_version,
        max_tokens=options.analyst_max_tokens,
    )
    return CompanyDependencyBrief(
        company=company,
        run_dir=str(run_dir),
        model_version=model_version,
        company_role=role,
        top_operating_dependencies=top_operating,
        top_risk_exposures=top_risk,
        current_fact_edges=current_fact,
        strategic_relations=strategic,
        evidence_table=evidence_table,
        analyst_interpretation=interpretation,
        diagnostics={
            "company_query": company_query,
            "company_evidence_rows": len(evidence),
            "claim_count": len(claims),
            "llm_enabled": llm_client is not None,
            "selected_entity_count": len(selected_entities),
        },
    )


def select_top_operating_claims(
    current_fact_evidence: list[dict[str, Any]],
    options: BriefOptions,
) -> list[BriefClaim]:
    operating_evidence = [
        row
        for row in current_fact_evidence
        if row.get("relation_type") in OPERATING_RELATION_TYPES
    ]
    current_claims = build_claims(operating_evidence)
    named_claims = [
        claim
        for claim in current_claims
        if not is_low_quality_operating_object(claim.canonical_object)
    ]
    if named_claims:
        return top_operating_claims(named_claims, options.max_claims_per_section)
    weak_descriptive_claims = [
        claim
        for claim in current_claims
        if claim.relation_type
        in {
            "supplier_dependency",
            "manufacturing_dependency",
            "foundry_dependency",
            "packaging_or_assembly_dependency",
        }
        and matches_weak_object_pattern(claim.canonical_object)
        and not is_generic_object(claim.canonical_object)
        and not is_geography_object(claim.canonical_object)
        and not is_heading_object(claim.canonical_object)
    ]
    if weak_descriptive_claims:
        return top_operating_claims(weak_descriptive_claims, options.max_claims_per_section)
    generic_claims = [
        claim
        for claim in current_claims
        if is_generic_object(claim.canonical_object)
        and not is_geography_object(claim.canonical_object)
        and not is_heading_object(claim.canonical_object)
    ]
    return top_operating_claims(generic_claims, options.max_claims_per_section)


def top_operating_claims(claims: list[BriefClaim], limit: int) -> list[BriefClaim]:
    ordered = sorted(
        claims,
        key=lambda claim: (
            operating_relation_rank(claim.relation_type),
            -claim.evidence_count,
            -claim.avg_confidence,
            claim.relation_type,
            claim.canonical_object.lower(),
        ),
    )
    return ordered[:limit]


def operating_relation_rank(relation_type: str) -> int:
    if relation_type == "licensing_dependency":
        return 2
    if relation_type == "distribution_or_channel_dependency":
        return 1
    return 0


def select_top_risk_claims(risk_evidence: list[dict[str, Any]], options: BriefOptions) -> list[BriefClaim]:
    claims = build_claims(risk_evidence, id_prefix="R")
    ordered = sorted(
        claims,
        key=lambda claim: (
            risk_object_rank(claim.canonical_object),
            -claim.evidence_count,
            -claim.avg_confidence,
            claim.relation_type,
            claim.canonical_object.lower(),
        ),
    )
    return ordered[: options.max_claims_per_section]


def risk_object_rank(value: str) -> int:
    if is_generic_object(value) or is_heading_object(value):
        return 2
    if matches_weak_object_pattern(value):
        return 1
    return 0


def filter_supported_current_fact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    supported = [row for row in evidence if is_supported_current_fact_display_row(row)]
    return supported or evidence


def is_supported_current_fact_display_row(row: dict[str, Any]) -> bool:
    relation_type = str(row.get("relation_type") or "")
    if relation_type in RISK_RELATION_TYPES:
        return is_supported_display_risk_relation(row)
    return is_supported_display_relation(row)


def filter_display_risk_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    supported = [row for row in evidence if is_supported_display_risk_relation(row)]
    return supported or evidence


def is_supported_display_risk_relation(row: dict[str, Any]) -> bool:
    relation_type = str(row.get("relation_type") or "")
    modality = str(row.get("modality") or "")
    obj = str(row.get("canonical_object") or row.get("object") or "")
    if modality == "current_fact" and relation_type in OPERATING_RELATION_TYPES and not is_supported_display_relation(row):
        return False
    if modality == "current_fact" and (is_generic_object(obj) or is_heading_object(obj)):
        return False
    if relation_type == "concentration_risk":
        return has_concentration_context(row)
    if relation_type == "facility_or_geographic_exposure":
        return has_facility_or_geography_context(row) and (
            has_risk_context(row) or modality in {"risk_hypothetical", "forward_looking"}
        )
    if relation_type in RISK_RELATION_TYPES and not has_risk_context(row):
        return "risk" in modality or "hypothetical" in modality
    return True


def is_supported_display_relation(row: dict[str, Any]) -> bool:
    relation_type = str(row.get("relation_type") or "")
    if relation_type not in OPERATING_RELATION_TYPES | STRATEGIC_RELATION_TYPES:
        return True
    obj = str(row.get("canonical_object") or row.get("object") or "")
    if not evidence_mentions_object(row):
        return is_low_quality_display_object(obj)
    context = local_object_context(row)
    if is_negative_strategic_context(context):
        return False
    if relation_type == "licensing_dependency" and is_credit_agreement_index_context(row, context):
        return False
    if is_trademark_context(context):
        return False
    if row.get("modality") == "current_fact" and is_hypothetical_context(context):
        return False
    if is_competition_landscape_context(row, context) and not has_strong_dependency_cue(context):
        return False
    if is_list_only_context(context) and not has_strong_dependency_cue(context):
        return False
    cues = RELATION_LOCAL_CUES.get(relation_type)
    if cues and not contains_any(context, cues):
        return False
    if relation_type == "supplier_dependency" and not has_strong_dependency_cue(context):
        return contains_any(context, {"supplier", "supplied by", "supplies", "vendor"})
    return True


def select_strategic_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    for row in evidence:
        relation_type = str(row.get("relation_type") or "")
        modality = str(row.get("modality") or "")
        obj = str(row.get("canonical_object") or row.get("object") or "")
        if is_low_quality_display_object(obj):
            continue
        if relation_type in STRATEGIC_RELATION_TYPES or modality == "strategic":
            if is_supported_display_relation(row):
                rows.append(row)
    return rows


def filter_display_current_fact_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    filtered = [
        row
        for row in evidence
        if str(row.get("relation_type") or "") in OPERATING_RELATION_TYPES
        and not is_low_quality_display_object(str(row.get("canonical_object") or row.get("object") or ""))
    ]
    if filtered:
        return filtered
    weak_descriptive = [
        row
        for row in evidence
        if str(row.get("relation_type") or "")
        in {
            "supplier_dependency",
            "manufacturing_dependency",
            "foundry_dependency",
            "packaging_or_assembly_dependency",
        }
        and matches_weak_object_pattern(str(row.get("canonical_object") or row.get("object") or ""))
        and not is_generic_object(str(row.get("canonical_object") or row.get("object") or ""))
        and not is_geography_object(str(row.get("canonical_object") or row.get("object") or ""))
        and not is_heading_object(str(row.get("canonical_object") or row.get("object") or ""))
    ]
    return weak_descriptive


def evidence_mentions_object(row: dict[str, Any]) -> bool:
    text = normalize_name(row.get("evidence_text", ""))
    return any(variant in text for variant in object_name_variants(row))


def local_object_context(row: dict[str, Any], window_chars: int = 220) -> str:
    text = normalize_name(row.get("evidence_text", ""))
    for variant in object_name_variants(row):
        for segment in re.split(r"\s+•\s+", text):
            if variant in segment:
                return trim_context_around_variant(segment, variant, window_chars)
        idx = text.find(variant)
        if idx >= 0:
            return trim_context_around_variant(text, variant, window_chars)
    return text[: window_chars * 2]


def trim_context_around_variant(text: str, variant: str, window_chars: int) -> str:
    idx = text.find(variant)
    if idx < 0:
        return text[: window_chars * 2]
    start = max(0, idx - window_chars)
    end = min(len(text), idx + len(variant) + window_chars)
    return text[start:end]


def object_name_variants(row: dict[str, Any]) -> list[str]:
    values = [
        str(row.get("canonical_object") or ""),
        str(row.get("object") or ""),
    ]
    variants: list[str] = []
    for value in values:
        normalized = normalize_name(value)
        simplified = simplify_legal_name(normalized)
        for candidate in [normalized, simplified]:
            if len(candidate) >= 4 and candidate not in variants:
                variants.append(candidate)
    return variants


def simplify_legal_name(value: str) -> str:
    cleaned = re.sub(
        r"\b(corporation|corp|incorporated|inc|company|co|limited|ltd|plc|n v|nv|ag|s a|sa|llc|l l c)\b\.?",
        "",
        value,
    )
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", cleaned)
    return " ".join(cleaned.split())


def is_negative_strategic_context(context: str) -> bool:
    return (
        ("shall not create" in context or "shall not itself create" in context)
        and ("joint venture" in context or "partnership" in context or "similar association" in context)
    ) or "not create a joint venture" in context


def is_trademark_context(context: str) -> bool:
    return "trademark" in context and not has_strong_dependency_cue(context)


def is_hypothetical_context(context: str) -> bool:
    hypothetical_cues = {
        "could materially adversely affect",
        "if we are unable",
        "if we fail",
        "if we lose",
        "if we otherwise fail",
        "may be affected",
        "may not",
        "would adversely affect",
    }
    if contains_any(context, hypothetical_cues) and not starts_with_current_dependency(context):
        return True
    early_context = context[:180]
    return (
        (early_context.startswith("if ") or " if " in early_context)
        and contains_any(early_context, {"adversely affect", "did not", "fail", "would"})
        and not starts_with_current_dependency(context)
    )


def is_competition_landscape_context(row: dict[str, Any], context: str) -> bool:
    full_text = normalize_name(row.get("evidence_text", ""))
    competition_cues = {"competition", "competitive", "competitors", "compete with"}
    list_cues = {"such as", "include such as", "consisting of"}
    if contains_any(context, competition_cues):
        return True
    return contains_any(full_text[:500], competition_cues) and contains_any(context, list_cues)


def is_list_only_context(context: str) -> bool:
    return contains_any(context, {"consisting of", "include such as", "including", "such as"})


def starts_with_current_dependency(context: str) -> bool:
    current_cues = {"we depend", "we rely", "we utilize", "we use", "we engage"}
    return contains_any(context[:180], current_cues)


def has_strong_dependency_cue(context: str) -> bool:
    strong_cues = {
        "depend on",
        "depends on",
        "engage with",
        "entered into",
        "license",
        "partnership",
        "procure",
        "rely on",
        "relies on",
        "signed an agreement",
        "strategic collaboration",
        "supplied by",
        "utilize",
        "we use",
    }
    return contains_any(context, strong_cues)


def has_risk_context(row: dict[str, Any]) -> bool:
    text = normalize_name(row.get("evidence_text", ""))
    context = local_object_context(row)
    return contains_any(context, RISK_CONTEXT_CUES) or contains_any(text[:900], RISK_CONTEXT_CUES)


def has_concentration_context(row: dict[str, Any]) -> bool:
    context = local_object_context(row)
    concentration_cues = {
        "10% or more",
        "accounted for",
        "concentrated",
        "concentration",
        "few customers",
        "limited number",
        "major customer",
        "significant portion",
        "substantial portion",
    }
    object_segments = object_local_segments(row, context)
    if object_segments:
        return any(contains_any(segment, concentration_cues) for segment in object_segments)
    return contains_any(context, concentration_cues)


def has_facility_or_geography_context(row: dict[str, Any]) -> bool:
    obj = str(row.get("canonical_object") or row.get("object") or "")
    if looks_like_legal_entity(obj):
        return False
    context = local_object_context(row)
    text = normalize_name(row.get("evidence_text", ""))[:1000]
    facility_cues = {
        "data center",
        "facilities",
        "facility",
        "geographic",
        "international",
        "located",
        "locations",
        "manufacturing",
        "operations",
        "region",
        "worldwide operations",
    }
    if is_geography_object(obj):
        return contains_any(context, facility_cues | RISK_CONTEXT_CUES) or contains_any(text, facility_cues | RISK_CONTEXT_CUES)
    return contains_any(context, facility_cues) or contains_any(text, facility_cues)


def object_local_segments(row: dict[str, Any], context: str) -> list[str]:
    variants = object_name_variants(row)
    segments = [segment.strip() for segment in re.split(r"[.;]\s*", context) if segment.strip()]
    return [segment for segment in segments if any(variant in segment for variant in variants)]


def is_credit_agreement_index_context(row: dict[str, Any], context: str) -> bool:
    section = normalize_name(row.get("source_section", ""))
    text = normalize_name(row.get("evidence_text", ""))[:1200]
    if "material_contract" in section or "exhibit_10" in section:
        return False
    return (
        "credit agreement" in text
        and "incorporated by reference" in text
        and not contains_any(context, {"cross-license", "license agreement", "desires to license"})
    )


def contains_any(value: str, needles: set[str]) -> bool:
    return any(needle in value for needle in needles)


def load_brief_inputs(run_dir: Path) -> dict[str, Any]:
    return {
        "companies": read_csv_rows(run_dir / "company_universe_resolved.csv"),
        "evidence": read_jsonl(run_dir / "relation_evidence.jsonl"),
        "edges": read_csv_rows(run_dir / "graph_edges.csv"),
        "source_documents": read_csv_rows(run_dir / "source_document_manifest.csv"),
        "filings": read_csv_rows(run_dir / "filing_manifest.csv"),
        "llm_selected_entities": read_csv_rows(run_dir / "entity_resolution_llm_selected.csv"),
    }


def read_csv_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def find_company(companies: list[dict[str, Any]], query: str) -> dict[str, Any]:
    normalized_query = normalize_lookup(query)
    for company in companies:
        candidates = [
            company.get("ticker", ""),
            company.get("cik", ""),
            company.get("company_name", ""),
        ]
        if any(normalize_lookup(value) == normalized_query for value in candidates):
            return dict(company)
    for company in companies:
        if normalized_query and normalized_query in normalize_lookup(company.get("company_name", "")):
            return dict(company)
    raise ValueError(f"Company not found in run universe: {query}")


def filter_company_evidence(evidence: list[dict[str, Any]], company: dict[str, Any]) -> list[dict[str, Any]]:
    ticker = normalize_lookup(company.get("ticker", ""))
    cik = normalize_cik(company.get("cik", ""))
    company_name = normalize_lookup(company.get("company_name", ""))
    rows = []
    for row in evidence:
        if normalize_lookup(row.get("ticker", "")) == ticker:
            rows.append(dict(row))
        elif normalize_cik(row.get("cik", "")) == cik and cik:
            rows.append(dict(row))
        elif normalize_lookup(row.get("subject", "")) == company_name and company_name:
            rows.append(dict(row))
    return rows


def load_selected_entity_map(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        if str(row.get("decision", "")).strip() != "select":
            continue
        key = normalize_name(row.get("query_object", ""))
        if key:
            selected[key] = row
    return selected


def enrich_evidence_object(row: dict[str, Any], selected_entities: dict[str, dict[str, Any]]) -> dict[str, Any]:
    enriched = dict(row)
    selection = selected_entities.get(normalize_name(row.get("object", "")))
    if selection:
        enriched["canonical_object"] = selection.get("selected_canonical_name") or row.get("object", "")
        enriched["object_lei"] = selection.get("selected_lei", "")
        enriched["object_jurisdiction"] = selection.get("selected_jurisdiction", "")
    else:
        enriched["canonical_object"] = row.get("object", "")
        enriched["object_lei"] = ""
        enriched["object_jurisdiction"] = ""
    return enriched


def build_claims(evidence: list[dict[str, Any]], id_prefix: str = "C") -> list[BriefClaim]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in dedupe_evidence(evidence):
        relation_type = str(row.get("relation_type", "")).strip()
        obj = str(row.get("canonical_object") or row.get("object") or "").strip()
        if relation_type and obj:
            grouped[(relation_type, obj)].append(row)
    claims = []
    for idx, ((relation_type, obj), rows) in enumerate(grouped.items(), start=1):
        confidences = [safe_float(row.get("confidence_score")) for row in rows]
        modalities = Counter(str(row.get("modality", "")) for row in rows if row.get("modality"))
        forms = Counter(str(row.get("form", "")) for row in rows if row.get("form"))
        accessions = sorted({str(row.get("accession_number", "")) for row in rows if row.get("accession_number")})
        dates = sorted(str(row.get("filing_date", "")) for row in rows if row.get("filing_date"))
        claim_id = f"{id_prefix}{idx:03d}"
        representative = sorted(
            rows,
            key=lambda row: (
                passage_quality_score(row),
                safe_float(row.get("confidence_score")),
                str(row.get("filing_date", "")),
            ),
            reverse=True,
        )[:3]
        claims.append(
            BriefClaim(
                claim_id=claim_id,
                category=claim_category(relation_type, modalities),
                relation_type=relation_type,
                object=str(rows[0].get("object", "")),
                canonical_object=obj,
                object_lei=str(rows[0].get("object_lei", "")),
                modality_mix="; ".join(name for name, _ in modalities.most_common()),
                evidence_count=len(rows),
                avg_confidence=round(sum(confidences) / len(confidences), 3) if confidences else 0.0,
                forms="; ".join(name for name, _ in forms.most_common()),
                accessions="; ".join(accessions[:8]),
                first_seen=dates[0] if dates else "",
                last_seen=dates[-1] if dates else "",
                representative_evidence_ids=[evidence_id(row) for row in representative],
            )
        )
    return claims


def dedupe_evidence(evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str, str, str]] = set()
    rows = []
    for row in evidence:
        key = (
            str(row.get("relation_type", "")),
            normalize_name(row.get("object", "")),
            str(row.get("modality", "")),
            str(row.get("accession_number", "")),
            str(row.get("paragraph_offset", "")),
            normalize_name(str(row.get("evidence_text", ""))[:180]),
        )
        if key in seen:
            continue
        seen.add(key)
        rows.append(row)
    return rows


def claim_category(relation_type: str, modalities: Counter[str]) -> str:
    modality_text = " ".join(modalities.keys())
    if relation_type in STRATEGIC_RELATION_TYPES or "strategic" in modality_text:
        return "strategic_relation"
    if relation_type in RISK_RELATION_TYPES or "risk" in modality_text or "hypothetical" in modality_text:
        return "risk_exposure"
    if relation_type in OPERATING_RELATION_TYPES:
        return "operating_dependency"
    return "other"


def top_claims(claims: list[BriefClaim], limit: int) -> list[BriefClaim]:
    ordered = sorted(
        claims,
        key=lambda claim: (
            is_generic_object(claim.canonical_object),
            -claim.evidence_count,
            -claim.avg_confidence,
            claim.relation_type,
            claim.canonical_object.lower(),
        ),
    )
    return ordered[:limit]


def build_evidence_table(
    evidence: list[dict[str, Any]],
    selected_claims: list[BriefClaim],
    max_rows: int,
    max_chars: int,
) -> list[EvidenceRow]:
    unique_claims = dedupe_claims(selected_claims)
    claim_lookup = {(claim.relation_type, normalize_name(claim.canonical_object)): claim for claim in unique_claims}
    source_rows = dedupe_evidence(evidence)
    rows_by_key: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        key = (str(row.get("relation_type", "")), normalize_name(row.get("canonical_object") or row.get("object", "")))
        rows_by_key[key].append(row)
    for group in rows_by_key.values():
        group.sort(
            key=lambda item: (
                passage_quality_score(item),
                safe_float(item.get("confidence_score")),
                str(item.get("filing_date", "")),
            ),
            reverse=True,
        )
    rows: list[EvidenceRow] = []
    seen_ids: set[str] = set()

    def append_row(row: dict[str, Any]) -> None:
        if len(rows) >= max_rows:
            return
        key = (str(row.get("relation_type", "")), normalize_name(row.get("canonical_object") or row.get("object", "")))
        claim = claim_lookup.get(key)
        if not claim:
            return
        row_id = evidence_id(row)
        if row_id in seen_ids:
            return
        seen_ids.add(row_id)
        rows.append(evidence_row_from_dict(row, claim, row_id, max_chars))

    for claim in unique_claims:
        key = (claim.relation_type, normalize_name(claim.canonical_object))
        if rows_by_key.get(key):
            append_row(rows_by_key[key][0])

    for row in sorted(
        source_rows,
        key=lambda item: (
            passage_quality_score(item),
            safe_float(item.get("confidence_score")),
            str(item.get("filing_date", "")),
        ),
        reverse=True,
    ):
        append_row(row)
        if len(rows) >= max_rows:
            break
    return rows


def passage_quality_score(row: dict[str, Any]) -> float:
    text = normalize_name(row.get("evidence_text", ""))
    obj = normalize_name(row.get("canonical_object") or row.get("object", ""))
    section = normalize_name(row.get("source_section", ""))
    score = 0.0
    if obj and obj in text:
        score += 2.0
    if section_contains_core_disclosure(section):
        score += 0.8
    if row.get("modality") == "current_fact":
        score += 0.4
    if starts_with_boilerplate(text):
        score -= 2.0
    if looks_like_financial_table_fragment(text):
        score -= 1.0
    if looks_like_exhibit_index_fragment(text):
        score -= 2.5
    if is_material_contract_clause(row, text):
        score += 1.0
    if obj and is_low_quality_display_object(obj):
        score -= 0.8
    return score


def section_contains_core_disclosure(section: str) -> bool:
    return any(token in section for token in ["business", "mdna", "risk_factors", "material_contract"])


def starts_with_boilerplate(text: str) -> bool:
    return text.startswith("table of contents") or text.startswith("item 1a risk factors") or text.startswith("please carefully consider")


def looks_like_financial_table_fragment(text: str) -> bool:
    if not text:
        return False
    dollar_count = text.count("$")
    percent_count = text.count("%")
    return dollar_count + percent_count >= 5


def looks_like_exhibit_index_fragment(text: str) -> bool:
    exhibit_refs = len(re.findall(r"\b10\.\d+\b", text))
    return (
        "incorporated by reference" in text
        or ("filed as exhibit" in text and exhibit_refs >= 1)
        or exhibit_refs >= 4
    )


def is_material_contract_clause(row: dict[str, Any], text: str) -> bool:
    section = normalize_name(row.get("source_section", ""))
    relation_type = str(row.get("relation_type") or "")
    if "material_contract" not in section and "exhibit_10" not in section:
        return False
    if relation_type == "licensing_dependency":
        return contains_any(text[:900], {"cross-license agreement", "license agreement", "desires to license"})
    return contains_any(text[:700], {"agreement", "entered into", "whereas"})


def dedupe_claims(claims: list[BriefClaim]) -> list[BriefClaim]:
    seen: set[tuple[str, str]] = set()
    unique = []
    for claim in claims:
        key = (claim.relation_type, normalize_name(claim.canonical_object))
        if key in seen:
            continue
        seen.add(key)
        unique.append(claim)
    return unique


def evidence_row_from_dict(row: dict[str, Any], claim: BriefClaim, row_id: str, max_chars: int) -> EvidenceRow:
    return EvidenceRow(
        evidence_id=row_id,
        claim_id=claim.claim_id,
        relation_type=str(row.get("relation_type", "")),
        modality=str(row.get("modality", "")),
        subject=str(row.get("subject", "")),
        object=str(row.get("object", "")),
        canonical_object=str(row.get("canonical_object") or row.get("object", "")),
        confidence_score=round(safe_float(row.get("confidence_score")), 3),
        certainty=str(row.get("certainty", "")),
        form=str(row.get("form", "")),
        filing_date=str(row.get("filing_date", "")),
        accession_number=str(row.get("accession_number", "")),
        section=str(row.get("source_section", "")),
        paragraph_offset=safe_int(row.get("paragraph_offset")),
        source_document_url=str(row.get("source_document_url", "")),
        evidence_text=evidence_excerpt(row, max_chars),
    )


def evidence_excerpt(row: dict[str, Any], max_chars: int) -> str:
    clean = " ".join(str(row.get("evidence_text", "")).split())
    if len(clean) <= max_chars:
        return clean
    anchor = evidence_excerpt_anchor(clean, row)
    if anchor < 0:
        return truncate_text(clean, max_chars)
    return centered_truncate(clean, anchor, max_chars)


def evidence_excerpt_anchor(clean_text: str, row: dict[str, Any]) -> int:
    lowered = clean_text.casefold()
    for value in [str(row.get("object") or ""), str(row.get("canonical_object") or "")]:
        value = value.strip()
        if not value:
            continue
        idx = lowered.find(value.casefold())
        if idx >= 0:
            return idx
        simplified = simplify_legal_name(normalize_name(value))
        if len(simplified) >= 4:
            match = re.search(rf"\b{re.escape(simplified)}\b", lowered)
            if match:
                return match.start()
    return -1


def centered_truncate(value: str, anchor: int, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    half = max_chars // 2
    start = max(0, anchor - half)
    end = min(len(value), start + max_chars)
    start = max(0, end - max_chars)
    snippet = value[start:end].strip()
    if start > 0:
        snippet = "..." + snippet[3:].lstrip()
    if end < len(value):
        snippet = snippet[:-3].rstrip() + "..."
    return snippet


def infer_company_role(company: dict[str, Any], claims: list[BriefClaim]) -> dict[str, Any]:
    relation_counts = Counter(claim.relation_type for claim in claims for _ in range(max(1, claim.evidence_count)))
    return {
        "declared_role": company.get("role", ""),
        "company_notes": company.get("notes", ""),
        "dominant_relation_types": dict(relation_counts.most_common(8)),
        "brief_role_label": company.get("role", "") or infer_role_from_relations(relation_counts),
    }


def infer_role_from_relations(relation_counts: Counter[str]) -> str:
    if relation_counts.get("cloud_or_hosting_dependency") or relation_counts.get("data_center_dependency"):
        return "ai_infrastructure_cloud_or_data_center"
    if relation_counts.get("foundry_dependency") or relation_counts.get("packaging_or_assembly_dependency"):
        return "semiconductor_or_compute_supply_chain"
    if relation_counts.get("power_or_utility_dependency"):
        return "power_or_utility_infrastructure"
    return "ai_value_chain_company"


def generate_analyst_interpretation(
    company: dict[str, Any],
    role: dict[str, Any],
    top_operating: list[BriefClaim],
    top_risk: list[BriefClaim],
    current_fact: list[BriefClaim],
    strategic: list[BriefClaim],
    evidence_table: list[EvidenceRow],
    llm_client: Any | None,
    model_version: str,
    max_tokens: int,
) -> dict[str, Any]:
    fallback = deterministic_interpretation(company, top_operating, top_risk, current_fact, strategic)
    if llm_client is None:
        return fallback
    writer_payload = build_interpretation_payload(
        company=company,
        role=role,
        top_operating=top_operating,
        top_risk=top_risk,
        current_fact=current_fact,
        strategic=strategic,
        evidence_table=evidence_table,
    )
    allowed_ids = {row.evidence_id for row in evidence_table}
    rounds = []
    try:
        outline_raw = llm_client.chat_json(
            BRIEF_OUTLINE_SYSTEM_PROMPT,
            writer_payload,
            max_tokens=max(1200, min(max_tokens, 1800)),
        )
        rounds.append("outline_planning")
        outline = normalize_outline(outline_raw, allowed_ids)
        final_payload = build_final_writer_payload(writer_payload, outline)
        raw = llm_client.chat_json(BRIEF_FINAL_SYSTEM_PROMPT, final_payload, max_tokens=max_tokens)
        rounds.append("final_writing")
    except Exception as exc:
        fallback["generation_error"] = str(exc)[:500]
        return fallback
    if not isinstance(raw, dict):
        fallback["generation_error"] = f"LLM returned {type(raw).__name__}, expected object"
        return fallback
    interpretation = normalize_interpretation(raw, fallback, model_version)
    interpretation["outline"] = outline
    invalid = invalid_citations(interpretation, allowed_ids)
    uncited = uncited_interpretation_items(interpretation)
    if invalid or uncited:
        repaired = repair_interpretation_citations(
            llm_client=llm_client,
            interpretation=interpretation,
            outline=outline,
            writer_payload=writer_payload,
            invalid=invalid,
            uncited=uncited,
            allowed_ids=allowed_ids,
            fallback=fallback,
            model_version=model_version,
        )
        rounds.append("citation_repair")
        interpretation = repaired
        if invalid_citations(interpretation, allowed_ids) or uncited_interpretation_items(interpretation):
            interpretation = enforce_citation_constraints(interpretation, allowed_ids)
    final_invalid = invalid_citations(interpretation, allowed_ids)
    final_uncited = uncited_interpretation_items(interpretation)
    if final_invalid:
        interpretation.setdefault("citation_warnings", []).append(
            f"LLM cited ids not present in the evidence table: {', '.join(final_invalid[:8])}"
        )
    if final_uncited:
        interpretation.setdefault("citation_warnings", []).append(
            f"LLM left uncited interpretation fields: {', '.join(final_uncited[:8])}"
        )
    cited = valid_citations(interpretation, allowed_ids)
    if not cited:
        interpretation.setdefault("citation_warnings", []).append("LLM produced no valid evidence citations.")
    interpretation["valid_citations"] = cited
    support_warnings = validate_interpretation_support(interpretation, evidence_table)
    if support_warnings:
        interpretation["support_warnings"] = support_warnings
    interpretation["generation_rounds"] = rounds + ["citation_validation"]
    return interpretation


BRIEF_OUTLINE_SYSTEM_PROMPT = """You are an outline planner for a disclosure-derived dependency brief.
Use only the supplied structured dependency evidence. Do not invent counterparties, market facts, financial numbers, or causal links.
Distinguish current facts, strategic relationships, forward-looking statements, and risk-hypothetical language.
Use only evidence ids from allowed_evidence_ids. Do not cite claim ids such as C001, F001, R001, or S001.
Create the outline only; do not write the final brief.
Each outline item must include 1-2 evidence_ids copied exactly from allowed_evidence_ids.
Return at most 2 items per array. Keep each point under 22 words.
Return one compact JSON object:
{
  "dependency_thesis": [{"point":"string","evidence_ids":["E..."],"strength":"high|medium|low"}],
  "risk_focus": [{"point":"string","evidence_ids":["E..."],"strength":"high|medium|low"}],
  "monitoring_plan": [{"point":"string","evidence_ids":["E..."],"strength":"high|medium|low"}],
  "evidence_limits": [{"point":"string","evidence_ids":["E..."],"strength":"high|medium|low"}]
}"""


BRIEF_FINAL_SYSTEM_PROMPT = """You are a Seeking Alpha style equity analyst and NLP evidence reviewer.
Write for an ETF portfolio manager who wants investable dependency intelligence, not a raw graph.
Use the supplied outline and evidence table. Do not invent facts, counterparties, market numbers, or causal links.
Every paragraph or bullet must cite at least one evidence id copied exactly from allowed_evidence_ids.
Never cite claim ids such as C001, F001, R001, or S001.
Distinguish current facts from forward-looking or risk-hypothetical language.
Do not infer mitigation, resilience, diversification benefits, or causal financial impact unless the cited evidence explicitly says so.
Avoid strong causal wording such as "hinges", "validates", "reduces execution risk", "revenue potential", or "directly limits" unless cited current-fact evidence names a specific counterparty.
If a relation type or object looks ambiguous, discuss it under weak_or_missing_evidence instead of treating it as a firm dependency.
Keep the paragraph under 130 words and each bullet under 45 words.
Return 2-3 bullets per list.
Use parenthetical evidence citations, for example: (E000021015FOU98).
Return one compact JSON object:
{
  "one_paragraph_summary": "string with evidence ids",
  "what_this_implies": ["bullet with evidence id", "bullet with evidence id"],
  "what_to_monitor": ["bullet with evidence id", "bullet with evidence id"],
  "weak_or_missing_evidence": ["bullet with evidence id", "bullet with evidence id"]
}"""


BRIEF_REPAIR_SYSTEM_PROMPT = """You repair a dependency brief JSON object.
Return the same JSON schema as the draft, but remove or replace invalid citations.
Use only evidence ids copied exactly from allowed_evidence_ids. Do not cite claim ids.
Do not add new facts. If a sentence cannot be supported by allowed evidence ids, rewrite it as weak evidence or remove it.
Return one compact JSON object only."""


def build_interpretation_payload(
    company: dict[str, Any],
    role: dict[str, Any],
    top_operating: list[BriefClaim],
    top_risk: list[BriefClaim],
    current_fact: list[BriefClaim],
    strategic: list[BriefClaim],
    evidence_table: list[EvidenceRow],
) -> str:
    payload = {
        "company": company,
        "company_role": role,
        "top_operating_dependencies": [claim_payload(claim) for claim in top_operating],
        "top_risk_exposures": [claim_payload(claim) for claim in top_risk],
        "current_fact_edges": [claim_payload(claim) for claim in current_fact],
        "strategic_relations": [claim_payload(claim) for claim in strategic],
        "allowed_evidence_ids": [row.evidence_id for row in evidence_table],
        "evidence_table": [evidence_payload(row) for row in evidence_table],
        "instructions": [
            "Focus on operational dependencies, concentration, bottlenecks, and monitoring signals.",
            "Cite only evidence ids that appear in allowed_evidence_ids.",
            "Do not cite claim ids such as C001, F001, R001, or S001.",
            "Do not treat risk_hypothetical evidence as a current operating dependency.",
            "Flag weak evidence where objects are generic classes or confidence is low.",
        ],
    }
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def build_final_writer_payload(writer_payload: str, outline: dict[str, Any]) -> str:
    payload = json.loads(writer_payload)
    payload["outline"] = outline
    payload["writing_stage"] = "final_from_outline"
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


def normalize_interpretation(raw: dict[str, Any], fallback: dict[str, Any], model_version: str) -> dict[str, Any]:
    return {
        "one_paragraph_summary": str(raw.get("one_paragraph_summary") or fallback["one_paragraph_summary"])[:1600],
        "what_this_implies": normalize_bullets(raw.get("what_this_implies"), fallback["what_this_implies"]),
        "what_to_monitor": normalize_bullets(raw.get("what_to_monitor"), fallback["what_to_monitor"]),
        "weak_or_missing_evidence": normalize_bullets(
            raw.get("weak_or_missing_evidence"),
            fallback["weak_or_missing_evidence"],
        ),
        "model_version": model_version,
    }


def normalize_outline(raw: Any, allowed_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return deterministic_outline()
    return {
        "dependency_thesis": normalize_outline_items(raw.get("dependency_thesis"), allowed_ids),
        "risk_focus": normalize_outline_items(raw.get("risk_focus"), allowed_ids),
        "monitoring_plan": normalize_outline_items(raw.get("monitoring_plan"), allowed_ids),
        "evidence_limits": normalize_outline_items(raw.get("evidence_limits"), allowed_ids),
    }


def normalize_outline_items(value: Any, allowed_ids: set[str]) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    items = []
    for item in value[:6]:
        if isinstance(item, dict):
            point = str(item.get("point") or "").strip()
            evidence_ids = normalize_evidence_ids(item.get("evidence_ids"), allowed_ids)
            strength = str(item.get("strength") or "medium").strip().lower()
        else:
            point = str(item).strip()
            evidence_ids = []
            strength = "medium"
        if point:
            items.append(
                {
                    "point": point[:500],
                    "evidence_ids": evidence_ids,
                    "strength": strength if strength in {"high", "medium", "low"} else "medium",
                }
            )
    return items


def normalize_evidence_ids(value: Any, allowed_ids: set[str]) -> list[str]:
    if isinstance(value, str):
        candidates = re.findall(r"\bE[0-9A-Z]{8,}\b", value)
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    clean = []
    for item in candidates:
        if item in allowed_ids and item not in clean:
            clean.append(item)
    return clean[:3]


def deterministic_outline() -> dict[str, Any]:
    return {
        "dependency_thesis": [],
        "risk_focus": [],
        "monitoring_plan": [],
        "evidence_limits": [],
    }


def claim_payload(claim: BriefClaim) -> dict[str, Any]:
    data = claim.to_dict()
    data.pop("representative_evidence_ids", None)
    data.pop("claim_id", None)
    return data


def evidence_payload(row: EvidenceRow) -> dict[str, Any]:
    data = row.to_dict()
    data.pop("claim_id", None)
    return data


def invalid_citations(interpretation: dict[str, Any], allowed_evidence_ids: set[str]) -> list[str]:
    text = json.dumps(interpretation, ensure_ascii=False)
    cited_evidence = set(re.findall(r"\bE[0-9A-Z]{8,}\b", text))
    cited_claims = set(re.findall(r"\b[CRFS]\d{3}\b", text))
    invalid = sorted(cited_claims | (cited_evidence - allowed_evidence_ids))
    return invalid


def valid_citations(interpretation: dict[str, Any], allowed_evidence_ids: set[str]) -> list[str]:
    text = json.dumps(interpretation, ensure_ascii=False)
    cited_evidence = set(re.findall(r"\bE[0-9A-Z]{8,}\b", text))
    return sorted(cited_evidence & allowed_evidence_ids)


def uncited_interpretation_items(interpretation: dict[str, Any]) -> list[str]:
    uncited = []
    if not has_evidence_citation(str(interpretation.get("one_paragraph_summary") or "")):
        uncited.append("one_paragraph_summary")
    for field in ["what_this_implies", "what_to_monitor", "weak_or_missing_evidence"]:
        value = interpretation.get(field)
        if not isinstance(value, list):
            uncited.append(field)
            continue
        for idx, item in enumerate(value):
            if not has_evidence_citation(str(item)):
                uncited.append(f"{field}[{idx}]")
    return uncited


def has_evidence_citation(value: str) -> bool:
    return bool(re.search(r"\bE[0-9A-Z]{8,}\b", value))


def validate_interpretation_support(interpretation: dict[str, Any], evidence_table: list[EvidenceRow]) -> list[str]:
    evidence_by_id = {row.evidence_id: row for row in evidence_table}
    warnings = []
    for field, text in interpretation_text_items(interpretation):
        lowered = normalize_name(text)
        if not contains_any(lowered, OVERCLAIM_PHRASES):
            continue
        cited_rows = [evidence_by_id[eid] for eid in re.findall(r"\bE[0-9A-Z]{8,}\b", text) if eid in evidence_by_id]
        if not any(is_strong_current_named_evidence(row) for row in cited_rows):
            warnings.append(f"{field}: strong wording is not backed by current-fact named-counterparty evidence")
    return warnings


OVERCLAIM_PHRASES = {
    "directly limits",
    "hinges",
    "immediately halt",
    "operational viability",
    "reduces execution risk",
    "revenue potential",
    "single-point failure",
    "validates",
}


def interpretation_text_items(interpretation: dict[str, Any]) -> list[tuple[str, str]]:
    items = [("one_paragraph_summary", str(interpretation.get("one_paragraph_summary") or ""))]
    for field in ["what_this_implies", "what_to_monitor", "weak_or_missing_evidence"]:
        value = interpretation.get(field)
        if isinstance(value, list):
            items.extend((f"{field}[{idx}]", str(item)) for idx, item in enumerate(value))
        else:
            items.append((field, str(value or "")))
    return items


def is_strong_current_named_evidence(row: EvidenceRow) -> bool:
    return (
        row.modality == "current_fact"
        and row.relation_type in OPERATING_RELATION_TYPES
        and not is_low_quality_display_object(row.canonical_object)
    )


def repair_interpretation_citations(
    llm_client: Any,
    interpretation: dict[str, Any],
    outline: dict[str, Any],
    writer_payload: str,
    invalid: list[str],
    uncited: list[str],
    allowed_ids: set[str],
    fallback: dict[str, Any],
    model_version: str,
) -> dict[str, Any]:
    payload = json.loads(writer_payload)
    payload["draft_interpretation"] = interpretation
    payload["outline"] = outline
    payload["invalid_citations"] = invalid
    payload["uncited_fields"] = uncited
    payload["allowed_evidence_ids"] = sorted(allowed_ids)
    try:
        raw = llm_client.chat_json(
            BRIEF_REPAIR_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False, sort_keys=True),
            max_tokens=1200,
        )
    except Exception as exc:
        interpretation["citation_repair_error"] = str(exc)[:500]
        return interpretation
    if not isinstance(raw, dict):
        interpretation["citation_repair_error"] = f"Repair returned {type(raw).__name__}, expected object"
        return interpretation
    repaired = normalize_interpretation(raw, fallback, model_version)
    repaired["outline"] = outline
    repaired["citation_repair_attempted"] = True
    return repaired


def enforce_citation_constraints(interpretation: dict[str, Any], allowed_ids: set[str]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(interpretation, ensure_ascii=False))
    for invalid in invalid_citations(cleaned, allowed_ids):
        replacement = nearest_allowed_evidence_id(invalid, allowed_ids) if invalid.startswith("E") else ""
        cleaned = replace_citation_value(cleaned, invalid, replacement)
    fallback_id = next(iter(valid_citations(cleaned, allowed_ids)), "")
    if not fallback_id and allowed_ids:
        fallback_id = sorted(allowed_ids)[0]
    if fallback_id:
        if not has_evidence_citation(str(cleaned.get("one_paragraph_summary") or "")):
            cleaned["one_paragraph_summary"] = append_citation(str(cleaned.get("one_paragraph_summary") or ""), fallback_id)
        for field in ["what_this_implies", "what_to_monitor", "weak_or_missing_evidence"]:
            value = cleaned.get(field)
            if not isinstance(value, list):
                cleaned[field] = [append_citation(str(value or "Evidence is insufficiently specific."), fallback_id)]
                continue
            cleaned[field] = [
                item if has_evidence_citation(str(item)) else append_citation(str(item), fallback_id)
                for item in value
            ]
    cleaned["deterministic_citation_cleanup"] = True
    return cleaned


def nearest_allowed_evidence_id(invalid: str, allowed_ids: set[str]) -> str:
    if len(invalid) >= 3:
        prefix = invalid[:-2]
        matches = sorted(item for item in allowed_ids if item.startswith(prefix))
        if matches:
            return matches[0]
    return ""


def replace_citation_value(value: Any, invalid: str, replacement: str) -> Any:
    if isinstance(value, str):
        return cleanup_citation_text(value.replace(invalid, replacement))
    if isinstance(value, list):
        return [replace_citation_value(item, invalid, replacement) for item in value]
    if isinstance(value, dict):
        return {key: replace_citation_value(item, invalid, replacement) for key, item in value.items()}
    return value


def append_citation(text: str, evidence_id: str) -> str:
    text = text.strip() or "Evidence is weak or underspecified."
    return f"{text.rstrip('.')} ({evidence_id})."


def cleanup_citation_text(text: str) -> str:
    text = re.sub(r"\s+", " ", text)
    text = re.sub(r"\(\s*,\s*", "(", text)
    text = re.sub(r",\s*\)", ")", text)
    text = re.sub(r"\(\s*\)", "", text)
    text = re.sub(r"\s+([,.;:])", r"\1", text)
    return text.strip()


def parse_lenient_json_content(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = content.strip("`")
        content = content.removeprefix("json").strip()
    start = min([idx for idx in [content.find("{"), content.find("[")] if idx >= 0], default=0)
    end = max(content.rfind("}"), content.rfind("]"))
    if end > start:
        content = content[start : end + 1]
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        sanitized = re.sub(r"[\x00-\x1f]+", " ", content)
        return json.loads(sanitized)


def deterministic_interpretation(
    company: dict[str, Any],
    top_operating: list[BriefClaim],
    top_risk: list[BriefClaim],
    current_fact: list[BriefClaim],
    strategic: list[BriefClaim],
) -> dict[str, Any]:
    name = company.get("company_name") or company.get("ticker") or "Company"
    operating_types = ", ".join(claim.relation_type for claim in top_operating[:4]) or "no high-signal operating dependency"
    return {
        "one_paragraph_summary": (
            f"{name} has {len(top_operating)} operating dependency claim groups, "
            f"{len(top_risk)} risk exposure claim groups, {len(current_fact)} current-fact groups, "
            f"and {len(strategic)} strategic relation groups in this run. The strongest operating themes are: {operating_types}."
        ),
        "what_this_implies": [
            "Treat the brief as disclosure-derived dependency intelligence; every claim should be checked against the evidence table.",
            "Prioritize current_fact claims over forward-looking or risk-hypothetical claims when mapping actual operating dependencies.",
        ],
        "what_to_monitor": [
            "Watch new 10-K, 10-Q, 8-K, 20-F, 6-K, and material exhibits for changes in named counterparties and concentration language.",
            "Monitor whether generic class dependencies become named counterparties in later filings.",
        ],
        "weak_or_missing_evidence": [
            "Generic objects and class-level dependencies are useful recall signals but weak entity-resolution targets.",
            "Risk-hypothetical disclosures indicate exposure language, not necessarily an active dependency.",
        ],
        "model_version": "deterministic",
    }


def normalize_bullets(value: Any, fallback: list[str]) -> list[str]:
    if not isinstance(value, list):
        return fallback
    bullets = [str(item).strip()[:700] for item in value if str(item).strip()]
    return bullets[:6] or fallback


def write_company_dependency_brief(
    brief: CompanyDependencyBrief,
    output_dir: Path,
    basename: str | None = None,
) -> dict[str, str]:
    ticker = str(brief.company.get("ticker") or brief.company.get("company_name") or "company")
    stem = basename or f"{safe_filename(ticker)}_dependency_brief"
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / f"{stem}.json"
    markdown_path = output_dir / f"{stem}.md"
    write_json(json_path, brief.to_dict())
    markdown_path.write_text(render_markdown_brief(brief), encoding="utf-8")
    return {"json": str(json_path), "markdown": str(markdown_path)}


def render_markdown_brief(brief: CompanyDependencyBrief) -> str:
    company_name = brief.company.get("company_name") or brief.company.get("ticker") or "Company"
    lines = [f"# Company Dependency Brief: {company_name}", ""]
    lines.extend(
        [
            "## 1. Company role",
            "",
            f"- Declared role: `{brief.company_role.get('declared_role') or brief.company_role.get('brief_role_label')}`",
            f"- Notes: {brief.company_role.get('company_notes') or 'n/a'}",
            f"- Dominant relation types: {format_relation_counts(brief.company_role.get('dominant_relation_types', {}))}",
            "",
        ]
    )
    append_claim_section(lines, "## 2. Top operating dependencies", brief.top_operating_dependencies)
    append_claim_section(lines, "## 3. Top risk exposures", brief.top_risk_exposures)
    append_claim_section(lines, "## 4. Current-fact edges", brief.current_fact_edges)
    append_claim_section(lines, "## 5. Strategic relations", brief.strategic_relations)
    lines.extend(["## 6. Evidence table", ""])
    if brief.evidence_table:
        lines.append("| ID | Claim | Relation | Modality | Form | Filing date | Accession | Section | Para | Evidence | SEC URL |")
        lines.append("|---|---|---|---|---|---|---|---|---:|---|---|")
        for row in brief.evidence_table:
            lines.append(
                "| "
                + " | ".join(
                    [
                        escape_md(row.evidence_id),
                        escape_md(row.claim_id),
                        escape_md(row.relation_type),
                        escape_md(row.modality),
                        escape_md(row.form),
                        escape_md(row.filing_date),
                        escape_md(row.accession_number),
                        escape_md(row.section),
                        str(row.paragraph_offset),
                        escape_md(row.evidence_text),
                        f"[SEC]({row.source_document_url})" if row.source_document_url else "",
                    ]
                )
                + " |"
            )
    else:
        lines.append("_No evidence rows found for this company in the selected run._")
    lines.extend(["", "## 7. Analyst interpretation", ""])
    interpretation = brief.analyst_interpretation
    append_outline_section(lines, interpretation.get("outline"))
    lines.append(str(interpretation.get("one_paragraph_summary", "")))
    lines.append("")
    append_bullet_section(lines, "What this implies", interpretation.get("what_this_implies", []))
    append_bullet_section(lines, "What to monitor", interpretation.get("what_to_monitor", []))
    append_bullet_section(lines, "What evidence is weak", interpretation.get("weak_or_missing_evidence", []))
    lines.extend(
        [
            "",
            "## Diagnostics",
            "",
            f"- Run dir: `{brief.run_dir}`",
            f"- Model: `{brief.model_version or brief.analyst_interpretation.get('model_version', '')}`",
            f"- Company evidence rows: `{brief.diagnostics.get('company_evidence_rows')}`",
            f"- Claim groups: `{brief.diagnostics.get('claim_count')}`",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def append_claim_section(lines: list[str], heading: str, claims: list[BriefClaim]) -> None:
    lines.extend([heading, ""])
    if not claims:
        lines.extend(["_No high-signal claims in this section._", ""])
        return
    for claim in claims:
        lei = f", LEI `{claim.object_lei}`" if claim.object_lei else ""
        lines.append(
            f"- `{claim.relation_type}` -> **{claim.canonical_object}**{lei}; "
            f"evidence={claim.evidence_count}, avg_conf={claim.avg_confidence}, "
            f"modality={claim.modality_mix or 'n/a'}, forms={claim.forms or 'n/a'}"
        )
    lines.append("")


def append_outline_section(lines: list[str], outline: Any) -> None:
    if not isinstance(outline, dict):
        return
    lines.extend(["### Writing outline", ""])
    labels = [
        ("dependency_thesis", "Dependency thesis"),
        ("risk_focus", "Risk focus"),
        ("monitoring_plan", "Monitoring plan"),
        ("evidence_limits", "Evidence limits"),
    ]
    wrote = False
    for key, label in labels:
        items = outline.get(key)
        if not isinstance(items, list) or not items:
            continue
        wrote = True
        lines.append(f"**{label}**")
        for item in items:
            if not isinstance(item, dict):
                continue
            evidence_ids = ", ".join(str(eid) for eid in item.get("evidence_ids", []))
            suffix = f" ({evidence_ids})" if evidence_ids else ""
            lines.append(f"- {item.get('point', '')}{suffix}")
        lines.append("")
    if wrote:
        lines.append("### Final interpretation")
        lines.append("")


def append_bullet_section(lines: list[str], heading: str, bullets: list[str]) -> None:
    lines.append(f"### {heading}")
    lines.append("")
    if not bullets:
        lines.append("- n/a")
    else:
        for bullet in bullets:
            lines.append(f"- {bullet}")
    lines.append("")


def format_relation_counts(counts: dict[str, Any]) -> str:
    if not counts:
        return "n/a"
    return ", ".join(f"`{key}`={value}" for key, value in list(counts.items())[:8])


def evidence_id(row: dict[str, Any]) -> str:
    accession = str(row.get("accession_number", "")).replace("-", "")[-6:] or "000000"
    paragraph = safe_int(row.get("paragraph_offset"))
    relation = safe_relation_code(row.get("relation_type", "rel"))
    confidence = int(round(safe_float(row.get("confidence_score")) * 100))
    return f"E{accession}{paragraph:03d}{relation}{confidence:02d}"


def safe_relation_code(value: Any) -> str:
    code = re.sub(r"[^A-Z0-9]", "", str(value or "REL").upper())
    return (code[:3] or "REL").ljust(3, "X")


def is_generic_object(value: str) -> bool:
    normalized = normalize_name(value)
    if not normalized:
        return True
    if normalized.endswith(" class") or " class " in normalized:
        return True
    return any(term in normalized for term in GENERIC_OBJECT_TERMS)


def is_low_quality_operating_object(value: str) -> bool:
    return is_low_quality_display_object(value)


def is_low_quality_display_object(value: str) -> bool:
    return (
        is_generic_object(value)
        or is_geography_object(value)
        or is_heading_object(value)
        or is_placeholder_object(value)
        or matches_weak_object_pattern(value)
    )


def is_geography_object(value: str) -> bool:
    return normalize_name(value) in GEOGRAPHY_OBJECTS


def is_heading_object(value: str) -> bool:
    normalized = normalize_name(value)
    return normalized in HEADING_OBJECTS or normalized.startswith("item ")


def matches_weak_object_pattern(value: str) -> bool:
    normalized = normalize_name(value)
    return any(pattern in normalized for pattern in WEAK_OBJECT_PATTERNS)


def is_placeholder_object(value: str) -> bool:
    normalized = normalize_name(value)
    if normalized in PLACEHOLDER_OBJECTS:
        return True
    if re.fullmatch(r"(direct )?customer [a-z]", normalized):
        return True
    if re.fullmatch(r"(product|customer|supplier|partner|competitor)s?", normalized):
        return True
    return False


def looks_like_legal_entity(value: str) -> bool:
    normalized = normalize_name(value)
    legal_terms = {
        "ag",
        "corp",
        "corporation",
        "inc",
        "incorporated",
        "limited",
        "llc",
        "ltd",
        "n v",
        "nv",
        "plc",
    }
    return any(re.search(rf"\b{re.escape(term)}\b", normalized) for term in legal_terms)


def normalize_lookup(value: Any) -> str:
    return str(value or "").strip().casefold()


def normalize_cik(value: Any) -> str:
    digits = "".join(ch for ch in str(value or "") if ch.isdigit())
    return digits.lstrip("0") or digits


def normalize_name(value: Any) -> str:
    return " ".join(str(value or "").casefold().replace("&", " and ").split())


def safe_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def safe_int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def truncate_text(value: str, max_chars: int) -> str:
    clean = " ".join(value.split())
    if len(clean) <= max_chars:
        return clean
    return clean[: max(0, max_chars - 3)].rstrip() + "..."


def safe_filename(value: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    return safe.strip("_") or "company"


def escape_md(value: Any) -> str:
    text = str(value or "").replace("\n", " ").replace("|", "\\|")
    return text
