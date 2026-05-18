from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Iterable

from valuechain.entity_resolution import COMMON_ALIASES, normalize_company_suffix, normalize_entity_key
from valuechain.models import RelationEvidence


GENERIC_OBJECT_LABELS: dict[str, str] = {
    "cloud or hosting provider": "Cloud or hosting dependency class",
    "supplier(s)": "Supplier dependency class",
    "data center or compute capacity": "Data center or compute capacity class",
    "licensor or licensed technology": "Licensing dependency class",
    "network or interconnection provider": "Network or interconnection dependency class",
    "facility or geography": "Facility or geographic exposure class",
    "power, utility, or cooling supply": "Power, utility, or cooling supply class",
    "distribution channel partner": "Distribution channel dependency class",
    "packaging, assembly, or test provider": "Packaging, assembly, or test dependency class",
    "concentrated dependency": "Concentration risk class",
    "strategic partner": "Strategic partner class",
    "foundry capacity or service": "Foundry capacity class",
    "manufacturing provider": "Manufacturing dependency class",
    "major customer(s)": "Major customer concentration class",
    "co-investment partner": "Co-investment partner class",
    "unnamed counterparty": "Unnamed counterparty",
}

GEOGRAPHY_ALIASES: dict[str, str] = {
    "taiwan": "Taiwan",
    "china": "China",
    "people s republic of china": "China",
    "united states": "United States",
    "u s": "United States",
    "us": "United States",
    "japan": "Japan",
    "south korea": "South Korea",
    "korea": "South Korea",
    "russia": "Russia",
    "netherlands": "Netherlands",
    "ireland": "Ireland",
    "israel": "Israel",
    "singapore": "Singapore",
    "malaysia": "Malaysia",
    "india": "India",
    "hong kong": "Hong Kong",
    "macau": "Macau",
    "canada": "Canada",
    "australia": "Australia",
    "latin america": "Latin America",
    "asia": "Asia",
    "europe": "Europe",
    "in europe": "Europe",
    "european union": "European Union",
}

NAMED_ONLY_RELATIONS = {
    "strategic_partner",
    "co_investment",
}

CLASS_ALLOWED_RELATIONS = {
    "customer_dependency",
    "manufacturing_dependency",
    "foundry_dependency",
    "packaging_or_assembly_dependency",
    "data_center_dependency",
    "power_or_utility_dependency",
    "network_or_interconnection_dependency",
    "distribution_or_channel_dependency",
    "licensing_dependency",
    "facility_or_geographic_exposure",
    "concentration_risk",
}

CLASS_RESTRICTED_RELATIONS = {
    "supplier_dependency",
    "cloud_or_hosting_dependency",
}

COUNTERPARTY_RELATIONS = {
    "supplier_dependency",
    "manufacturing_dependency",
    "foundry_dependency",
    "packaging_or_assembly_dependency",
    "cloud_or_hosting_dependency",
    "data_center_dependency",
    "power_or_utility_dependency",
    "network_or_interconnection_dependency",
    "distribution_or_channel_dependency",
    "strategic_partner",
    "co_investment",
    "licensing_dependency",
}

DEPENDENCY_MARKERS = (
    "rely on",
    "reliance on",
    "relies on",
    "relied on",
    "depend on",
    "depends on",
    "dependent on",
    "dependency on",
    "dependence on",
    "third-party",
    "third party",
    "sole source",
    "single source",
    "limited number",
    "small number",
    "source from",
    "sources from",
    "sourced from",
    "purchase from",
    "purchases from",
    "obtain from",
    "obtains from",
    "procure from",
    "procures from",
    "we use",
    "we utilize",
    "utilize third-party",
    "uses third-party",
    "hosted on",
    "hosted by",
    "powered by",
    "provided by",
    "supplied by",
    "entered into",
    "contract with",
    "contracts with",
    "contract for",
    "contracts for",
    "engage with",
    "engages with",
    "outsourced",
    "subcontractors",
    "suppliers",
    "vendors",
    "providers",
    "transportation suppliers",
    "power purchase agreement",
    "power purchase agreements",
    "ppas",
    "master services agreement",
    "services agreement",
    "supply agreement",
    "purchase agreement",
    "reseller agreement",
    "distribution agreement",
    "cloud services agreement",
    "colocation agreement",
    "capacity agreement",
    "data center lease",
    "lease agreement",
    "license agreement",
    "interconnection agreement",
)

STRONG_DEPENDENCY_MARKERS = (
    "rely on",
    "reliance on",
    "relies on",
    "relied on",
    "depend on",
    "depends on",
    "dependent on",
    "dependency on",
    "dependence on",
    "third-party",
    "third party",
    "sole source",
    "single source",
    "limited number",
    "small number",
    "source from",
    "sources from",
    "sourced from",
    "purchase from",
    "purchases from",
    "obtain from",
    "obtains from",
    "procure from",
    "procures from",
    "we use",
    "we utilize",
    "hosted on",
    "hosted by",
    "powered by",
    "provided by",
    "supplied by",
    "entered into",
    "contract with",
    "contracts with",
    "contract for",
    "contracts for",
    "outsourced",
    "subcontractors",
    "transportation suppliers",
    "power purchase agreement",
    "power purchase agreements",
    "ppas",
    "master services agreement",
    "supply agreement",
    "purchase agreement",
    "cloud services agreement",
    "colocation agreement",
    "capacity agreement",
    "data center lease",
    "license agreement",
)

CONCENTRATION_MARKERS = (
    "concentration",
    "substantial portion",
    "significant portion",
    "single supplier",
    "single customer",
    "sole source",
    "limited number",
    "small number",
    "major customer",
    "large customer",
    "large customers",
    "accounted for",
    "accounts for",
)

STRATEGIC_MARKERS = (
    "strategic partnership",
    "strategic partner",
    "strategic collaboration",
    "collaboration agreement",
    "alliance",
    "joint development",
    "lead partner",
    "co-developer",
    "joint investment",
    "co-investment",
    "joint venture",
    "jointly invest",
)

SELF_PRODUCT_MARKERS = (
    "we offer",
    "we provide",
    "we sell",
    "we deliver",
    "we develop",
    "we market",
    "our products",
    "our services",
    "customers use",
    "competition",
    "compete",
)

REGULATORY_OBJECT_TERMS = (
    "act",
    "regulation",
    "directive",
    "gdpr",
    "dma",
    "dsa",
    "online safety",
    "privacy law",
    "ofac",
    "sanctioned person",
    "specially designated nationals",
    "blocked persons",
    "worker adjustment",
    "united nations security",
)

FRAGMENT_OBJECT_TERMS = (
    "management s discussion",
    "analysis",
    "research",
    "strategy",
    "contractual commitments",
    "hardware furthermore",
    "cybersecurity additionally",
    "development research",
    "qualitative disclosur",
    "enterprise support services",
    "industry solutions",
    "learning experience more personal computing",
    "operates ascenty",
)

PRONOUN_OR_PLACEHOLDER_OBJECT_KEYS = {
    "company",
    "registrant",
    "issuer",
    "group",
    "we",
    "us",
    "our",
    "subsidiary or affiliate",
}

PRODUCT_OBJECT_TERMS = (
    "ai chatbots",
    "ai initiatives",
    "docs",
    "calendar",
    "drive",
    "workspace",
    "vertex ai",
    "gemini",
    "facebook",
    "instagram",
    "feed",
    "stories",
    "xbox",
    "surface",
    "ryzen",
    "pc processors",
    "british pounds",
    "canadian dollars",
    "crm",
    "oem",
    "pcs",
)

SUBJECT_BRANDS: dict[str, tuple[str, ...]] = {
    "alphabet": ("google", "youtube", "gemini", "vertex", "docs", "calendar", "drive", "workspace"),
    "amazon com": ("amazon", "aws"),
    "microsoft": ("microsoft", "azure", "xbox", "surface", "teams", "sharepoint", "exchange", "power apps"),
    "meta platforms": ("meta", "facebook", "instagram", "whatsapp", "threads", "feed", "stories"),
    "advanced micro devices": ("amd", "ryzen", "epyc", "instinct", "radeon"),
    "nvidia": ("nvidia", "cuda", "blackwell", "hopper", "geforce"),
}

CLOUD_VENDOR_ALIASES = {"aws", "amazon web services", "azure", "microsoft azure", "google cloud", "gcp"}

ORG_SUFFIX_RE = re.compile(
    r"\b(?:inc|incorporated|corporation|corp|company|co|ltd|limited|plc|n v|s a|llc|holdings)\b"
)


@dataclass(frozen=True)
class ObjectNormalization:
    original: str
    display_name: str
    canonical_key: str
    object_kind: str
    specificity: float
    is_generic: bool


@dataclass(frozen=True)
class EvidenceDecision:
    original: RelationEvidence
    record: RelationEvidence
    action: str
    reason: str
    canonical_object_key: str
    object_kind: str
    object_specificity: float
    quality_score: float

    def to_diagnostic_row(self) -> dict[str, object]:
        return {
            "action": self.action,
            "reason": self.reason,
            "subject": self.original.subject,
            "original_object": self.original.object,
            "normalized_object": self.record.object,
            "canonical_object_key": self.canonical_object_key,
            "object_kind": self.object_kind,
            "object_specificity": round(self.object_specificity, 3),
            "quality_score": round(self.quality_score, 3),
            "relation_type": self.original.relation_type,
            "modality": self.original.modality,
            "confidence_score": self.original.confidence_score,
            "form": self.original.form,
            "filing_date": self.original.filing_date,
            "accession_number": self.original.accession_number,
            "source_section": self.original.source_section,
            "passage_id": self.original.passage_id,
            "evidence_preview": re.sub(r"\s+", " ", self.original.evidence_text[:260]).strip(),
        }


def denoise_relation_evidence(records: Iterable[RelationEvidence]) -> tuple[list[RelationEvidence], list[dict[str, object]]]:
    decisions = [evaluate_relation_evidence(record) for record in records]
    kept: list[RelationEvidence] = []
    seen: set[tuple[str, str, str, str, str]] = set()
    diagnostics: list[dict[str, object]] = []
    for decision in decisions:
        diagnostics.append(decision.to_diagnostic_row())
        if decision.action != "keep":
            continue
        key = (
            normalize_entity_key(decision.record.subject),
            decision.canonical_object_key,
            decision.record.relation_type,
            decision.record.modality,
            decision.record.passage_id,
        )
        if key in seen:
            diagnostics.append({**decision.to_diagnostic_row(), "action": "drop", "reason": "duplicate_after_normalization"})
            continue
        seen.add(key)
        kept.append(decision.record)
    return kept, diagnostics


def evaluate_relation_evidence(record: RelationEvidence) -> EvidenceDecision:
    info = normalize_dependency_object(record.object, subject=record.subject, evidence_text=record.evidence_text)
    normalized_record = replace(record, object=info.display_name)
    text = record.evidence_text.lower()
    score = evidence_quality_score(record, info)
    reason = keep_or_drop_reason(record, info, score, text)
    action = "keep" if reason == "kept" else "drop"
    return EvidenceDecision(
        original=record,
        record=normalized_record,
        action=action,
        reason=reason,
        canonical_object_key=info.canonical_key,
        object_kind=info.object_kind,
        object_specificity=info.specificity,
        quality_score=score,
    )


def normalize_dependency_object(
    obj: str,
    subject: str = "",
    evidence_text: str = "",
) -> ObjectNormalization:
    original = obj.strip()
    if not original:
        return ObjectNormalization(original, "Unnamed counterparty", "generic:unnamed-counterparty", "generic", 0.0, True)

    inferred = infer_common_alias_from_text(original, subject=subject, evidence_text=evidence_text)
    if inferred:
        return inferred

    key = object_key(original)
    generic_labels = {object_key(name): label for name, label in GENERIC_OBJECT_LABELS.items()}
    if key in generic_labels:
        display = generic_labels[key]
        return ObjectNormalization(original, display, f"class:{key}", "dependency_class", 0.25, True)

    alias_target = canonical_company_name(original)
    if alias_target:
        canonical = object_key(alias_target)
        return ObjectNormalization(original, alias_target, f"company:{canonical}", "company", 1.0, False)

    geography = matching_geography(key)
    if geography:
        return ObjectNormalization(original, geography, f"geography:{object_key(geography)}", "geography", 0.72, False)

    if looks_like_named_organization(original):
        display = normalize_display_name(original)
        return ObjectNormalization(original, display, f"organization:{object_key(display)}", "organization", 0.82, False)

    if is_generic_dependency_phrase(original):
        display = normalize_display_name(original)
        return ObjectNormalization(original, display, f"class:{key}", "dependency_class", 0.35, True)

    display = normalize_display_name(original)
    return ObjectNormalization(original, display, f"unknown:{key}", "unknown", 0.48, False)


def infer_common_alias_from_text(
    obj: str,
    subject: str = "",
    evidence_text: str = "",
) -> ObjectNormalization | None:
    if object_key(obj) not in {object_key(name) for name in GENERIC_OBJECT_LABELS}:
        return None
    subject_key = normalize_entity_key(subject)
    text = evidence_text.lower()
    for alias, target in sorted(COMMON_ALIASES.items(), key=lambda item: len(item[0]), reverse=True):
        if normalize_entity_key(target) == subject_key:
            continue
        if re.search(rf"\b{re.escape(alias.lower())}\b", text):
            return ObjectNormalization(
                obj,
                target,
                f"company:{object_key(target)}",
                "company",
                1.0,
                False,
            )
    return None


def keep_or_drop_reason(
    record: RelationEvidence,
    info: ObjectNormalization,
    score: float,
    text: str,
) -> str:
    if normalize_entity_key(record.subject) and normalize_entity_key(record.subject) == object_key(info.display_name):
        return "object_is_subject"
    if record.relation_type in {"strategic_partner", "co_investment"} and not has_strategic_signal(text):
        return "strategic_language_required"
    if record.relation_type == "strategic_partner" and record.modality != "strategic":
        return "strategic_modality_required"
    if record.relation_type == "strategic_partner" and is_competition_context(text):
        return "competition_context_without_dependency"
    if record.relation_type == "subsidiary_or_control" and (
        not object_supported_for_subsidiary_or_control(info) or not has_subsidiary_or_control_signal(record, text)
    ):
        return "object_not_supported_for_relation"
    if record.relation_type in NAMED_ONLY_RELATIONS and info.is_generic:
        return "named_counterparty_required"
    if record.relation_type == "facility_or_geographic_exposure" and not object_supported_for_facility_or_geography(info, text):
        return "object_not_supported_for_relation"
    if record.relation_type in CLASS_RESTRICTED_RELATIONS and info.is_generic:
        if class_object_supported_by_text(record.relation_type, text):
            return "kept"
        if record.relation_type == "cloud_or_hosting_dependency" and has_named_cloud_vendor(text):
            if cloud_vendor_is_subject(record.subject, text):
                return "internal_brand_or_product_object"
            return "kept"
        return "generic_object_not_graph_ready"
    if info.is_generic and record.relation_type not in CLASS_ALLOWED_RELATIONS:
        return "class_object_not_allowed"
    if info.is_generic and not class_object_supported_by_text(record.relation_type, text):
        return "generic_object_without_dependency_signal"
    if record.relation_type == "cloud_or_hosting_dependency" and looks_like_self_product_statement(text, info):
        return "self_product_statement"
    if record.relation_type == "network_or_interconnection_dependency" and looks_like_self_product_statement(text, info):
        return "self_product_statement"
    if looks_like_internal_brand_object(record.subject, info.display_name, text):
        return "internal_brand_or_product_object"
    if is_regulatory_or_fragment_object(info.display_name):
        return "regulatory_or_fragment_object"
    if record.relation_type in COUNTERPARTY_RELATIONS and not object_supported_for_counterparty_relation(record, info, text):
        return "object_not_supported_for_relation"
    if is_competition_context(text) and record.relation_type in COUNTERPARTY_RELATIONS and not has_strong_dependency_signal(text):
        return "competition_context_without_dependency"
    if score < 0.4:
        return "low_quality_score"
    return "kept"


def evidence_quality_score(record: RelationEvidence, info: ObjectNormalization) -> float:
    text = record.evidence_text.lower()
    score = record.confidence_score * 0.55 + info.specificity * 0.30
    if record.modality == "current_fact":
        score += 0.08
    elif record.modality == "strategic":
        score += 0.06
    elif record.modality == "risk_hypothetical":
        score += 0.02
    elif record.modality == "forward_looking":
        score -= 0.02
    if any(
        section in record.source_section
        for section in [
            "item_1_business",
            "item_1_01_material_agreement",
            "exhibit_10_material_contract",
            "exhibit_21_subsidiaries",
            "exhibit_99_1_investor_or_earnings",
        ]
    ):
        score += 0.04
    if "risk" in record.source_section:
        score += 0.02
    if has_strong_dependency_signal(text):
        score += 0.07
    if has_concentration_signal(text):
        score += 0.05
    if info.is_generic:
        score -= 0.12
    if looks_like_self_product_statement(text, info):
        score -= 0.18
    return round(max(0.0, min(score, 1.0)), 3)


def class_object_supported_by_text(relation_type: str, text: str) -> bool:
    if relation_type == "supplier_dependency":
        return has_strong_dependency_signal(text) or has_concentration_signal(text)
    if relation_type == "cloud_or_hosting_dependency":
        return any(
            marker in text
            for marker in [
                "third-party cloud",
                "third party cloud",
                "cloud computing platform provider",
                "cloud computing platform providers",
                "hosting provider",
                "hosting providers",
                "hosted by",
                "hosting facilities",
                "single vendor dependence",
            ]
        ) or has_strong_dependency_signal(text)
    if relation_type in {"customer_dependency", "concentration_risk"}:
        return has_concentration_signal(text)
    if relation_type in {"manufacturing_dependency", "foundry_dependency", "packaging_or_assembly_dependency"}:
        return has_strong_dependency_signal(text) or any(
            marker in text for marker in ["outsourced", "subcontractors", "contract manufacturers", "foundries"]
        )
    if relation_type == "data_center_dependency":
        return any(
            marker in text
            for marker in [
                "third-party",
                "third party",
                "leased",
                "lease",
                "colocation",
                "co-location",
                "providers",
                "hosting facilities",
                "data center providers",
            ]
        )
    if relation_type == "power_or_utility_dependency":
        return any(
            marker in text
            for marker in [
                "power supply",
                "electricity",
                "utility",
                "energy supply",
                "grid",
                "cooling",
                "fuel",
                "natural gas",
                "uranium",
                "transportation contracts",
                "transportation suppliers",
                "power purchase agreement",
                "power purchase agreements",
                "ppas",
            ]
        )
    if relation_type == "network_or_interconnection_dependency":
        return any(
            marker in text
            for marker in [
                "interconnection",
                "peering",
                "carrier",
                "bandwidth",
                "third-party network",
                "isp",
                "internet infrastructure",
                "co-location relationships",
            ]
        )
    if relation_type == "distribution_or_channel_dependency":
        return has_strong_dependency_signal(text) or any(
            marker in text for marker in ["distributor", "reseller", "channel partner", "distribution agreement", "reseller agreement"]
        )
    if relation_type == "licensing_dependency":
        return any(marker in text for marker in ["licensed from", "license from", "license agreement", "third-party licenses", "open source"])
    if relation_type == "subsidiary_or_control":
        return any(marker in text for marker in ["subsidiary", "subsidiaries", "wholly owned", "controlled by", "ownership interest"])
    if relation_type == "facility_or_geographic_exposure":
        return any(alias in text for alias in GEOGRAPHY_ALIASES) or any(marker in text for marker in ["facility", "facilities"])
    return has_dependency_signal(text)


def canonical_company_name(name: str) -> str:
    key = object_key(name)
    for alias, target in COMMON_ALIASES.items():
        if object_key(alias) == key or object_key(target) == key:
            return target
    return ""


def object_key(name: str) -> str:
    lowered = name.lower().replace("&", " and ")
    lowered = normalize_company_suffix(lowered)
    lowered = re.sub(r"[^a-z0-9]+", " ", lowered)
    lowered = re.sub(r"\b(the|a|an)\b", " ", lowered)
    lowered = re.sub(r"\b(specifically|additionally|furthermore)\b\s*$", " ", lowered)
    return re.sub(r"\s+", " ", lowered).strip()


def normalize_display_name(name: str) -> str:
    cleaned = re.sub(r"\s+", " ", name.strip(" \t\r\n,;:."))
    if not cleaned:
        return "Unnamed counterparty"
    return cleaned


def looks_like_named_organization(name: str) -> bool:
    key = object_key(name)
    if not key:
        return False
    if ORG_SUFFIX_RE.search(key):
        return True
    words = normalize_display_name(name).split()
    if len(words) <= 1:
        return False
    titleish = sum(1 for word in words if word[:1].isupper() or word.isupper())
    return titleish >= max(2, len(words) - 1)


def is_generic_dependency_phrase(name: str) -> bool:
    key = object_key(name)
    generic_terms = (
        "provider",
        "providers",
        "supplier",
        "suppliers",
        "vendor",
        "vendors",
        "customer",
        "customers",
        "capacity",
        "facility",
        "facilities",
        "geography",
        "channel",
        "partner",
        "partners",
        "utility",
        "manufacturer",
        "manufacturers",
        "subsidiary",
        "subsidiaries",
        "affiliate",
        "affiliates",
        "dependency",
        "class",
        "concentration",
        "risk",
    )
    return any(term in key.split() for term in generic_terms)


def has_dependency_signal(text: str) -> bool:
    return any(marker in text for marker in DEPENDENCY_MARKERS)


def has_strong_dependency_signal(text: str) -> bool:
    return any(marker in text for marker in STRONG_DEPENDENCY_MARKERS)


def has_concentration_signal(text: str) -> bool:
    return any(marker in text for marker in CONCENTRATION_MARKERS)


def has_strategic_signal(text: str) -> bool:
    return any(marker in text for marker in STRATEGIC_MARKERS)


def has_named_cloud_vendor(text: str) -> bool:
    return any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in CLOUD_VENDOR_ALIASES)


def looks_like_self_product_statement(text: str, info: ObjectNormalization) -> bool:
    if info.object_kind in {"company", "organization"}:
        return False
    if has_strong_dependency_signal(text):
        return False
    return any(marker in text for marker in SELF_PRODUCT_MARKERS)


def object_supported_for_counterparty_relation(
    record: RelationEvidence,
    info: ObjectNormalization,
    text: str,
) -> bool:
    if info.object_kind == "company":
        if record.relation_type in {"strategic_partner", "co_investment"}:
            return object_appears_near_markers(text, info.display_name, STRATEGIC_MARKERS)
        if record.relation_type in {"customer_dependency", "concentration_risk"}:
            return has_concentration_signal(text) or has_strong_dependency_signal(text)
        return has_strong_dependency_signal(text) or named_company_supported_by_relation_context(record.relation_type, text)
    if record.relation_type == "facility_or_geographic_exposure":
        return info.object_kind == "geography" or (
            info.object_kind == "organization" and looks_like_legal_entity(info.display_name)
        )
    if info.object_kind == "geography":
        return False
    if info.object_kind == "organization":
        if looks_like_legal_entity(info.display_name):
            if record.relation_type in {"strategic_partner", "co_investment"}:
                return object_appears_near_markers(text, info.display_name, STRATEGIC_MARKERS)
            if record.relation_type in {"customer_dependency", "concentration_risk"}:
                return has_concentration_signal(text) or has_strong_dependency_signal(text)
            return (
                record.relation_type not in COUNTERPARTY_RELATIONS
                or has_strong_dependency_signal(text)
                or named_company_supported_by_relation_context(record.relation_type, text)
            )
        if appears_in_counterparty_list(text, info.display_name) and has_strong_dependency_signal(text):
            return True
        if record.relation_type in {"strategic_partner", "co_investment"} and any(
            marker in text for marker in ["partnership", "collaboration", "alliance", "joint"]
        ):
            return object_appears_near_markers(text, info.display_name, STRATEGIC_MARKERS)
        return False
    if info.object_kind == "unknown":
        return False
    return True


def named_company_supported_by_relation_context(relation_type: str, text: str) -> bool:
    if relation_type == "cloud_or_hosting_dependency":
        return any(marker in text for marker in ["hosted by", "hosted on", "cloud provider", "cloud computing platform provider"])
    if relation_type == "data_center_dependency":
        return any(marker in text for marker in ["leased data center", "colocation", "co-location", "data center provider", "hosted by"])
    if relation_type == "power_or_utility_dependency":
        return any(marker in text for marker in ["power purchase agreement", "utility", "electricity supplied", "energy supply"])
    if relation_type == "distribution_or_channel_dependency":
        return any(marker in text for marker in ["reseller agreement", "distribution agreement", "channel partner"])
    if relation_type == "licensing_dependency":
        return any(marker in text for marker in ["licensed from", "license agreement", "license from"])
    return False


def object_supported_for_subsidiary_or_control(info: ObjectNormalization) -> bool:
    if info.is_generic or info.object_kind in {"geography", "unknown", "generic"}:
        return False
    return info.object_kind in {"company", "organization"}


def object_appears_near_markers(
    text: str,
    display_name: str,
    markers: Iterable[str],
    window: int = 280,
) -> bool:
    lowered = text.lower()
    aliases = object_aliases(display_name)
    for marker in markers:
        start = lowered.find(marker)
        while start >= 0:
            fragment = lowered[max(0, start - window) : start + len(marker) + window]
            fragment_key = object_key(fragment)
            if any(alias in fragment or object_key(alias) in fragment_key for alias in aliases):
                return True
            start = lowered.find(marker, start + len(marker))
    return False


def object_aliases(display_name: str) -> set[str]:
    aliases = {display_name.lower(), object_key(display_name)}
    display_key = object_key(display_name)
    for alias, target in COMMON_ALIASES.items():
        if object_key(target) == display_key or object_key(alias) == display_key:
            aliases.add(alias.lower())
            aliases.add(object_key(alias))
            aliases.add(target.lower())
            aliases.add(object_key(target))
    return {alias for alias in aliases if alias}


def object_supported_for_facility_or_geography(info: ObjectNormalization, text: str) -> bool:
    if info.object_kind == "geography" or info.is_generic:
        return True
    if info.object_kind == "organization" and looks_like_legal_entity(info.display_name):
        return True
    if "," in info.display_name and any(marker in text for marker in ["facility", "plant", "data center", "office"]):
        return True
    return False


def looks_like_legal_entity(name: str) -> bool:
    return ORG_SUFFIX_RE.search(object_key(name)) is not None


def appears_in_counterparty_list(text: str, name: str) -> bool:
    lowered = text.lower()
    name_key = name.lower().strip(" ,;:.")
    index = lowered.find(name_key)
    if index < 0:
        return False
    window = lowered[max(0, index - 120) : index]
    has_list_marker = any(marker in window for marker in ["such as", "including", "include", "includes"])
    has_counterparty_noun = any(
        marker in window
        for marker in [
            "supplier",
            "vendor",
            "subcontractor",
            "contract manufacturer",
            "partner",
            "provider",
            "customer",
            "foundr",
            "licensor",
        ]
    )
    return has_list_marker and has_counterparty_noun


def is_regulatory_or_fragment_object(name: str) -> bool:
    raw_key = re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()
    if raw_key in PRONOUN_OR_PLACEHOLDER_OBJECT_KEYS or raw_key in {"the company", "the registrant", "the issuer"}:
        return True
    if raw_key.endswith(" the company") or " the company " in raw_key:
        return True
    key = object_key(name)
    if key in PRONOUN_OR_PLACEHOLDER_OBJECT_KEYS:
        return True
    key_words = set(key.split())
    if any((term in key_words if len(term.split()) == 1 else term in key) for term in REGULATORY_OBJECT_TERMS):
        return True
    if any(term in key for term in FRAGMENT_OBJECT_TERMS):
        return True
    if any(term == key or term in key for term in PRODUCT_OBJECT_TERMS):
        return True
    if len(key.split()) > 7 and not looks_like_legal_entity(name):
        return True
    return False


def looks_like_internal_brand_object(subject: str, obj: str, text: str) -> bool:
    subject_key = normalize_entity_key(subject)
    obj_key = object_key(obj)
    brands = SUBJECT_BRANDS.get(subject_key, ())
    return any(obj_key == brand or obj_key.startswith(f"{brand} ") for brand in brands)


def cloud_vendor_is_subject(subject: str, text: str) -> bool:
    subject_key = normalize_entity_key(subject)
    vendor_subjects = {
        "amazon com": ("aws", "amazon web services"),
        "microsoft": ("azure", "microsoft azure"),
        "alphabet": ("google cloud", "gcp"),
    }
    aliases = vendor_subjects.get(subject_key, ())
    return any(re.search(rf"\b{re.escape(alias)}\b", text) for alias in aliases)


def matching_geography(key: str) -> str:
    if key in GEOGRAPHY_ALIASES:
        return GEOGRAPHY_ALIASES[key]
    for alias, geography in GEOGRAPHY_ALIASES.items():
        if key.startswith(f"{alias} "):
            return geography
    return ""


def is_competition_context(text: str) -> bool:
    return any(marker in text[:700] for marker in ["competition", "competitors", "compete with", "competitive"])


def has_subsidiary_or_control_signal(record: RelationEvidence, text: str) -> bool:
    if record.source_section.startswith("exhibit_21"):
        return True
    return any(
        marker in text
        for marker in [
            "subsidiary",
            "subsidiaries",
            "wholly owned",
            "majority owned",
            "controlled by",
            "parent company",
            "ownership interest",
        ]
    )


def is_placeholder_object(obj: str) -> bool:
    info = normalize_dependency_object(obj)
    return info.is_generic or object_key(obj).endswith(" class")
