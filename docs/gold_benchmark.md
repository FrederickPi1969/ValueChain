# NVIDIA Gold Benchmark v1

`tests/gold/nvidia_v1.json` formalizes the seven NVIDIA supplier relationships that were manually reviewed during the NVIDIA-only phase. It is deliberately small: it is a regression contract, not an exhaustive model of NVIDIA's supply chain.

Run it against a saved canonical run:

```bash
valuechain evaluate --run-id five-company-sec-v2 --gold tests/gold/nvidia_v1.json
```

The evaluator reports precision, recall, F1, false positives, false negatives, forbidden relationship detections, reversed-direction errors, and likely entity-resolution mismatches. Precision is the primary guardrail because a false supply-chain edge is more harmful than a missed candidate.

The gold set includes explicit negative expectations for Alphabet, Microsoft, and Amazon as NVIDIA suppliers. It should grow only from documented human review; do not infer new labels from model output.
