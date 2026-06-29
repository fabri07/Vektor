"""Tests para stats_enrichment.py — glue entre handlers y stats_engine.

Verifica:
- enrich_timeseries: estadística/proyección/anomalías sobre serie diaria.
- Proyección gateada por días activos (ceros estructurales no sesgan la tendencia).
- Anomalías sobre días activos (días cerrados no se reportan como "caida").
- enrich_distribution: concentración (sin proyección).
- Guardas insufficient_data se propagan verbatim (no-invention).
- El módulo no llama LLM.
"""
from __future__ import annotations


def test_no_llm_import() -> None:
    from app.application.agents.shared import stats_enrichment

    assert not hasattr(stats_enrichment, "anthropic")


class TestEnrichTimeseries:
    def test_serie_con_tendencia(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_timeseries

        # 14 días activos, tendencia creciente clara
        series = [float(100 + i * 10) for i in range(14)]
        result = enrich_timeseries(series)
        assert result["dias_activos"] == 14
        assert result["dias_totales"] == 14
        assert result["estadistica"]["n"] == 14
        # proyección corre (>=7 activos, ratio 1.0)
        assert result["proyeccion"]["status"] == "ok"
        assert result["proyeccion"]["trend_slope"] > 0

    def test_ceros_estructurales_no_son_caida(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_timeseries

        # patrón: 6 días de venta + 1 día cerrado (0), repetido → los ceros NO
        # deben aparecer como anomalía "caida"
        series: list[float] = []
        for _ in range(8):
            series.extend([100.0, 110.0, 105.0, 95.0, 100.0, 108.0, 0.0])
        result = enrich_timeseries(series)
        tipos = {a["type"] for a in result["anomalias"]}
        assert "caida" not in tipos or all(a["value"] != 0.0 for a in result["anomalias"])
        # ningún valor 0.0 en las anomalías (los ceros se excluyen)
        assert all(a["value"] != 0.0 for a in result["anomalias"])

    def test_proyeccion_gateada_por_pocos_activos(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_timeseries

        # 60 días, solo 5 con venta → ratio 0.083 < 0.5 → proyección insuficiente
        series = [0.0] * 60
        for i in range(5):
            series[i] = 100.0
        result = enrich_timeseries(series)
        assert result["dias_activos"] == 5
        assert result["proyeccion"]["status"] == "insufficient_data"

    def test_proyeccion_gateada_por_ratio_bajo(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_timeseries

        # 30 días totales, 8 activos (>=7) pero ratio 0.27 < 0.5 → insuficiente
        series = [0.0] * 30
        for i in range(8):
            series[i] = 100.0
        result = enrich_timeseries(series)
        assert result["dias_activos"] == 8
        assert result["proyeccion"]["status"] == "insufficient_data"

    def test_serie_vacia(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_timeseries

        result = enrich_timeseries([])
        assert result["dias_activos"] == 0
        assert result["dias_totales"] == 0
        assert result["estadistica"]["status"] == "insufficient_data"
        assert result["proyeccion"]["status"] == "insufficient_data"
        assert result["anomalias"] == []


class TestEnrichDistribution:
    def test_distribucion_suficiente(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_distribution

        result = enrich_distribution([500.0, 300.0, 150.0, 30.0, 20.0])
        conc = result["concentracion"]
        assert conc["n"] == 5
        assert conc["top1_share"] == 0.5

    def test_distribucion_insuficiente(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_distribution

        result = enrich_distribution([100.0, 200.0])  # n=2 < 5
        assert result["concentracion"]["status"] == "insufficient_data"

    def test_no_proyeccion_en_distribucion(self) -> None:
        from app.application.agents.shared.stats_enrichment import enrich_distribution

        result = enrich_distribution([500.0, 300.0, 150.0, 30.0, 20.0])
        assert "proyeccion" not in result
        assert "estadistica" not in result
