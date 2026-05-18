#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

export VALUECHAIN_LLM_BASE_URL="${VALUECHAIN_LLM_BASE_URL:-http://192.168.50.18:31969/v1}"
export VALUECHAIN_LLM_API_KEY="${VALUECHAIN_LLM_API_KEY:-1969}"
export VALUECHAIN_EXTRACTION_MODEL="${VALUECHAIN_EXTRACTION_MODEL:-Qwen/Qwen3.5-4B}"
export VALUECHAIN_EMBEDDING_MODEL="${VALUECHAIN_EMBEDDING_MODEL:-qwen3-embed-0.6b}"
export VALUECHAIN_LLM_CONCURRENCY="${VALUECHAIN_LLM_CONCURRENCY:-6}"
export VALUECHAIN_FILINGS_PER_FORM="${VALUECHAIN_FILINGS_PER_FORM:-1}"
export VALUECHAIN_MIN_RELEVANCE_SCORE="${VALUECHAIN_MIN_RELEVANCE_SCORE:-1.8}"
export VALUECHAIN_RUN_ID="${VALUECHAIN_RUN_ID:-industry-hybrid-recall-v3}"

valuechain run \
  --forms 10-K,10-Q,8-K,20-F,6-K \
  --max-filings-per-company "$VALUECHAIN_FILINGS_PER_FORM" \
  --filing-selection form-balanced \
  --skip-yahoo \
  --min-relevance-score "$VALUECHAIN_MIN_RELEVANCE_SCORE" \
  --run-id "$VALUECHAIN_RUN_ID" \
  --run-label "Industry hybrid recall v3 - balanced 40" \
  --write-postgres \
  --extractor hybrid \
  --llm-concurrency "$VALUECHAIN_LLM_CONCURRENCY" \
  --embedding-merge \
  --embedding-threshold 0.92
