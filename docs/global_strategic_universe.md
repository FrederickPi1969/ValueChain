# Global Strategic Company Universe

## Objective

The acquisition scope is not limited to AI or semiconductors. The target is a
strategically useful global corporate disclosure universe for institutional
research:

```text
complete United States issuer universe
+ complete mainland China issuer universe
+ Global Strategic 1000
```

The Global Strategic 1000 covers important non-US and non-mainland-China
companies across technology, energy, power, transportation, industrial
equipment, materials, finance, healthcare, telecommunications, food systems,
defense, and physical infrastructure. It includes the existing curated Japan
and South Korea universes.

The selection unit is an `issuer_group`, not a ticker. ADRs, dual listings,
multiple share classes, and local depositary receipts must not consume separate
slots for the same operating group.

## Scope Boundaries

- United States and mainland China are complete acquisition universes and do
  not consume Global Strategic 1000 slots.
- Japan currently contributes 92 curated groups and South Korea contributes 89.
- The remaining 819 slots cover other major markets and strategic regional
  champions.
- A company may be in scope even when its market capitalization is below a
  broad-market cutoff if it controls scarce capacity, critical infrastructure,
  a transport corridor, a strategic resource, or a difficult-to-substitute
  technology.
- A company is not included merely because it is locally large. The selection
  must have a documented strategic, systemic, cross-border, or ETF-relevance
  rationale.

## Regional Allocation

Regional allocations are portfolio limits, not automatic inclusion lists.
Unused slots may move to another region only through a reviewed quarterly
universe release.

| Region | Target groups |
| --- | ---: |
| Europe, United Kingdom, Switzerland, and Nordics | 300 |
| Japan | 92 |
| South Korea | 89 |
| Taiwan | 75 |
| India | 75 |
| Canada | 55 |
| Australia and New Zealand | 50 |
| Hong Kong and non-A-share Hong Kong issuers | 40 |
| Singapore | 20 |
| Brazil | 35 |
| Middle East, Israel, and Turkey | 60 |
| Southeast Asia excluding Singapore | 50 |
| Mexico and Latin America excluding Brazil | 35 |
| Africa | 24 |
| **Total** | **1,000** |

Broad, liquid official or institutional benchmark universes are candidate
denominators, not final selections. Examples include STOXX Europe 600, FTSE
TWSE Taiwan 50 and Mid-Cap 100, Nifty 50, S&P/TSX 60, S&P/ASX 50 and 100,
Hang Seng Index, Straits Times Index, Ibovespa, MSCI Tadawul 30, and FADX 15.
Strategic overrides are applied after benchmark and liquidity screening.

## Primary Sector Allocation

Each issuer group receives one primary strategic sector for quota accounting.
Secondary sector tags may be added without consuming another slot.

| Primary strategic sector | Target groups | Coverage examples |
| --- | ---: | --- |
| Digital technology and semiconductors | 150 | chips, equipment, servers, software, cloud, payments |
| Energy and power | 160 | oil, gas, LNG, grids, nuclear, renewable generation |
| Industrial and capital equipment | 120 | automation, electrical equipment, machinery, robotics |
| Transportation and logistics | 110 | rail, shipping, ports, airlines, airports, parcel networks |
| Mining, materials, and chemicals | 100 | copper, iron ore, lithium, uranium, steel, chemicals |
| Financial infrastructure | 110 | systemic banks, exchanges, insurance, clearing, payments |
| Telecommunications and networks | 70 | operators, satellites, subsea cables, network equipment |
| Healthcare and pharmaceuticals | 65 | pharma, vaccines, devices, contract manufacturing |
| Food, agriculture, and consumer supply chains | 55 | fertilizer, food, agriculture, global retail and brands |
| Defense and security | 30 | aerospace defense, military electronics, cyber security |
| Water, waste, and physical infrastructure | 30 | water, waste, infrastructure and utility operators |
| **Total** | **1,000** | |

## Selection Score

Every selected group must retain the component scores and supporting notes.

| Component | Weight | Question |
| --- | ---: | --- |
| Strategic centrality | 25% | Does the group sit on important upstream or downstream paths? |
| Global revenue or export exposure | 15% | Is its activity materially cross-border? |
| Critical infrastructure role | 15% | Does it operate essential physical or financial infrastructure? |
| Substitution difficulty | 10% | How quickly could customers replace it? |
| Scarce capacity or resource control | 10% | Does it control constrained capacity, resources, or routes? |
| Market capitalization and ETF relevance | 10% | Is it investable and material to institutional portfolios? |
| Cross-border operating footprint | 10% | Does it connect several markets or jurisdictions? |
| Disclosure accessibility | 5% | Can authoritative disclosures be monitored reproducibly? |

Scores are normalized to 0-100. The default entry threshold is 60. A group
below 60 requires an explicit mandatory-override reason and reviewer approval.
Disclosure accessibility affects operating cost but must not exclude an
otherwise systemically important company; it may instead lower the monitoring
tier until a lawful source is available.

## Mandatory Overrides

The scoring process must explicitly consider:

- national or regional grid, port, railway, exchange, and telecom operators;
- national oil, gas, mining, fertilizer, and strategically important chemical companies;
- semiconductor, aerospace, defense, pharmaceutical, and industrial chokepoints;
- globally systemic or regionally critical banks, insurers, payment networks,
  clearing houses, and securities exchanges;
- companies controlling critical minerals, production capacity, transport
  routes, data centers, power infrastructure, or specialized manufacturing;
- major global food, agriculture, healthcare, and logistics networks whose
  disruption would propagate across markets.

## Monitoring Tiers

The 1,000-group cap does not mean downloading every announcement from every
company.

| Tier | Target | Default retention |
| --- | ---: | --- |
| S | 200 | All periodic reports, material announcements, contracts, capex disclosures, and event filings |
| A | 400 | All periodic reports and selected material announcements |
| B | 400 | Annual and interim reports; other documents remain on demand |

Tier assignment is independent of sector quota. Tier S emphasizes companies
with high propagation risk, scarce capacity, geopolitical sensitivity, or
frequent market-moving disclosures.

## Identity and Duplicate Control

The versioned universe dataset must include at least:

```text
universe_version
issuer_group_id
canonical_name
legal_name
country_of_risk
country_of_incorporation
primary_listing
primary_ticker
isin
lei
primary_disclosure_source
secondary_disclosure_sources
sec_covered
strategic_sector
secondary_sectors
strategic_score
monitoring_tier
selection_reason
mandatory_override_reason
effective_from
effective_to
reviewed_at
```

`primary_disclosure_source` determines the default archive route. A company
that already files through SEC EDGAR remains in the strategic universe, but its
native source should only add disclosures that are absent from EDGAR. Exact
document hashes, report periods, native filing identifiers, and canonical
document types are used to prevent duplicate storage.

## Review and Versioning

The universe is reviewed quarterly and published as an immutable versioned CSV.
Each review must:

1. refresh official issuer and benchmark denominators;
2. reconcile mergers, delistings, name changes, and listing migrations;
3. recompute quantitative scores with an `as_of_date`;
4. record additions, removals, tier changes, and the reason for every change;
5. preserve prior versions so historical coverage can be reconstructed;
6. avoid automatically deleting already archived documents when a company exits.

Recommended release artifacts:

```text
data/universe/global_strategic_1000/YYYY-QN/universe.csv
data/universe/global_strategic_1000/YYYY-QN/changes.csv
data/universe/global_strategic_1000/YYYY-QN/methodology.json
data/universe/global_strategic_1000/YYYY-QN/validation_report.json
```

High-volume generated datasets remain outside Git. The repository should retain
the methodology, schema, small validation samples, and generation code.

## Storage Planning

At the current acquisition profile, the Global Strategic 1000 is expected to
add approximately 100-200 GB of raw disclosure data per year. Together with the
complete US and mainland-China universes, the working raw-data estimate is
approximately 470-600 GB per year.

The estimate assumes tiered retention, native/SEC deduplication, selective
exhibit storage, and no blanket archive of low-value daily announcements. It
must be recalculated from actual bytes after each quarterly universe release.
The 2020-2026 collection window fits the current HDD plan; a complete 15-year
archive is expected to require additional storage.

