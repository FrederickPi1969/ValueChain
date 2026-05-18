#!/usr/bin/env bash
set -uo pipefail

cd "$(dirname "$0")/.."

export VALUECHAIN_LLM_BASE_URL="${VALUECHAIN_LLM_BASE_URL:-http://192.168.50.18:31969/v1}"
export VALUECHAIN_LLM_API_KEY="${VALUECHAIN_LLM_API_KEY:-1969}"
export VALUECHAIN_EXTRACTION_MODEL="${VALUECHAIN_EXTRACTION_MODEL:-Qwen/Qwen3.5-4B}"
export VALUECHAIN_EMBEDDING_MODEL="${VALUECHAIN_EMBEDDING_MODEL:-qwen3-embed-0.6b}"
export VALUECHAIN_LLM_CONCURRENCY="${VALUECHAIN_LLM_CONCURRENCY:-6}"

valuechain run \
  --forms 10-K,10-Q,8-K \
  --max-filings-per-company 3 \
  --skip-yahoo \
  --run-id industry-hybrid-40-3f \
  --run-label "Industry hybrid - 40 companies / 3 filings" \
  --write-postgres \
  --extractor hybrid \
  --llm-concurrency "$VALUECHAIN_LLM_CONCURRENCY" \
  --embedding-merge \
  --embedding-threshold 0.92
