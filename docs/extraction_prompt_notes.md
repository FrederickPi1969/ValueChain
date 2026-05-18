# Information Extraction Prompt Notes

## Rule Principles

- Recall is now prioritized over precision for the prototype: extract evidence-backed named relations when available, and class-level exposure relations when the filing discloses a real dependency without naming the counterparty.
- Prefer named counterparties, named organizations, named facilities, named geographies, or anonymous concentration labels with disclosed percentages.
- Allow descriptive class objects such as `single-source suppliers`, `third-party data center providers`, `cloud computing platform providers`, `major customers`, `natural gas transportation suppliers`, and `fuel suppliers`.
- Do not output schema labels as objects. `supplier_dependency` is invalid as an object; `limited number of suppliers` is valid when supported by text.
- Cap LLM output to the strongest eight relations per passage, with short evidence quotes, to avoid truncated JSON and runaway extraction.
- Separate current operating facts from hypothetical risk language. Conditional risk-factor language should remain `risk_hypothetical` unless the passage also directly states a present reliance.
- Strategic relations require explicit strategic partnership, alliance, collaboration agreement, joint development, joint venture, or co-investment wording.
- Strategic relation outputs must use `strategic` modality; otherwise they are dropped by schema validation.
- Product-market, competition, customer-benefit, business-segment, and self-product statements are still not dependency relations.
- Hybrid mode is the default for large runs: deterministic rules provide recall-oriented class exposure candidates, and LLM output adds named or higher-context relations.

## Failure Cases

- GE Vernova "Power" was incorrectly interpreted as `power_or_utility_dependency`; in context it was a business segment.
- Palantir passages mentioning AI, third parties, AWS, or Microsoft were incorrectly connected as cloud/supplier dependencies from loose co-occurrence.
- Arista competitor/supplier context produced `strategic_partner` edges without explicit strategic partnership language.
- Some Qwen outputs used relation labels such as `manufacturing_dependency` or `concentration_risk` as the object; these are now rejected by schema validation.
- Qwen 3.5 4B can miss class-level exposure in LLM-only mode; Cisco, Datadog, NextEra, IBM, Oracle, Salesforce, and Dell showed many candidate passages but zero LLM-only evidence under the high-precision prompt.
- Qwen can return malformed or truncated JSON on dense passages; single-passage LLM failures now return no records instead of crashing the batch.
- Recent-filing selection previously pulled only NVDA 8-K filings and missed 10-K/10-Q backbone disclosures; filing discovery now supports form-balanced selection.
- Some inline SEC filings place item headings only in a table of contents near the end of the extracted text; section parsing now falls back to full filing text instead of treating those table-of-contents matches as real sections.

## Examples

- Good: AMD says it relies on Taiwan Semiconductor Manufacturing Company Limited for wafers at 7nm or smaller nodes. Extract `AMD -> Taiwan Semiconductor Manufacturing Company Limited`, `foundry_dependency`, `current_fact`.
- Good: Arista says two end customers accounted for disclosed revenue percentages. Extract anonymous `Customer A` / `Customer B` concentration edges with the disclosed time scope.
- Good: NextEra says FPL had firm transportation contracts with ten natural gas transportation suppliers. Extract class-level `natural gas transportation suppliers`, `power_or_utility_dependency`.
- Good: Salesforce discloses interruptions or delays from third-party data center hosting facilities and cloud computing platform providers. Extract class-level `data_center_dependency` and `cloud_or_hosting_dependency`.
- Good: Dell says it relies on single-source or limited-source vendors. Extract class-level `single-source or limited-source vendors`, `supplier_dependency`.
- Good: Digital Realty discloses ownership interest in Digital Core REIT. Extract `subsidiary_or_control` only when the ownership/control relation is explicit.
- Bad: A passage says a company sells cloud or data center products. Do not extract `cloud_or_hosting_dependency` or `data_center_dependency` unless it relies on an external provider or constrained resource.
- Bad: A passage lists competitors such as Cisco, Broadcom, NVIDIA, Dell, or HPE. Do not extract `strategic_partner`.
