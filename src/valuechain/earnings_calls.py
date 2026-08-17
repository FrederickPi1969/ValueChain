"""Policy, discovery, and archival primitives for quarterly earnings calls.

The source-policy layer retains the existing evidence-only public contract.
Discovery treats every URL as a candidate until the Qwen judgement and the
deterministic eligibility gate agree it is the requested company's call.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import re
import shutil
import subprocess
import time
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlparse

import requests

from valuechain.config import Settings
from valuechain.llm_client import LLMConfig, OpenAICompatibleClient

FIRST_PARTY_KINDS = {"issuer_ir", "issuer_or_designated_webcast"}


@dataclass(frozen=True)
class EarningsCallSource:
    issuer: str
    source_url: str
    provider: str
    domain_kind: str
    event_date: str = ""
    fiscal_quarter: str = ""
    content_type: str = ""
    permits_local_processing: bool = True
    permits_redistribution: bool = False

    @property
    def hostname(self) -> str:
        return (urlparse(self.source_url).hostname or "").lower()

    def to_dict(self) -> dict[str, object]:
        return asdict(self) | {"hostname": self.hostname}


def assess_source(source: EarningsCallSource) -> tuple[bool, str]:
    """Return whether a source may enter the local extraction queue."""

    if urlparse(source.source_url).scheme not in {"https", "http"} or not source.hostname:
        return False, "invalid_source_url"
    if not source.permits_local_processing:
        return False, "local_processing_not_permitted"
    if source.provider == "sec_exhibits":
        return True, "sec_exhibit"
    if source.domain_kind in FIRST_PARTY_KINDS:
        return True, "first_party_source"
    if source.domain_kind == "configured":
        return True, "configured_provider"
    return False, "unapproved_source_kind"


def public_excerpt_limit(source: EarningsCallSource) -> int:
    """Keep the UI evidence-only even when a local parseable copy exists."""

    return 0 if source.permits_redistribution else 280


USER_AGENT = "Mozilla/5.0 (compatible; ValueChainEarningsResearch/0.1)"
SEARCH_ENGINES = ("google", "duckduckgo")
# Endeavor Tailscale route.  Do not replace with the public Cloudflare domain.
GOOGLE_SERP_URL = "http://100.114.26.88:10087/search"
PROXY_POOL_URL = "https://proxy.frederickpi.com/proxy/random/normal"
TRANSCRIPT_HINTS = ("transcript", "earnings call", "conference call", "webcast", "results call")
FILING_HINTS = ("10-k", "10-q", "8-k", "annual report", "sec filing", "edgar")
# These domains were empirically tested through both ULSCR and the configured
# authenticated OpenCLI profile in the Q1-2026 pilot.  They served an index or
# a paywalled/truncated excerpt instead of a complete call.  Candidates are
# still recorded for auditability, but never accepted as acquisition targets.
BLOCKED_TRANSCRIPT_DOMAINS = ("seekingalpha.com", "gurufocus.com", "fool.com")


@dataclass(frozen=True)
class SearchRule:
    quarter: str
    quarter_variants: tuple[str, ...]
    year_variants: tuple[str, ...]
    event_variants: tuple[str, ...]


@dataclass
class Candidate:
    url: str
    title: str
    snippet: str
    engine: str
    query: str
    source_type: str = "webpage"


@dataclass
class Judgement:
    candidate_index: int
    is_target: bool
    confidence: float
    content_kind: str
    reason: str


class _ResultParser(HTMLParser):
    """Small dependency-free parser for Google and DuckDuckGo result pages."""
    def __init__(self, engine: str) -> None:
        super().__init__()
        self.engine, self.items, self._href, self._parts = engine, [], "", []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            href = dict(attrs).get("href", "") or ""
            if href:
                self._href, self._parts = href, []

    def handle_data(self, data: str) -> None:
        if self._href:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag != "a" or not self._href:
            return
        url = _clean_result_url(self._href)
        title = " ".join("".join(self._parts).split())
        if url and title and url.startswith("http"):
            self.items.append((url, title))
        self._href, self._parts = "", []


class _TextParser(HTMLParser):
    """Extract visible text without scripts, styles, and navigation markup."""
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._ignored = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"}:
            self._ignored += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "head"} and self._ignored:
            self._ignored -= 1

    def handle_data(self, data: str) -> None:
        if not self._ignored:
            self.parts.append(data)


def build_search_rules(year: int, quarter: str, keywords: Iterable[str] = ()) -> list[SearchRule]:
    """Rule table for spelling and fiscal-year variants (documented in the guide)."""
    q = quarter.upper().replace(" ", "")
    if q not in {"Q1", "Q2", "Q3", "Q4"}:
        raise ValueError("quarter must be Q1, Q2, Q3, or Q4")
    ordinal = {"Q1": "first", "Q2": "second", "Q3": "third", "Q4": "fourth"}[q]
    yy = str(year)[-2:]
    event_terms = tuple(keywords) or (
        "earnings call transcript", "earnings call", "results webcast",
        "results conference call", "financial results briefing", "investor update",
        "results announcement webcast", "analyst conference",
    )
    return [SearchRule(q, (q, q.lower(), f"{ordinal} quarter", f"{ordinal}-quarter", f"{ordinal} quarterly"),
                       (str(year), f"FY{yy}", f"FY {yy}", f"fiscal {year}", f"fiscal year {year}"), event_terms)]


def build_queries(company: str, year: int, quarter: str, keywords: Iterable[str] = ()) -> list[str]:
    rule = build_search_rules(year, quarter, keywords)[0]
    # Put one canonical (Q1 + calendar-year) query for *every* event phrase
    # first; capped runs therefore retain the supplied keyword-list diversity.
    triples = [(rule.quarter_variants[0], rule.year_variants[0], event) for event in rule.event_variants]
    triples += [(q, y, rule.event_variants[0]) for q in rule.quarter_variants for y in rule.year_variants]
    triples += [(q, y, event) for event in rule.event_variants[1:] for q in rule.quarter_variants for y in rule.year_variants]
    queries = []
    for q, y, event in triples:
        queries.append(f'"{company}" "{q}" "{y}" "{event}"')
    return list(dict.fromkeys(queries))


def best_bet_queries(company: str, year: int, quarter: str) -> list[str]:
    """Four descending-probability searches, suitable for a strict query budget."""
    rule = build_search_rules(year, quarter)[0]
    q, spoken = rule.quarter_variants[0], rule.quarter_variants[2]
    calendar, fiscal = rule.year_variants[0], rule.year_variants[1]
    return [
        # Official IR teams, especially outside the US, commonly label the
        # artifact “earnings conference call” rather than “transcript”.
        # This is the highest-recall first bet supplied by the user.
        f'"{company}" "{q}" "{calendar}" "earnings conference call"',
        f'"{company}" "{q}" "{calendar}" "earnings call transcript"',
        f'"{company}" "{spoken}" "{calendar}" "earnings call transcript"',
        f'"{company}" "{q}" "{fiscal}" "earnings conference call"',
    ]


def _clean_result_url(href: str) -> str:
    href = html.unescape(href)
    if href.startswith("/url?"):
        return unquote(parse_qs(urlparse(href).query).get("q", [""])[0])
    if href.startswith("//duckduckgo.com/l/?"):
        return unquote(parse_qs(urlparse(href).query).get("uddg", [""])[0])
    return href


def _google_search(query: str, timeout: int = 30, proxies: dict[str, str] | None = None) -> list[Candidate]:
    """Use the Endeavor Google CSE pool, not brittle Google HTML scraping."""
    # This is an internal Tailnet hop. Endeavor's GoogleCSEClient applies a
    # rotating proxy to the *upstream Google CSE request*; proxying this hop
    # would make the external proxy attempt to route a private 100.x address.
    response = requests.get(GOOGLE_SERP_URL, params={"q": query, "num": 10}, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    return [Candidate(str(item["link"]), str(item.get("title", "")), str(item.get("snippet", "")), "google", query) for item in data.get("results", []) if isinstance(item, dict) and item.get("link")][:10]


def _duckduckgo_search(query: str, timeout: int = 20, proxies: dict[str, str] | None = None) -> list[Candidate]:
    response = requests.get("https://html.duckduckgo.com/html/", params={"q": query}, headers={"User-Agent": USER_AGENT}, timeout=timeout, proxies=proxies)
    response.raise_for_status()
    parser = _ResultParser("duckduckgo"); parser.feed(response.text)
    return [Candidate(url, title, "", "duckduckgo", query) for url, title in parser.items][:10]


def _rotating_proxy() -> dict[str, str] | None:
    """Fetch one proxy for a logical search group; never persist its credentials."""
    try:
        raw = requests.get(PROXY_POOL_URL, headers={"User-Agent": USER_AGENT}, timeout=8).json().get("proxy", "")
        host, port, user, password = str(raw).split(":", 3)
        return {"http": f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}", "https": f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}"}
    except (requests.RequestException, ValueError, AttributeError):
        return None


def search_query(query: str, *, proxies: dict[str, str] | None = None) -> tuple[str, list[Candidate]]:
    """Issue exactly one search: internal Google first, then DDG only if needed."""
    try:
        found = _google_search(query)
        if found:
            return "google", found
    except requests.RequestException:
        pass
    try:
        return "duckduckgo", _duckduckgo_search(query, proxies=proxies)
    except requests.RequestException:
        return "none", []


def search_candidates(company: str, year: int, quarter: str, *, max_queries: int = 32, settings: Settings | None = None) -> list[Candidate]:
    """Endeavor Google SERP + one rotating proxy; DDG only retries a failed query."""
    settings = settings or Settings()
    proxies = settings.proxies or _rotating_proxy()
    results: dict[str, Candidate] = {}
    for query in build_queries(company, year, quarter)[:max_queries]:
        _, found = search_query(query, proxies=proxies)
        for item in found:
            results.setdefault(item.url, item)
        time.sleep(0.25)
    return list(results.values())[:40]


def _judge_prompt(company: str, year: int, quarter: str, candidates: list[Candidate]) -> tuple[str, str]:
    system = """You are a precision financial-research link classifier. Return only valid JSON.
Accept a link only if it is a recording, webcast, or verbatim/near-verbatim transcript of the named company's requested quarterly earnings/results call. Official hosting is NOT required: a YouTube result whose title explicitly identifies the target company, requested quarter/year, and an earnings/results/conference call is a valid youtube_video acquisition lead even when its snippet is generic YouTube boilerplate or the uploader is third-party. Reject YouTube summaries, highlights, previews, reactions, and analysis videos that are not the call itself. A press release or results deck alone is not a transcript. Reject SEC filings (10-K, 10-Q, 8-K), annual reports, estimates, news summaries, unrelated companies, and calls for a different quarter/year. Fiscal-year labels such as FY26 may correctly refer to calendar 2026 only when the candidate itself makes the requested period clear. An explicit title is evidence; do not infer from a bare URL alone."""
    rows = [{"candidate_index": i, "url": c.url, "title": c.title, "snippet": c.snippet, "source_type": c.source_type} for i, c in enumerate(candidates)]
    user = f"Target: company={company!r}; period={quarter.upper()} {year}.\nCandidates:\n{json.dumps(rows, ensure_ascii=False)}\nReturn a JSON array; every object must have candidate_index, is_target, confidence (0..1), content_kind (official_transcript|third_party_transcript|official_webcast|youtube_video|other), and reason."
    return system, user


def judge_candidates(company: str, year: int, quarter: str, candidates: list[Candidate], settings: Settings | None = None) -> list[Judgement]:
    if not candidates:
        return []
    settings = settings or Settings()
    client = OpenAICompatibleClient(LLMConfig(settings.llm_base_url, settings.llm_api_key, settings.complex_model, timeout_s=180))
    system, user = _judge_prompt(company, year, quarter, candidates)
    payload = client.chat_json(system, user, max_tokens=max(800, 110 * len(candidates)))
    return apply_deterministic_candidate_rules(
        company,
        year,
        quarter,
        candidates,
        _parse_judgements(payload, len(candidates)),
    )


async def judge_candidates_async(company: str, year: int, quarter: str, candidates: list[Candidate], settings: Settings | None = None) -> list[Judgement]:
    """Async Qwen classifier used by the high-throughput batch worker."""
    if not candidates:
        return []
    settings = settings or Settings()

    async def classify_batch(batch: list[Candidate]) -> list[Judgement]:
        system, user = _judge_prompt(company, year, quarter, batch)
        last_error: Exception | None = None
        for attempt in range(3):
            client = OpenAICompatibleClient(
                LLMConfig(
                    settings.llm_base_url,
                    settings.llm_api_key,
                    settings.complex_model,
                    timeout_s=180,
                )
            )
            try:
                payload = await client.chat_json_async(
                    system,
                    user,
                    max_tokens=max(800, 110 * len(batch)),
                )
                parsed = _parse_judgements(payload, len(batch))
                indexes = [item.candidate_index for item in parsed]
                if sorted(indexes) != list(range(len(batch))) or len(set(indexes)) != len(indexes):
                    raise ValueError(
                        f"Qwen judgement indexes were incomplete or duplicated: {indexes}"
                    )
                return parsed
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                last_error = exc
                await asyncio.sleep(0.6 * (attempt + 1))
            finally:
                await client.aclose()
        raise RuntimeError(f"Qwen returned invalid JSON after 3 attempts: {last_error}")

    async def classify_resilient(batch: list[Candidate], offset: int) -> list[Judgement]:
        try:
            parsed = await classify_batch(batch)
        except RuntimeError:
            if len(batch) == 1:
                raise
            midpoint = len(batch) // 2
            left = await classify_resilient(batch[:midpoint], offset)
            right = await classify_resilient(batch[midpoint:], offset + midpoint)
            return left + right
        return [
            Judgement(
                candidate_index=item.candidate_index + offset,
                is_target=item.is_target,
                confidence=item.confidence,
                content_kind=item.content_kind,
                reason=item.reason,
            )
            for item in parsed
        ]

    return apply_deterministic_candidate_rules(
        company,
        year,
        quarter,
        candidates,
        await classify_resilient(candidates, 0),
    )


def _explicit_youtube_call_title(
    company: str,
    year: int,
    quarter: str,
    candidate: Candidate,
) -> bool:
    host = urlparse(candidate.url).netloc.lower().removeprefix("www.")
    if host not in {"youtube.com", "youtu.be", "m.youtube.com"}:
        return False
    title = candidate.title.lower()
    normalized_quarter = quarter.upper()
    ordinal = {"Q1": "first", "Q2": "second", "Q3": "third", "Q4": "fourth"}.get(
        normalized_quarter
    )
    if ordinal is None:
        return False
    yy = str(year)[-2:]
    period_match = bool(
        re.search(rf"\b{re.escape(normalized_quarter.lower())}\b", title)
        or f"{ordinal} quarter" in title
    ) and bool(
        re.search(rf"\b{year}\b", title)
        or re.search(rf"\bfy\s*{re.escape(yy)}\b", title)
    )
    call_match = bool(
        re.search(
            r"\b(?:earnings|results)(?:\s+conference)?\s+call\b|"
            r"\b(?:earnings|results)\s+webcast\b",
            title,
        )
    )
    if not period_match or not call_match:
        return False

    ticker_match = re.search(r"\(([A-Za-z0-9.\-]{1,12})\)\s*$", company)
    ticker = ticker_match.group(1).lower() if ticker_match else ""
    if ticker and re.search(rf"(?:\$|\b){re.escape(ticker)}\b", title):
        return True
    company_name = company[: ticker_match.start()].strip() if ticker_match else company
    stopwords = {
        "company", "corporation", "corp", "inc", "incorporated", "limited",
        "ltd", "plc", "group", "holding", "holdings", "the", "and", "co",
    }
    identity_tokens = [
        token
        for token in re.findall(r"[a-z0-9]+", company_name.lower())
        if len(token) >= 4 and token not in stopwords
    ]
    return any(re.search(rf"\b{re.escape(token)}\b", title) for token in identity_tokens)


def apply_deterministic_candidate_rules(
    company: str,
    year: int,
    quarter: str,
    candidates: list[Candidate],
    judgements: list[Judgement],
) -> list[Judgement]:
    by_index = {item.candidate_index: item for item in judgements}
    for index, candidate in enumerate(candidates):
        if _explicit_youtube_call_title(company, year, quarter, candidate):
            prior = by_index.get(index)
            by_index[index] = Judgement(
                index,
                True,
                max(0.95, prior.confidence if prior is not None else 0.0),
                "youtube_video",
                "Deterministic rule: title explicitly matches company, period, and earnings call.",
            )
    return [by_index[index] for index in sorted(by_index)]


def _parse_judgements(payload: object, candidate_count: int) -> list[Judgement]:
    if not isinstance(payload, list):
        raise TypeError("Qwen judgement response was not a JSON list")
    allowed = {"official_transcript", "third_party_transcript", "official_webcast", "youtube_video", "other"}
    return [Judgement(int(x["candidate_index"]), bool(x["is_target"]), float(x["confidence"]), str(x["content_kind"]) if str(x["content_kind"]) in allowed else "other", str(x.get("reason", ""))) for x in payload if isinstance(x, dict) and int(x.get("candidate_index", -1)) in range(candidate_count)]


def eligible(candidate: Candidate, judgement: Judgement) -> bool:
    text = f"{candidate.title} {candidate.snippet} {candidate.url}".lower()
    host = urlparse(candidate.url).netloc.lower().removeprefix("www.")
    blocked = any(host == domain or host.endswith(f".{domain}") for domain in BLOCKED_TRANSCRIPT_DOMAINS)
    return judgement.is_target and judgement.confidence >= 0.70 and judgement.content_kind != "other" and not blocked and not any(x in text for x in FILING_HINTS)


def fetch_text(candidate: Candidate, destination: Path, settings: Settings | None = None) -> Path | None:
    """Archive HTML text, or use yt-dlp's auto captions for a YouTube result."""
    destination.mkdir(parents=True, exist_ok=True)
    settings = settings or Settings()
    proxies = settings.proxies or _rotating_proxy()
    if "youtube.com/" in candidate.url or "youtu.be/" in candidate.url:
        if not shutil.which("yt-dlp"):
            return None
        base = destination / "youtube"
        command = ["yt-dlp", "--skip-download", "--write-auto-subs", "--sub-langs", "en", "--sub-format", "vtt"]
        if proxies and proxies.get("https"):
            command += ["--proxy", proxies["https"]]
        command += ["-o", str(base) + ".%(ext)s", candidate.url]
        subprocess.run(command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        vtts = list(destination.glob("youtube*.vtt"))
        if not vtts:
            return None
        text = re.sub(r"(?m)^\d\d:\d\d:\d\d\.\d\d\d.*$|^WEBVTT.*$|^Kind:.*$|^Language:.*$|^\s*$", "", vtts[0].read_text(errors="ignore"))
        text = re.sub(r"<[^>]+>", "", text)
        out = destination / "transcript.txt"; out.write_text(text, encoding="utf-8"); return out
    try:
        response = requests.get(candidate.url, headers={"User-Agent": USER_AGENT}, timeout=30, proxies=proxies)
        response.raise_for_status()
    except requests.RequestException:
        return None
    content_type = response.headers.get("content-type", "").lower()
    if "pdf" in content_type or candidate.url.lower().split("?")[0].endswith(".pdf"):
        if not shutil.which("pdftotext"):
            return None
        pdf = destination / "source.pdf"; pdf.write_bytes(response.content)
        out = destination / "transcript.txt"
        completed = subprocess.run(["pdftotext", "-layout", str(pdf), str(out)], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
        return out if completed.returncode == 0 and out.exists() and out.stat().st_size > 800 else None
    text_parser = _TextParser(); text_parser.feed(response.text)
    plain = re.sub(r"\s+", " ", " ".join(text_parser.parts)).strip()
    if len(plain) < 800:
        return None
    out = destination / "transcript.txt"; out.write_text(plain, encoding="utf-8"); return out


def run(company: str, year: int, quarter: str, output_dir: Path, max_queries: int = 4, high_confidence: float = 0.90) -> dict:
    """Four-query bounded, sequential discovery with an evidence-preserving manifest."""
    if not 1 <= max_queries <= 4:
        raise ValueError("max_queries must be between 1 and 4")
    settings, proxies = Settings(), None
    proxies = settings.proxies or _rotating_proxy()
    executed: list[dict] = []
    all_candidates: list[Candidate] = []
    all_judgements: list[dict] = []
    accepted: list[tuple[Candidate, Judgement]] = []
    stopped_early = False
    for query in best_bet_queries(company, year, quarter)[:max_queries]:
        engine, found = search_query(query, proxies=proxies)
        # Store every returned result before filtering, including irrelevant links.
        offset = len(all_candidates)
        all_candidates.extend(found)
        judgements = judge_candidates(company, year, quarter, found, settings)
        indexed = []
        for judgement in judgements:
            global_judgement = asdict(judgement) | {"candidate_index": offset + judgement.candidate_index}
            indexed.append(global_judgement); all_judgements.append(global_judgement)
            candidate = found[judgement.candidate_index]
            if eligible(candidate, judgement):
                accepted.append((candidate, judgement))
        executed.append({"query": query, "engine": engine, "results": [asdict(x) for x in found], "judgements": indexed})
        if any(eligible(found[j.candidate_index], j) and j.confidence >= high_confidence for j in judgements):
            stopped_early = True
            break
        time.sleep(0.25)
    record = {"company": company, "year": year, "quarter": quarter.upper(), "search_engines": list(SEARCH_ENGINES), "query_budget": max_queries, "high_confidence_threshold": high_confidence, "executed_queries": executed, "stopped_early": stopped_early, "candidates": [asdict(x) for x in all_candidates], "judgements": all_judgements, "accepted": []}
    seen_urls: set[str] = set()
    for rank, (candidate, judgement) in enumerate(accepted, 1):
        if candidate.url in seen_urls:
            continue
        seen_urls.add(candidate.url)
        text_path = fetch_text(candidate, output_dir / f"{rank:02d}")
        record["accepted"].append({"candidate": asdict(candidate), "judgement": asdict(judgement), "transcript_path": str(text_path) if text_path else None})
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "manifest.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("company"); parser.add_argument("--year", type=int, required=True); parser.add_argument("--quarter", required=True)
    parser.add_argument("--output-dir", type=Path, required=True); parser.add_argument("--max-queries", type=int, default=4); parser.add_argument("--high-confidence", type=float, default=0.90)
    args = parser.parse_args()
    print(json.dumps(run(args.company, args.year, args.quarter, args.output_dir, args.max_queries, args.high_confidence), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
