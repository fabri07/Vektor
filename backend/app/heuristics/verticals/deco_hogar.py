"""Margin benchmarks for decoración hogar vertical.

Home decoration businesses have higher ticket sizes and seasonal demand.
Healthy net margin range: 30%-45%.
"""

from app.heuristics.verticals import MarginBenchmark

BENCHMARK = MarginBenchmark(
    critical_below=0.15,   # < 15% → critical
    warning_below=0.30,    # < 30% → warning
    healthy_min=0.30,      # 30%-45% → healthy
    healthy_max=0.45,      # >= 45% → excellent
)
