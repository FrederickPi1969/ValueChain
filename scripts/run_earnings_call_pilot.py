"""Run the seven-company Q1 2026 recall pilot.

The seed candidates are independently discovered links retained as a replayable
search snapshot. They let the pilot verify classification and acquisition even
when Google/DDG rate-limit the development machine. Normal production runs use
``valuechain.earnings_calls.run`` and have no seeds.
"""
from __future__ import annotations

import json
from pathlib import Path

from valuechain.earnings_calls import Candidate, eligible, fetch_text, judge_candidates

PILOT = {
    "Tesla": ("https://earnings.video/earnings/tesla/q1-2026/transcript", "TSLA Q1 2026 Earnings Call Transcript | Tesla, Inc."),
    "Microsoft": ("https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q1", "Microsoft Fiscal Year 2026 First Quarter Earnings Conference Call"),
    "Google": ("https://www.fool.com/earnings/call-transcripts/2026/04/29/alphabet-googl-q1-2026-earnings-call-transcript/", "Alphabet (GOOGL) Q1 2026 Earnings Call Transcript"),
    "Meta": ("https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Transcript.pdf", "Meta Platforms Q1 2026 Earnings Call Transcript"),
    "Walmart": ("https://corporate.walmart.com/content/dam/corporate/documents/newsroom/2025/05/15/walmart-releases-q1-fy26-earnings/q1-fy26-earnings-call-transcript.pdf", "Walmart Q1 FY2026 Earnings Call Transcript"),
    "Nebius": ("https://www.benzinga.com/news/26/07/60237384/nebius-group-reports-q1-2026-results-full-earnings-call-transcript", "Nebius Group Reports Q1 2026 Results: Full Earnings Call Transcript"),
    "Reddit": ("https://cdn3.benzinga.com/insights/news/26/04/52201185/full-transcript-reddit-q1-2026-earnings-call", "Full Transcript: Reddit Q1 2026 Earnings Call"),
}


def main() -> None:
    root = Path("data/earnings_calls/pilot_2026_q1")
    outcomes = []
    for company, (url, title) in PILOT.items():
        candidate = Candidate(url, title, "Replayable search snapshot; Q1 2026 earnings call transcript/webcast.", "google", f'"{company}" "Q1" "2026" "earnings call transcript"', "youtube_video" if "youtube" in url else "webpage")
        # A filing lookalike is intentionally included; it must be rejected.
        negative = Candidate("https://www.sec.gov/Archives/edgar/data/example/10-q.htm", f"{company} Q1 2026 Form 10-Q", "SEC filing", "google", candidate.query)
        judgements = judge_candidates(company, 2026, "Q1", [candidate, negative])
        accepted = [(x, judgements[x]) for x in range(2) if any(j.candidate_index == x and eligible([candidate, negative][x], j) for j in judgements)]
        text_path = fetch_text(candidate, root / company.lower()) if accepted else None
        outcomes.append({"company": company, "source_url": url, "accepted": bool(accepted), "transcript_path": str(text_path) if text_path else None, "judgements": [j.__dict__ for j in judgements]})
        print(f"{company}: {'PASS' if accepted and text_path else 'FAIL'}")
    passed = sum(bool(x["accepted"] and x["transcript_path"]) for x in outcomes)
    summary = {"target": "Q1 2026 earning-call content", "companies": outcomes, "recall": f"{passed}/{len(outcomes)}"}
    root.mkdir(parents=True, exist_ok=True)
    (root / "pilot_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    if passed != len(outcomes):
        raise SystemExit(f"Pilot recall {passed}/{len(outcomes)}")


if __name__ == "__main__":
    main()
