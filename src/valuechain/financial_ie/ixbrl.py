from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag


IXBRL_EXTRACTOR_VERSION = "inline-xbrl-v0.4"


@dataclass(frozen=True, slots=True)
class ConceptSpec:
    field: str
    concepts: tuple[str, ...]
    period_type: str


CONCEPT_SPECS: tuple[ConceptSpec, ...] = (
    ConceptSpec(
        "revenue",
        (
            "RevenueFromContractWithCustomerExcludingAssessedTax",
            "Revenues",
            "SalesRevenueNet",
            "Revenue",
            "RevenueFromContractsWithCustomers",
            "RegulatedAndUnregulatedOperatingRevenue",
            "RevenuesNetOfInterestExpense",
        ),
        "duration",
    ),
    ConceptSpec("net_income", ("NetIncomeLoss", "ProfitLoss"), "duration"),
    ConceptSpec("operating_income", ("OperatingIncomeLoss",), "duration"),
    ConceptSpec("total_assets", ("Assets",), "instant"),
    ConceptSpec("total_liabilities", ("Liabilities",), "instant"),
    ConceptSpec(
        "stockholders_equity",
        (
            "StockholdersEquity",
            "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
            "Equity",
            "EquityAttributableToOwnersOfParent",
        ),
        "instant",
    ),
    ConceptSpec(
        "cash_and_equivalents",
        (
            "CashAndCashEquivalentsAtCarryingValue",
            "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
            "CashAndCashEquivalents",
        ),
        "instant",
    ),
    ConceptSpec(
        "operating_cash_flow",
        (
            "NetCashProvidedByUsedInOperatingActivities",
            "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations",
            "CashFlowsFromUsedInOperatingActivities",
        ),
        "duration",
    ),
    ConceptSpec(
        "capital_expenditure",
        (
            "PaymentsToAcquirePropertyPlantAndEquipment",
            "PaymentsForAdditionsToPropertyPlantAndEquipment",
            "PaymentsToAcquireProductiveAssets",
            "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
        ),
        "duration",
    ),
    ConceptSpec("research_and_development", ("ResearchAndDevelopmentExpense",), "duration"),
    ConceptSpec("employees", ("EntityNumberOfEmployees",), "instant"),
)


def extract_financial_facts(path: Path, *, report_date: str = "") -> list[dict[str, Any]]:
    soup = BeautifulSoup(path.read_bytes(), "html.parser")
    contexts = parse_contexts(soup)
    units = parse_units(soup)
    candidates: dict[str, list[dict[str, Any]]] = {spec.field: [] for spec in CONCEPT_SPECS}
    specs_by_concept = {
        concept.lower(): spec
        for spec in CONCEPT_SPECS
        for concept in spec.concepts
    }
    target_date = parse_iso_date(report_date)
    for tag in soup.find_all(is_inline_numeric_fact):
        concept_name = local_name(str(tag.attrs.get("name") or ""))
        spec = specs_by_concept.get(concept_name.lower())
        if spec is None:
            continue
        value = parse_inline_number(tag)
        if value is None:
            continue
        context_ref = str(tag.attrs.get("contextref") or tag.attrs.get("contextRef") or "")
        context = contexts.get(context_ref, {})
        if not context or context.get("has_dimensions"):
            continue
        period_end = parse_iso_date(str(context.get("end_date") or context.get("instant") or ""))
        if target_date and period_end and abs((period_end - target_date).days) > 45:
            continue
        unit_ref = str(tag.attrs.get("unitref") or tag.attrs.get("unitRef") or "")
        unit = units.get(unit_ref, local_name(unit_ref) or "unknown")
        candidate = {
            "field": spec.field,
            "value": decimal_string(value),
            "unit": normalize_unit(unit),
            "period_start": context.get("start_date", ""),
            "period_end": context.get("end_date") or context.get("instant", ""),
            "period_type": spec.period_type,
            "source_concept": str(tag.attrs.get("name") or ""),
            "context_ref": context_ref,
            "fact_id": str(tag.attrs.get("id") or ""),
            "extraction_method": "inline_xbrl",
            "extractor_version": IXBRL_EXTRACTOR_VERSION,
            "confidence": 1.0,
            "_score": score_candidate(spec, context, unit, target_date),
        }
        candidates[spec.field].append(candidate)
    selected: list[dict[str, Any]] = []
    for spec in CONCEPT_SPECS:
        rows = candidates[spec.field]
        if not rows:
            continue
        best = max(rows, key=lambda row: (row["_score"], row["context_ref"], row["fact_id"]))
        selected.append({key: value for key, value in best.items() if key != "_score"})
    return selected


def parse_contexts(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    contexts: dict[str, dict[str, Any]] = {}
    for tag in soup.find_all(lambda item: isinstance(item, Tag) and local_name(item.name) == "context"):
        context_id = str(tag.attrs.get("id") or "")
        if not context_id:
            continue
        contexts[context_id] = {
            "instant": child_text(tag, "instant"),
            "start_date": child_text(tag, "startDate"),
            "end_date": child_text(tag, "endDate"),
            "has_dimensions": tag.find(
                lambda item: isinstance(item, Tag)
                and local_name(item.name).lower() in {"segment", "scenario", "explicitmember", "typedmember"}
            )
            is not None,
        }
    return contexts


def parse_units(soup: BeautifulSoup) -> dict[str, str]:
    units: dict[str, str] = {}
    for tag in soup.find_all(lambda item: isinstance(item, Tag) and local_name(item.name) == "unit"):
        unit_id = str(tag.attrs.get("id") or "")
        if unit_id:
            units[unit_id] = " ".join(tag.stripped_strings)
    return units


def child_text(tag: Tag, child_name: str) -> str:
    child = tag.find(
        lambda item: isinstance(item, Tag) and local_name(item.name).lower() == child_name.lower()
    )
    return child.get_text(" ", strip=True) if child else ""


def is_inline_numeric_fact(tag: Tag) -> bool:
    return isinstance(tag, Tag) and local_name(tag.name).lower() in {"nonfraction", "fraction"}


def parse_inline_number(tag: Tag) -> Decimal | None:
    if str(tag.attrs.get("xsi:nil") or tag.attrs.get("nil") or "").lower() == "true":
        return None
    raw = str(tag.attrs.get("value") or tag.get_text("", strip=True)).strip()
    if not raw or raw in {"-", "—", "–"}:
        return None
    negative_parentheses = raw.startswith("(") and raw.endswith(")")
    clean = raw.replace(",", "").replace("$", "").replace("%", "")
    clean = re.sub(r"[^0-9eE.+-]", "", clean)
    if not clean:
        return None
    try:
        value = Decimal(clean)
        scale = int(str(tag.attrs.get("scale") or "0"))
    except (InvalidOperation, ValueError):
        return None
    value *= Decimal(10) ** scale
    if negative_parentheses or str(tag.attrs.get("sign") or "") == "-":
        value = -abs(value)
    return value


def score_candidate(
    spec: ConceptSpec,
    context: dict[str, Any],
    unit: str,
    target_date: date | None,
) -> int:
    score = 0
    if not context.get("has_dimensions"):
        score += 8
    period_end = parse_iso_date(str(context.get("end_date") or context.get("instant") or ""))
    if target_date and period_end:
        difference = abs((period_end - target_date).days)
        score += 8 if difference <= 7 else 4 if difference <= 45 else 0
    if spec.period_type == "instant" and context.get("instant"):
        score += 5
    if spec.period_type == "duration" and context.get("start_date") and context.get("end_date"):
        start = parse_iso_date(str(context["start_date"]))
        end = parse_iso_date(str(context["end_date"]))
        if start and end:
            days = (end - start).days
            score += 6 if 300 <= days <= 430 else 2 if 80 <= days <= 100 else 0
    normalized_unit = normalize_unit(unit)
    if spec.field == "employees":
        score += 2 if normalized_unit in {"shares", "pure", "unknown"} else 0
    else:
        score += 3 if normalized_unit == "USD" else 0
    return score


def normalize_unit(value: str) -> str:
    lowered = value.lower()
    if "usd" in lowered:
        return "USD"
    if "share" in lowered:
        return "shares"
    if "pure" in lowered:
        return "pure"
    return local_name(value) or "unknown"


def local_name(value: str | None) -> str:
    return str(value or "").split(":")[-1]


def parse_iso_date(value: str) -> date | None:
    try:
        return date.fromisoformat(value[:10])
    except (TypeError, ValueError):
        return None


def decimal_string(value: Decimal) -> str:
    rendered = format(value, "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
