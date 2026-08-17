import asyncio
from types import SimpleNamespace

from valuechain import earnings_calls
from valuechain.earnings_calls import (
    Candidate,
    EarningsCallSource,
    Judgement,
    assess_source,
    best_bet_queries,
    build_queries,
    build_search_rules,
    eligible,
    public_excerpt_limit,
)


def test_issuer_ir_source_is_allowed_but_not_redistributed() -> None:
    source = EarningsCallSource(
        issuer="NVIDIA",
        source_url="https://investor.nvidia.com/events-and-presentations/default.aspx",
        provider="issuer_ir_release",
        domain_kind="issuer_ir",
    )
    assert assess_source(source) == (True, "first_party_source")
    assert public_excerpt_limit(source) == 280


def test_unapproved_transcript_source_is_rejected() -> None:
    source = EarningsCallSource(
        issuer="NVIDIA",
        source_url="https://example.invalid/transcript",
        provider="unknown",
        domain_kind="third_party",
    )
    assert assess_source(source) == (False, "unapproved_source_kind")


def test_search_rule_includes_fiscal_and_spelled_variants():
    rule = build_search_rules(2026, "Q2")[0]
    assert "second quarter" in rule.quarter_variants
    assert "FY26" in rule.year_variants
    assert any("results webcast" == term for term in rule.event_variants)
    assert any('"second quarter"' in query and '"FY26"' in query for query in build_queries("Example Co", 2026, "Q2"))


def test_filing_is_never_eligible():
    candidate = Candidate("https://sec.gov/10-q", "Example Q1 2026 10-Q", "", "google", "q")
    judgement = Judgement(0, True, 0.99, "official_transcript", "wrong")
    assert not eligible(candidate, judgement)


def test_known_paywall_transcript_hosts_are_recorded_but_not_eligible():
    candidate = Candidate("https://seekingalpha.com/article/example", "Example Q1 2026 Earnings Call Transcript", "", "google", "q")
    judgement = Judgement(0, True, 0.99, "third_party_transcript", "title matches")
    assert not eligible(candidate, judgement)


def test_best_bets_are_four_and_start_with_exact_transcript_query():
    queries = best_bet_queries("Example Co", 2026, "Q2")
    assert len(queries) == 4
    assert '"Q2" "2026" "earnings conference call"' in queries[0]


def test_async_judge_splits_a_batch_after_repeated_invalid_json(monkeypatch):
    calls = 0

    class FakeClient:
        def __init__(self, config):
            pass

        async def chat_json_async(self, system, user, max_tokens):
            nonlocal calls
            calls += 1
            if calls <= 3:
                return {"not": "a judgement list"}
            return [
                {
                    "candidate_index": index,
                    "is_target": True,
                    "confidence": 0.9,
                    "content_kind": "third_party_transcript",
                    "reason": "mock",
                }
                for index in range(2)
            ]

        async def aclose(self):
            pass

    monkeypatch.setattr(earnings_calls, "OpenAICompatibleClient", FakeClient)
    candidates = [
        Candidate(f"https://example.com/{index}", f"Result {index}", "", "google", "q")
        for index in range(4)
    ]
    settings = SimpleNamespace(llm_base_url="local", llm_api_key="test", complex_model="qwen")
    verdicts = asyncio.run(
        earnings_calls.judge_candidates_async(
            "Example Corp",
            2026,
            "Q1",
            candidates,
            settings,
        )
    )
    assert [verdict.candidate_index for verdict in verdicts] == [0, 1, 2, 3]
    assert calls == 5


def test_explicit_youtube_earnings_call_title_overrides_officiality_rejection(
    monkeypatch,
):
    class FakeClient:
        def __init__(self, config):
            del config

        async def chat_json_async(self, system, user, max_tokens):
            del system, user, max_tokens
            return [
                {
                    "candidate_index": index,
                    "is_target": False,
                    "confidence": 0.9,
                    "content_kind": "youtube_video",
                    "reason": "third-party YouTube is not official",
                }
                for index in range(2)
            ]

        async def aclose(self):
            pass

    monkeypatch.setattr(earnings_calls, "OpenAICompatibleClient", FakeClient)
    candidates = [
        Candidate(
            "https://www.youtube.com/watch?v=call",
            "Verisk Analytics Inc ($VRSK) Q2 2026 Earnings Call - YouTube",
            "Enjoy the videos and music you love",
            "duckduckgo",
            "q",
        ),
        Candidate(
            "https://www.youtube.com/watch?v=summary",
            "Verisk Q2 2026 Earnings: What Investors Missed - YouTube",
            "Enjoy the videos and music you love",
            "duckduckgo",
            "q",
        ),
    ]
    settings = SimpleNamespace(
        llm_base_url="local", llm_api_key="test", complex_model="qwen"
    )
    verdicts = asyncio.run(
        earnings_calls.judge_candidates_async(
            "Verisk Analytics, Inc. (VRSK)", 2026, "Q2", candidates, settings
        )
    )
    assert verdicts[0].is_target
    assert verdicts[0].confidence == 0.95
    assert verdicts[0].content_kind == "youtube_video"
    assert not verdicts[1].is_target
