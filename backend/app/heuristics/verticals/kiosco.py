"""Margin benchmarks for kiosco vertical.

Kioscos operate with high transaction frequency and thin margins.
Healthy net margin range: 18%-28%.
"""

from app.heuristics.verticals import MarginBenchmark

BENCHMARK = MarginBenchmark(
    critical_below=0.10,   # < 10%  → critical
    warning_below=0.18,    # < 18%  → warning
    healthy_min=0.18,      # 18%-28% → healthy   (warning_below == healthy_min by design)
    healthy_max=0.28,      # >= 28% → excellent
)
