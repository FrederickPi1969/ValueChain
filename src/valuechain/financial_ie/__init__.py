"""Auditable financial-information extraction experiments.

This package deliberately writes artifacts only. Database persistence belongs to a
later, separately reviewed integration step.
"""

from valuechain.financial_ie.models import BenchmarkCase, DocumentChunk, ExtractionRecord

__all__ = ["BenchmarkCase", "DocumentChunk", "ExtractionRecord"]
