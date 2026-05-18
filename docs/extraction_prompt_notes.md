# Information Extraction Prompt Notes

## Rule Principles

- Extract only evidence-backed relations where the filing passage directly supports `subject -> object`.
- Prefer named counterparties, named organizations, named facilities, named geographies, or anonymous concentration labels with disclosed percentages.
- Do not let the LLM output generic dependency classes such as `supplier`, `customers`, or `cloud provider`; rules may create class candidates, but LLM output should be high precision.
- Cap LLM output to the strongest five relations per passage, with short evidence quotes, to avoid truncated JSON and runaway extraction.
- Separate current operating facts from hypothetical risk language. Conditional risk-factor language should remain `risk_hypothetical` unless the passage also directly states a present reliance.
- Strategic relations require explicit strategic partnership, alliance, collaboration agreement, joint development, joint venture, or co-investment wording.
- Strategic relation outputs must use `strategic` modality; otherwise they are dropped by schema validation.
- Product-market, competition, customer-benefit, business-segment, and self-product statements are not dependency relations.

## Failure Cases

- GE Vernova "Power" was incorrectly interpreted as `power_or_utility_dependency`; in context it was a business segment.
- Palantir passages mentioning AI, third parties, AWS, or Microsoft were incorrectly connected as cloud/supplier dependencies from loose co-occurrence.
- Arista competitor/supplier context produced `strategic_partner` edges without explicit strategic partnership language.
- Some Qwen outputs used relation labels such as `manufacturing_dependency` or `concentration_risk` as the object; these are now rejected by schema validation.
- Qwen can return malformed or truncated JSON on dense passages; single-passage LLM failures now return no records instead of crashing the batch.
- Recent-filing selection previously pulled only NVDA 8-K filings and missed 10-K/10-Q backbone disclosures; filing discovery now supports form-balanced selection.

## Examples

- Good: AMD says it relies on Taiwan Semiconductor Manufacturing Company Limited for wafers at 7nm or smaller nodes. Extract `AMD -> Taiwan Semiconductor Manufacturing Company Limited`, `foundry_dependency`, `current_fact`.
- Good: Arista says two end customers accounted for disclosed revenue percentages. Extract anonymous `Customer A` / `Customer B` concentration edges with the disclosed time scope.
- Good: Digital Realty discloses ownership interest in Digital Core REIT. Extract `subsidiary_or_control` only when the ownership/control relation is explicit.
- Bad: A passage says a company sells cloud or data center products. Do not extract `cloud_or_hosting_dependency` or `data_center_dependency` unless it relies on an external provider or constrained resource.
- Bad: A passage lists competitors such as Cisco, Broadcom, NVIDIA, Dell, or HPE. Do not extract `strategic_partner`.
