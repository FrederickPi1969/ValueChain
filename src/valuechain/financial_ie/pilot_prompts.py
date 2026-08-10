from __future__ import annotations

import json
import re
from typing import Any

from valuechain.financial_ie.models import DocumentChunk


PRIMARY_INDUSTRIES = (
    "semiconductors",
    "technology_hardware",
    "software_and_data",
    "cloud_and_data_centers",
    "telecommunications_and_networks",
    "energy_and_utilities",
    "industrials_and_infrastructure",
    "transportation_and_logistics",
    "aerospace_and_defense",
    "financial_services",
    "healthcare_and_life_sciences",
    "consumer_and_retail",
    "materials_and_mining",
    "real_estate",
    "diversified_or_other",
)

STRATEGIC_DOMAINS = (
    "ai_compute",
    "semiconductor_supply_chain",
    "cloud_infrastructure",
    "cybersecurity",
    "enterprise_software",
    "data_and_analytics",
    "telecom_infrastructure",
    "grid_and_power",
    "energy_security",
    "energy_transition",
    "industrial_automation",
    "critical_materials",
    "aerospace",
    "defense",
    "transportation",
    "logistics",
    "healthcare_infrastructure",
    "biopharma",
    "financial_infrastructure",
    "consumer_platform",
)

VALUE_CHAIN_ROLES = (
    "upstream_input_provider",
    "component_supplier",
    "equipment_provider",
    "manufacturer",
    "infrastructure_operator",
    "platform_provider",
    "software_or_service_provider",
    "distributor_or_channel",
    "end_market_demand",
    "capital_provider",
    "diversified",
)

SIGNAL_CATEGORIES = (
    "demand_and_revenue",
    "pricing_and_margin",
    "capital_allocation",
    "capacity_and_supply",
    "customer_concentration",
    "supplier_or_infrastructure_dependency",
    "regulatory_or_geopolitical",
    "technology_and_product",
    "partnership_or_mna",
    "liquidity_and_balance_sheet",
)


PROFILE_SYSTEM = """You extract a compact public-company business profile from regulatory filing evidence.
Return one JSON object only. Use only supplied chunks. Do not infer an industry, product, market, or strategic
importance from prior knowledge. Every important claim must be supported by a cited chunk and a short exact quote.
If evidence is absent, use an empty list or null rather than guessing."""

SIGNALS_SYSTEM = """You are a public-equities analyst extracting material company signals from regulatory filings.
Return one JSON object only. Use only supplied chunks. A signal must be company-specific and decision-relevant,
not generic boilerplate. Preserve whether language is current fact, historical fact, forward-looking, or hypothetical
risk. Cite an exact source quote and chunk id for every signal. Do not calculate financial metrics."""


def build_profile_prompt(company_name: str, chunks: list[DocumentChunk]) -> str:
    return f"""Company: {company_name}

Allowed primary_industry values:
{json.dumps(PRIMARY_INDUSTRIES)}
Allowed strategic_domains values:
{json.dumps(STRATEGIC_DOMAINS)}
Allowed value_chain_roles values:
{json.dumps(VALUE_CHAIN_ROLES)}

Return this schema:
{{
  "business_summary": "one or two factual sentences",
  "primary_industry": "one allowed value or null",
  "strategic_domains": ["up to five allowed values"],
  "value_chain_roles": ["up to five allowed values"],
  "products_services": ["up to eight filing-grounded items"],
  "end_markets": ["up to six filing-grounded customer or demand markets"],
  "operating_geographies": ["up to six material operating geographies"],
  "strategic_importance": 1,
  "strategic_importance_rationale": "one evidence-grounded sentence",
  "evidence": [{{"chunk_id":"id","quote":"exact source quote, at most 35 words"}}]
}}
strategic_importance is 1-5 and measures disclosed strategic relevance or bottleneck potential, not company size.
Use no more than four evidence entries.

FILING CHUNKS:
{render_chunks(chunks)}"""


def build_signals_prompt(
    company_name: str,
    chunks: list[DocumentChunk],
    profile: dict[str, Any],
) -> str:
    profile_context = {
        key: profile.get(key)
        for key in ("primary_industry", "strategic_domains", "value_chain_roles")
    }
    return f"""Company: {company_name}
Profile context: {json.dumps(profile_context, ensure_ascii=False)}

Allowed categories:
{json.dumps(SIGNAL_CATEGORIES)}

Return this schema:
{{"signals":[{{
  "category":"one allowed category",
  "headline":"short factual headline",
  "statement":"what happened or what exposure was disclosed",
  "direction":"positive|negative|mixed|neutral",
  "modality":"current_fact|historical_fact|forward_looking|risk_hypothetical",
  "significance":1,
  "significance_rationale":"why an ETF or public-equities analyst may care",
  "confidence":0.0,
  "chunk_id":"exact supplied chunk id",
  "evidence_quote":"exact source quote, at most 45 words"
}}]}}

significance is 1-5. Return at most 8 signals. Keep headline and rationale under 20 words each. Prefer quantified, named-counterparty, capacity, demand,
capital-spending, concentration, regulatory, product-transition, liquidity, or transaction evidence. Exclude generic
risk boilerplate and ordinary accounting-policy text. A hypothetical risk cannot be labeled current_fact.

FILING CHUNKS:
{render_chunks(chunks)}"""


def normalize_profile(payload: Any, chunk_map: dict[str, DocumentChunk]) -> dict[str, Any]:
    row = payload if isinstance(payload, dict) else {}
    profile = {
        "business_summary": clean_text(row.get("business_summary"), 700),
        "primary_industry": allowed_value(row.get("primary_industry"), PRIMARY_INDUSTRIES),
        "strategic_domains": allowed_list(row.get("strategic_domains"), STRATEGIC_DOMAINS, 5),
        "value_chain_roles": allowed_list(row.get("value_chain_roles"), VALUE_CHAIN_ROLES, 5),
        "products_services": text_list(row.get("products_services"), 8, 140),
        "end_markets": text_list(row.get("end_markets"), 6, 140),
        "operating_geographies": text_list(row.get("operating_geographies"), 6, 100),
        "strategic_importance": bounded_int(row.get("strategic_importance"), 1, 5),
        "strategic_importance_rationale": clean_text(row.get("strategic_importance_rationale"), 400),
        "evidence": normalize_evidence(row.get("evidence"), chunk_map, limit=4),
    }
    profile["evidence_valid"] = bool(profile["evidence"]) and all(
        item["evidence_valid"] for item in profile["evidence"]
    )
    return profile


def normalize_signals(payload: Any, chunk_map: dict[str, DocumentChunk]) -> list[dict[str, Any]]:
    rows = payload.get("signals", []) if isinstance(payload, dict) else payload if isinstance(payload, list) else []
    if not isinstance(rows, list):
        return []
    signals: list[dict[str, Any]] = []
    for row in rows[:8]:
        if not isinstance(row, dict):
            continue
        category = allowed_value(row.get("category"), SIGNAL_CATEGORIES)
        chunk_id = str(row.get("chunk_id") or "").strip()
        quote = clean_text(row.get("evidence_quote"), 800)
        chunk = chunk_map.get(chunk_id)
        evidence_valid = bool(chunk and quote and quote_in_text(quote, chunk.text))
        if not category or not clean_text(row.get("statement"), 800):
            continue
        signals.append(
            {
                "category": category,
                "headline": clean_text(row.get("headline"), 180),
                "statement": clean_text(row.get("statement"), 800),
                "direction": allowed_value(row.get("direction"), ("positive", "negative", "mixed", "neutral"))
                or "neutral",
                "modality": allowed_value(
                    row.get("modality"),
                    ("current_fact", "historical_fact", "forward_looking", "risk_hypothetical"),
                )
                or "current_fact",
                "significance": bounded_int(row.get("significance"), 1, 5),
                "significance_rationale": clean_text(row.get("significance_rationale"), 400),
                "confidence": bounded_float(row.get("confidence"), 0.0, 1.0),
                "chunk_id": chunk_id,
                "evidence_quote": quote,
                "evidence_valid": evidence_valid,
                "source_section": chunk.section_hint if chunk else "",
                "review_status": "candidate" if evidence_valid else "needs_evidence_review",
            }
        )
    return deduplicate_signals(signals)


def normalize_evidence(
    value: Any,
    chunk_map: dict[str, DocumentChunk],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    evidence: list[dict[str, Any]] = []
    for row in value[:limit]:
        if not isinstance(row, dict):
            continue
        chunk_id = str(row.get("chunk_id") or "").strip()
        quote = clean_text(row.get("quote"), 700)
        chunk = chunk_map.get(chunk_id)
        evidence.append(
            {
                "chunk_id": chunk_id,
                "quote": quote,
                "evidence_valid": bool(chunk and quote and quote_in_text(quote, chunk.text)),
                "source_section": chunk.section_hint if chunk else "",
            }
        )
    return evidence


def render_chunks(chunks: list[DocumentChunk]) -> str:
    return "\n\n".join(
        f"[chunk_id={chunk.chunk_id}; section={chunk.section_hint or 'unknown'}]\n{chunk.text}"
        for chunk in chunks
    )


def quote_in_text(quote: str, text: str) -> bool:
    normalized_text = normalize_match_text(text)
    if "..." in quote or "…" in quote:
        cursor = 0
        for part in re.split(r"(?:\.\.\.|…)", quote):
            normalized_part = normalize_match_text(part)
            if not normalized_part:
                continue
            position = normalized_text.find(normalized_part, cursor)
            if position < 0:
                return False
            cursor = position + len(normalized_part)
        return cursor > 0
    normalized_quote = normalize_match_text(quote.replace("...", " ").replace("…", " "))
    if not normalized_quote:
        return False
    if normalized_quote in normalized_text:
        return True
    quote_tokens = normalized_quote.split()
    if len(quote_tokens) >= 10:
        prefix = " ".join(quote_tokens[:8])
        suffix = " ".join(quote_tokens[-8:])
        return prefix in normalized_text and suffix in normalized_text
    return False


def normalize_match_text(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9%$.-]+", " ", value.lower()).strip()
    normalized = re.sub(r"(\d)\s+%", r"\1%", normalized)
    normalized = re.sub(r"\$\s+(\d)", r"$\1", normalized)
    return normalized


def clean_text(value: Any, limit: int) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def text_list(value: Any, limit: int, item_limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(clean_text(item, item_limit) for item in value if clean_text(item, item_limit)))[:limit]


def allowed_value(value: Any, allowed: tuple[str, ...]) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in allowed else None


def allowed_list(value: Any, allowed: tuple[str, ...], limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return list(dict.fromkeys(item for item in (allowed_value(row, allowed) for row in value) if item))[:limit]


def bounded_int(value: Any, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = minimum
    return max(minimum, min(maximum, parsed))


def bounded_float(value: Any, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = minimum
    return round(max(minimum, min(maximum, parsed)), 3)


def deduplicate_signals(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: dict[tuple[str, str], dict[str, Any]] = {}
    for signal in signals:
        key = (signal["category"], normalize_match_text(signal["headline"] or signal["statement"]))
        unique.setdefault(key, signal)
    return list(unique.values())
