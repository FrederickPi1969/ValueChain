# Earnings-call Pathfinder validation contract

The validator reads a normalized **20,000-character opening window** (not raw
HTML) and uses the whole cleaned text only for storage. An accepted source is a
complete earnings call when this opening has the characteristic call shape:

1. an event header identifying the company and requested quarter/year;
2. an operator or investor-relations welcome, often naming CEO/CFO;
3. the safe-harbor / forward-looking-statement and GAAP/non-GAAP disclaimer;
4. spoken management remarks with speaker turns, rather than a press-release
   table, slide deck, filing index, or search result list; and
5. where the call contains Q&A, an analyst/operator question-and-answer
   section after the prepared remarks.

Examples from the development set:

- Meta opens with `First Quarter 2026 Results Conference Call`, the date, IR
  director, a welcome, CEO/CFO names, forward-looking language, and then a
  named CEO's spoken remarks.
- Walmart opens with `Corrected Transcript`, `Q1 2026 Earnings Call`, named
  corporate and analyst participants, and then the operator/management call
  transcript.

The validator rejects SEC 10-K/10-Q/8-K filings, annual reports, press
releases, investor decks, transcript directory pages, market-news summaries,
and pages that merely link to a transcript. `Form 10-Q` or an SEC disclaimer
may appear *inside a genuine call's safe-harbor statement*, so the decision is
based on the document's spoken-call structure, not one keyword.
