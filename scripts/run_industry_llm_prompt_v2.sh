#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

export VALUECHAIN_LLM_BASE_URL="${VALUECHAIN_LLM_BASE_URL:-http://192.168.50.18:31969/v1}"
export VALUECHAIN_LLM_API_KEY="${VALUECHAIN_LLM_API_KEY:-1969}"
export VALUECHAIN_EXTRACTION_MODEL="${VALUECHAIN_EXTRACTION_MODEL:-Qwen/Qwen3.5-4B}"
export VALUECHAIN_EMBEDDING_MODEL="${VALUECHAIN_EMBEDDING_MODEL:-qwen3-embed-0.6b}"
export VALUECHAIN_LLM_CONCURRENCY="${VALUECHAIN_LLM_CONCURRENCY:-6}"
export VALUECHAIN_FILINGS_PER_FORM="${VALUECHAIN_FILINGS_PER_FORM:-2}"
export VALUECHAIN_MIN_RELEVANCE_SCORE="${VALUECHAIN_MIN_RELEVANCE_SCORE:-1.8}"
export VALUECHAIN_MAX_EXHIBITS_PER_FILING="${VALUECHAIN_MAX_EXHIBITS_PER_FILING:-6}"
export VALUECHAIN_FILING_DATE_FROM="${VALUECHAIN_FILING_DATE_FROM:-}"
export VALUECHAIN_FILING_DATE_TO="${VALUECHAIN_FILING_DATE_TO:-}"
export VALUECHAIN_RUN_ID="${VALUECHAIN_RUN_ID:-industry-sec-exhibits-v4}"
export VALUECHAIN_RUN_LABEL="${VALUECHAIN_RUN_LABEL:-Industry SEC exhibits v4 - expanded 79 x2}"

EXTRA_ARGS=()
if [ -n "$VALUECHAIN_FILING_DATE_FROM" ]; then
  EXTRA_ARGS+=(--filing-date-from "$VALUECHAIN_FILING_DATE_FROM")
fi
if [ -n "$VALUECHAIN_FILING_DATE_TO" ]; then
  EXTRA_ARGS+=(--filing-date-to "$VALUECHAIN_FILING_DATE_TO")
fi

valuechain run \
  --forms 10-K,10-Q,8-K,20-F,6-K \
  --max-filings-per-company "$VALUECHAIN_FILINGS_PER_FORM" \
  --filing-selection form-balanced \
  "${EXTRA_ARGS[@]}" \
  --skip-yahoo \
  --min-relevance-score "$VALUECHAIN_MIN_RELEVANCE_SCORE" \
  --run-id "$VALUECHAIN_RUN_ID" \
  --run-label "$VALUECHAIN_RUN_LABEL" \
  --write-postgres \
  --extractor hybrid \
  --llm-concurrency "$VALUECHAIN_LLM_CONCURRENCY" \
  --exhibit-types EX-10,EX-21,EX-99,EX-99.1 \
  --max-exhibits-per-filing "$VALUECHAIN_MAX_EXHIBITS_PER_FILING" \
  --embedding-merge \
  --embedding-threshold 0.92
