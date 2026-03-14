"""Margin benchmarks for limpieza (cleaning supplies) vertical.

B2B/B2C mix with recurring clients and thin margins.
Healthy net margin range: 20%-35%.
"""

from app.heuristics.verticals import MarginBenchmark

BENCHMARK = MarginBenchmark(
    critical_below=0.10,   # < 10% → critical
    warning_below=0.20,    # < 20% → warning
    healthy_min=0.20,      # 20%-35% → healthy
    healthy_max=0.35,      # >= 35% → excellent
)
