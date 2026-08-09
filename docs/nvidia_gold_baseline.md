# NVIDIA Gold Benchmark v1 baseline

Run date: 2026-08-08  
Pipeline artifact: `five-company-sec-v2`  
Gold data: `tests/gold/nvidia_v1.json`

```text
expected relationships: 7
predicted relationships: 7
matched: 7
false positives: 0
false negatives: 0
forbidden detections: 0
direction errors: 0
entity-resolution mismatches: 0
precision: 1.000
recall: 1.000
F1: 1.000
```

This is a deliberately narrow regression baseline, not an estimate of the system's overall supply-chain accuracy or the complete NVIDIA supply chain. Any extraction, entity-resolution, canonicalization, or review-state change should rerun this command before it is accepted:

```bash
valuechain evaluate --run-id five-company-sec-v2 --gold tests/gold/nvidia_v1.json
```
