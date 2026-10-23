"use client";

import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Plus,
  Upload,
  Trash2,
  Pencil,
  Megaphone,
  Info,
  TrendingUp,
} from "lucide-react";
import { PageWrapper } from "@/components/layout/PageWrapper";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Select } from "@/components/ui/Select";
import { Modal } from "@/components/ui/Modal";
import { StatCard } from "@/components/ui/StatCard";
import { EmptyState } from "@/components/ui/EmptyState";
import { UploadSizeHint } from "@/components/ui/UploadSizeHint";
import { MAX_UPLOAD_BYTES, MAX_UPLOAD_MB } from "@/constants/upload";
import { SmartTable, type SmartColumn } from "@/components/ui/SmartTable";
import { fieldDefinitionsService } from "@/services/fieldDefinitions.service";
import { buildEditableCustomFieldColumns } from "@/lib/customFieldsEditable";
import { AddColumnButton } from "@/features/customFields/AddColumnButton";
import { useSaveCustomField } from "@/features/customFields/useSaveCustomField";
import { SocialGuides } from "@/features/marketing/SocialGuides";
import { useToastStore } from "@/stores/toastStore";
import {
  marketingService,
  type SocialMetricResponse,
  type CreateSocialMetricPayload,
  type SocialPlatform,
  type MarketingDashboard,
} from "@/services/marketing.service";

// ── Constantes ─────────────────────────────────────────────────────────────

const PLATFORM_LABELS: Record<string, string> = {
  instagram: "Instagram",
  facebook: "Facebook",
  tiktok: "TikTok",
  other: "Otra",
};

const PLATFORM_OPTIONS: { value: SocialPlatform; label: string }[] = [
  { value: "instagram", label: "Instagram" },
  { value: "facebook", label: "Facebook" },
  { value: "tiktok", label: "TikTok" },
  { value: "other", label: "Otra" },
];

const PLATFORM_FILTER_OPTIONS = [
  { value: "", label: "Todas las plataformas" },
  ...PLATFORM_OPTIONS,
];

const DAYS_OPTIONS = [
  { value: "7", label: "Últimos 7 días" },
  { value: "30", label: "Últimos 30 días" },
  { value: "90", label: "Últimos 90 días" },
];

const CSV_COLUMNS = [
  "platform",
  "metric_date",
  "followers",
  "reach",
  "engagement",
  "ads_spend_ars",
] as const;

// ── Helpers de formato ──────────────────────────────────────────────────────

function fmtInt(n: number): string {
  return new Intl.NumberFormat("es-AR", { maximumFractionDigits: 0 }).format(n);
}

function fmtArs(n: number): string {
  return new Intl.NumberFormat("es-AR", {
    style: "currency",
    currency: "ARS",
    maximumFractionDigits: 0,
  }).format(n);
}

function platformLabel(platform: string): string {
  return PLATFORM_LABELS[platform] ?? platform;
}

// ── Saneo defensivo del dashboard (la forma puede variar levemente) ─────────

function safeNumber(value: unknown, fallback = 0): number {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

interface NormalizedDashboard {
  days: number;
  platforms: {
    platform: string;
    latest_followers: number;
    total_reach: number;
    total_engagement: number;
    total_ads_spend_ars: number;
  }[];
  totals: {
    followers: number;
    reach: number;
    engagement: number;
    ads_spend_ars: number;
  };
  ads_vs_sales: {
    ads_spend_ars: number;
    revenue_ars: number;
    ratio: number | null;
  };
}

function normalizeDashboard(raw: MarketingDashboard | undefined): NormalizedDashboard {
  const ads = raw?.ads_vs_sales;
  const ratio = ads?.ratio;
  return {
    days: safeNumber(raw?.days, 30),
    platforms: Array.isArray(raw?.platforms)
      ? raw.platforms.map((p) => ({
          platform: typeof p?.platform === "string" ? p.platform : "other",
          latest_followers: safeNumber(p?.followers),
          total_reach: safeNumber(p?.reach),
          total_engagement: safeNumber(p?.engagement),
          total_ads_spend_ars: safeNumber(p?.ads_spend_ars),
        }))
      : [],
    totals: {
      followers: safeNumber(raw?.total_followers),
      reach: safeNumber(raw?.total_reach),
      engagement: safeNumber(raw?.total_engagement),
      ads_spend_ars: safeNumber(raw?.total_ads_spend_ars),
    },
    ads_vs_sales: {
      ads_spend_ars: safeNumber(ads?.ads_spend_ars),
      revenue_ars: safeNumber(ads?.revenue_ars),
      ratio: typeof ratio === "number" && Number.isFinite(ratio) ? ratio : null,
    },
  };
}

function dashboardHasData(d: NormalizedDashboard): boolean {
  return (
    d.platforms.length > 0 ||
    d.totals.followers > 0 ||
    d.totals.reach > 0 ||
    d.totals.engagement > 0 ||
    d.totals.ads_spend_ars > 0
  );
}

// ── Parseo de CSV (sin dependencias) ────────────────────────────────────────

interface CsvParseResult {
  rows: CreateSocialMetricPayload[];
  errors: string[];
}

function parseCsv(text: string): CsvParseResult {
  const errors: string[] = [];
  const rows: CreateSocialMetricPayload[] = [];

  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);

  if (lines.length === 0) {
    return { rows, errors: ["El CSV está vacío."] };
  }

  // Detectar si la primera línea es encabezado.
  const firstCells = lines[0]!.split(",").map((c) => c.trim().toLowerCase());
  const looksLikeHeader = firstCells.includes("platform") || firstCells.includes("plataforma");

  // Mapeo de índices de columnas según el encabezado, o el orden por defecto.
  let colIndex: Record<(typeof CSV_COLUMNS)[number], number>;
  let dataStart = 0;

  if (looksLikeHeader) {
    const idxOf = (name: string, alias?: string): number => {
      const i = firstCells.indexOf(name);
      if (i >= 0) return i;
      return alias ? firstCells.indexOf(alias) : -1;
    };
    colIndex = {
      platform: idxOf("platform", "plataforma"),
      metric_date: idxOf("metric_date", "fecha"),
      followers: idxOf("followers", "seguidores"),
      reach: idxOf("reach", "alcance"),
      engagement: idxOf("engagement"),
      ads_spend_ars: idxOf("ads_spend_ars", "gasto"),
    };
    if (colIndex.platform < 0 || colIndex.metric_date < 0) {
      errors.push(
        "El encabezado debe incluir al menos las columnas 'platform' y 'metric_date'.",
      );
      return { rows, errors };
    }
    dataStart = 1;
  } else {
    // Orden por defecto: platform,metric_date,followers,reach,engagement,ads_spend_ars
    colIndex = {
      platform: 0,
      metric_date: 1,
      followers: 2,
      reach: 3,
      engagement: 4,
      ads_spend_ars: 5,
    };
  }

  const parseNum = (raw: string | undefined): number | undefined => {
    if (raw == null || raw.trim() === "") return undefined;
    const n = Number(raw.replace(/[^\d.,-]/g, "").replace(/\./g, "").replace(",", "."));
    return Number.isFinite(n) ? n : undefined;
  };

  for (let i = dataStart; i < lines.length; i++) {
    const cells = lines[i]!.split(",").map((c) => c.trim());
    const lineNo = i + 1;

    const platformRaw = (cells[colIndex.platform] ?? "").toLowerCase();
    const metricDate = cells[colIndex.metric_date] ?? "";

    if (!platformRaw) {
      errors.push(`Fila ${lineNo}: falta la plataforma.`);
      continue;
    }
    const platform: SocialPlatform = (
      ["instagram", "facebook", "tiktok", "other"] as const
    ).includes(platformRaw as SocialPlatform)
      ? (platformRaw as SocialPlatform)
      : "other";

    if (!/^\d{4}-\d{2}-\d{2}/.test(metricDate)) {
      errors.push(`Fila ${lineNo}: fecha inválida ("${metricDate}"). Usá formato AAAA-MM-DD.`);
      continue;
    }

    rows.push({
      platform,
      metric_date: metricDate.slice(0, 10),
      followers: colIndex.followers >= 0 ? parseNum(cells[colIndex.followers]) : undefined,
      reach: colIndex.reach >= 0 ? parseNum(cells[colIndex.reach]) : undefined,
      engagement: colIndex.engagement >= 0 ? parseNum(cells[colIndex.engagement]) : undefined,
      ads_spend_ars:
        colIndex.ads_spend_ars >= 0 ? parseNum(cells[colIndex.ads_spend_ars]) : undefined,
    });
  }

  return { rows, errors };
}

// ── Página ──────────────────────────────────────────────────────────────────

export default function MarketingPage() {
  const queryClient = useQueryClient();
  const toast = useToastStore((s) => s.add);

  const [days, setDays] = useState(30);
  const [platformFilter, setPlatformFilter] = useState("");
  const [manualOpen, setManualOpen] = useState(false);
  const [csvOpen, setCsvOpen] = useState(false);
  const [editing, setEditing] = useState<SocialMetricResponse | null>(null);

  const {
    data: dashboardRaw,
    isLoading: dashboardLoading,
    isError: dashboardError,
  } = useQuery({
    queryKey: ["marketing-dashboard", days],
    queryFn: () => marketingService.getDashboard(days),
    staleTime: 60 * 1000,
  });

  const dashboard = useMemo(() => normalizeDashboard(dashboardRaw), [dashboardRaw]);
  const hasData = dashboardHasData(dashboard);

  const {
    data: metrics = [],
    isLoading: metricsLoading,
    isError: metricsError,
  } = useQuery({
    queryKey: ["marketing-metrics", platformFilter],
    queryFn: () =>
      marketingService.getMetrics(platformFilter ? { platform: platformFilter } : undefined),
    staleTime: 60 * 1000,
  });

  const { data: fieldDefs = [] } = useQuery({
    queryKey: ["field-definitions", "marketing"],
    queryFn: () => fieldDefinitionsService.getAll("marketing"),
    staleTime: 5 * 60 * 1000,
  });

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: ["marketing-metrics"] });
    await queryClient.invalidateQueries({ queryKey: ["marketing-dashboard"] });
  };

  const saveMarketingCustomField = useSaveCustomField({
    listKey: ["marketing-metrics"],
    update: (id, custom_fields) => marketingService.updateMetric(id, { custom_fields }),
    extraInvalidate: [["marketing-dashboard"]],
  });

  const createMutation = useMutation({
    mutationFn: (payload: CreateSocialMetricPayload) => marketingService.createMetric(payload),
    onSuccess: async () => {
      setManualOpen(false);
      toast("Métrica cargada.", "success");
      await invalidate();
    },
    onError: () => toast("No se pudo cargar la métrica. Revisá los campos.", "error"),
  });

  const updateMutation = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: CreateSocialMetricPayload }) =>
      marketingService.updateMetric(id, payload),
    onSuccess: async () => {
      setEditing(null);
      toast("Métrica actualizada.", "success");
      await invalidate();
    },
    onError: () => toast("No se pudo actualizar la métrica.", "error"),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => marketingService.deleteMetric(id),
    onSuccess: async () => {
      toast("Métrica eliminada.", "success");
      await invalidate();
    },
    onError: () => toast("No se pudo eliminar la métrica.", "error"),
  });

  const bulkMutation = useMutation({
    mutationFn: (items: CreateSocialMetricPayload[]) => marketingService.bulkCreate(items),
    onSuccess: async (created) => {
      setCsvOpen(false);
      toast(`${created.length} métrica(s) importadas.`, "success");
      await invalidate();
    },
    onError: () => toast("No se pudieron importar las métricas del CSV.", "error"),
  });

  const columns: SmartColumn<SocialMetricResponse>[] = [
    {
      key: "platform",
      header: "Plataforma",
      render: (_v, row) => platformLabel(row.platform),
      csvValue: (_v, row) => platformLabel(row.platform),
    },
    {
      key: "metric_date",
      header: "Fecha",
      render: (_v, row) => new Date(row.metric_date).toLocaleDateString("es-AR"),
      csvValue: (_v, row) => row.metric_date,
    },
    {
      key: "followers",
      header: "Seguidores",
      render: (_v, row) => fmtInt(row.followers),
      csvValue: (_v, row) => String(row.followers),
    },
    {
      key: "reach",
      header: "Alcance",
      render: (_v, row) => fmtInt(row.reach),
      csvValue: (_v, row) => String(row.reach),
    },
    {
      key: "engagement",
      header: "Engagement",
      render: (_v, row) => fmtInt(row.engagement),
      csvValue: (_v, row) => String(row.engagement),
    },
    {
      key: "ads_spend_ars",
      header: "Gasto en ads",
      render: (_v, row) => fmtArs(row.ads_spend_ars),
      csvValue: (_v, row) => String(row.ads_spend_ars),
    },
    ...buildEditableCustomFieldColumns<SocialMetricResponse>(
      fieldDefs,
      saveMarketingCustomField,
    ),
  ];

  return (
    <PageWrapper
      title="Marketing"
      actions={
        <>
          <Button variant="secondary" size="sm" onClick={() => setCsvOpen(true)}>
            <Upload className="h-4 w-4" /> Importar CSV
          </Button>
          <Button variant="primary" size="sm" onClick={() => setManualOpen(true)}>
            <Plus className="h-4 w-4" /> Cargar métrica
          </Button>
        </>
      }
    >
      {/* Aviso de conexión automática */}
      <div className="flex items-start gap-2.5 rounded-xl border border-vk-border-w bg-vk-surface-w px-4 py-3 text-sm text-vk-text-secondary">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-vk-blue" />
        <p>
          La conexión automática con Instagram, Facebook y TikTok llegará pronto (requiere
          verificación de las apps). Por ahora cargá las métricas manualmente o por CSV.
        </p>
      </div>

      {/* Redes del negocio + guías de carga */}
      <SocialGuides />

      {/* Selector de período */}
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-display text-base font-semibold text-vk-text-primary">
          Resumen del período
        </h2>
        <div className="w-48">
          <Select
            options={DAYS_OPTIONS}
            value={String(days)}
            onChange={(v) => setDays(Number(v))}
          />
        </div>
      </div>

      {/* Dashboard */}
      {dashboardError ? (
        <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
          No se pudo cargar el resumen. Recargá la página.
        </p>
      ) : dashboardLoading ? (
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
          {[0, 1, 2, 3].map((i) => (
            <div
              key={i}
              className="h-28 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w"
            />
          ))}
        </div>
      ) : !hasData ? (
        <EmptyState
          icon={<Megaphone className="h-6 w-6 text-vk-text-muted" />}
          title="Todavía no hay métricas"
          description="Cargá tu primera métrica de redes (manual o por CSV) para ver el resumen acá."
          action={{ label: "Cargar métrica", onClick: () => setManualOpen(true) }}
        />
      ) : (
        <>
          <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <StatCard label="Seguidores" value={fmtInt(dashboard.totals.followers)} />
            <StatCard label="Alcance" value={fmtInt(dashboard.totals.reach)} />
            <StatCard label="Engagement" value={fmtInt(dashboard.totals.engagement)} />
            <StatCard label="Gasto en ads" value={fmtArs(dashboard.totals.ads_spend_ars)} />
          </div>

          {/* Ads vs Ventas */}
          <AdsVsSalesPanel data={dashboard.ads_vs_sales} />

          {/* Breakdown por plataforma */}
          {dashboard.platforms.length > 0 && (
            <div>
              <h3 className="mb-3 font-display text-sm font-semibold text-vk-text-primary">
                Por plataforma
              </h3>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {dashboard.platforms.map((p) => (
                  <div
                    key={p.platform}
                    className="rounded-lg border border-vk-border-w bg-vk-surface-w p-5"
                  >
                    <p className="mb-3 font-display text-sm font-semibold text-vk-text-primary">
                      {platformLabel(p.platform)}
                    </p>
                    <dl className="space-y-1.5 text-sm">
                      <PlatformRow label="Seguidores" value={fmtInt(p.latest_followers)} />
                      <PlatformRow label="Alcance" value={fmtInt(p.total_reach)} />
                      <PlatformRow label="Engagement" value={fmtInt(p.total_engagement)} />
                      <PlatformRow label="Gasto en ads" value={fmtArs(p.total_ads_spend_ars)} />
                    </dl>
                  </div>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {/* Tabla de métricas */}
      <div className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h2 className="font-display text-base font-semibold text-vk-text-primary">
            Métricas cargadas
          </h2>
          <div className="w-56">
            <Select
              options={PLATFORM_FILTER_OPTIONS}
              value={platformFilter}
              onChange={setPlatformFilter}
            />
          </div>
        </div>

        {metricsError ? (
          <p className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
            No se pudieron cargar las métricas. Recargá la página.
          </p>
        ) : metricsLoading ? (
          <div className="h-40 animate-pulse rounded-lg border border-vk-border-w bg-vk-surface-w" />
        ) : (
          <SmartTable
            columns={columns}
            data={metrics}
            exportFilename="vektor-marketing"
            toolbarActions={<AddColumnButton entityType="marketing" entityLabel="Marketing" />}
            emptyMessage="No hay métricas para el filtro seleccionado."
            renderActions={(row) => (
              <div className="flex items-center gap-1.5">
                <button
                  type="button"
                  onClick={() => setEditing(row)}
                  className="inline-flex items-center gap-1.5 rounded border border-vk-border-w px-2.5 py-1.5 text-sm text-vk-text-primary transition-colors hover:bg-vk-bg-light"
                >
                  <Pencil className="h-3.5 w-3.5" /> Editar
                </button>
                <button
                  type="button"
                  title="Eliminar"
                  aria-label="Eliminar métrica"
                  disabled={deleteMutation.isPending}
                  onClick={() => {
                    if (confirm("¿Eliminar esta métrica?")) deleteMutation.mutate(row.id);
                  }}
                  className="inline-flex h-8 w-8 items-center justify-center rounded border border-vk-danger/30 text-vk-danger transition-colors hover:bg-vk-danger-bg disabled:opacity-50"
                >
                  <Trash2 className="h-4 w-4" />
                </button>
              </div>
            )}
          />
        )}
      </div>

      {/* Modal: carga manual */}
      <MetricFormModal
        isOpen={manualOpen}
        title="Cargar métrica"
        saving={createMutation.isPending}
        onClose={() => setManualOpen(false)}
        onSave={(payload) => createMutation.mutate(payload)}
      />

      {/* Modal: edición */}
      <MetricFormModal
        isOpen={editing !== null}
        title="Editar métrica"
        initial={editing}
        saving={updateMutation.isPending}
        onClose={() => setEditing(null)}
        onSave={(payload) =>
          editing && updateMutation.mutate({ id: editing.id, payload })
        }
      />

      {/* Modal: import CSV */}
      <CsvImportModal
        isOpen={csvOpen}
        saving={bulkMutation.isPending}
        onClose={() => setCsvOpen(false)}
        onConfirm={(items) => bulkMutation.mutate(items)}
      />
    </PageWrapper>
  );
}

// ── Subcomponentes ───────────────────────────────────────────────────────────

function PlatformRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between">
      <dt className="text-vk-text-muted">{label}</dt>
      <dd className="font-medium text-vk-text-primary">{value}</dd>
    </div>
  );
}

function AdsVsSalesPanel({
  data,
}: {
  data: NormalizedDashboard["ads_vs_sales"];
}) {
  const ratioPct = data.ratio !== null ? `${(data.ratio * 100).toFixed(1)}%` : "—";
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6">
      <div className="mb-4 flex items-center gap-2">
        <TrendingUp className="h-4 w-4 text-vk-blue" />
        <h3 className="font-display text-sm font-semibold text-vk-text-primary">
          Ads vs Ventas
        </h3>
      </div>
      <div className="grid gap-4 sm:grid-cols-3">
        <div>
          <p className="text-xs uppercase tracking-widest text-vk-text-muted">Gasto en ads</p>
          <p className="mt-1 text-xl font-bold text-vk-text-primary">
            {fmtArs(data.ads_spend_ars)}
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-widest text-vk-text-muted">
            Ventas del período
          </p>
          <p className="mt-1 text-xl font-bold text-vk-text-primary">
            {fmtArs(data.revenue_ars)}
          </p>
        </div>
        <div>
          <p className="text-xs uppercase tracking-widest text-vk-text-muted">
            Ads / Ventas
          </p>
          <p className="mt-1 text-xl font-bold text-vk-text-primary">{ratioPct}</p>
        </div>
      </div>
      <p className="mt-3 text-xs text-vk-text-muted">
        Proporción del gasto en publicidad respecto a las ventas del mismo período.
      </p>
    </div>
  );
}

function MetricFormModal({
  isOpen,
  title,
  initial,
  saving,
  onClose,
  onSave,
}: {
  isOpen: boolean;
  title: string;
  initial?: SocialMetricResponse | null;
  saving: boolean;
  onClose: () => void;
  onSave: (payload: CreateSocialMetricPayload) => void;
}) {
  const [platform, setPlatform] = useState<SocialPlatform>("instagram");
  const [metricDate, setMetricDate] = useState("");
  const [followers, setFollowers] = useState("");
  const [reach, setReach] = useState("");
  const [engagement, setEngagement] = useState("");
  const [adsSpend, setAdsSpend] = useState("");

  useEffect(() => {
    if (!isOpen) return;
    if (initial) {
      setPlatform(initial.platform);
      setMetricDate(initial.metric_date.slice(0, 10));
      setFollowers(String(initial.followers));
      setReach(String(initial.reach));
      setEngagement(String(initial.engagement));
      setAdsSpend(String(initial.ads_spend_ars));
    } else {
      setPlatform("instagram");
      setMetricDate(new Date().toISOString().slice(0, 10));
      setFollowers("");
      setReach("");
      setEngagement("");
      setAdsSpend("");
    }
  }, [isOpen, initial]);

  const toNum = (v: string): number | undefined => {
    if (v.trim() === "") return undefined;
    const n = Number(v);
    return Number.isFinite(n) ? n : undefined;
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!metricDate) return;
    onSave({
      platform,
      metric_date: metricDate,
      followers: toNum(followers),
      reach: toNum(reach),
      engagement: toNum(engagement),
      ads_spend_ars: toNum(adsSpend),
    });
  };

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={title} size="lg">
      <form className="grid gap-4" onSubmit={submit}>
        <Select
          label="Plataforma"
          options={PLATFORM_OPTIONS}
          value={platform}
          onChange={(v) => setPlatform(v as SocialPlatform)}
        />
        <Input
          label="Fecha"
          type="date"
          required
          value={metricDate}
          onChange={(e) => setMetricDate(e.target.value)}
        />
        <div className="grid grid-cols-2 gap-3">
          <Input
            label="Seguidores"
            type="number"
            min={0}
            value={followers}
            onChange={(e) => setFollowers(e.target.value)}
          />
          <Input
            label="Alcance"
            type="number"
            min={0}
            value={reach}
            onChange={(e) => setReach(e.target.value)}
          />
          <Input
            label="Engagement"
            type="number"
            min={0}
            value={engagement}
            onChange={(e) => setEngagement(e.target.value)}
          />
          <Input
            label="Gasto en ads (ARS)"
            type="number"
            min={0}
            step="0.01"
            value={adsSpend}
            onChange={(e) => setAdsSpend(e.target.value)}
          />
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancelar
          </Button>
          <Button type="submit" variant="primary" loading={saving}>
            Guardar
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function CsvImportModal({
  isOpen,
  saving,
  onClose,
  onConfirm,
}: {
  isOpen: boolean;
  saving: boolean;
  onClose: () => void;
  onConfirm: (items: CreateSocialMetricPayload[]) => void;
}) {
  const [raw, setRaw] = useState("");
  const [parsed, setParsed] = useState<CsvParseResult | null>(null);
  const toastAdd = useToastStore((s) => s.add);

  useEffect(() => {
    if (!isOpen) {
      setRaw("");
      setParsed(null);
    }
  }, [isOpen]);

  const handleFile = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    if (file.size > MAX_UPLOAD_BYTES) {
      toastAdd(`El archivo debe ser menor a ${MAX_UPLOAD_MB} MB.`, "error");
      return;
    }
    const reader = new FileReader();
    reader.onload = () => {
      const text = typeof reader.result === "string" ? reader.result : "";
      setRaw(text);
      setParsed(parseCsv(text));
    };
    reader.readAsText(file);
  };

  const handlePreview = () => {
    setParsed(parseCsv(raw));
  };

  const canImport = parsed !== null && parsed.rows.length > 0;
  const previewRows = parsed?.rows.slice(0, 10) ?? [];

  return (
    <Modal isOpen={isOpen} onClose={onClose} title="Importar métricas desde CSV" size="2xl">
      <div className="grid gap-4">
        <div className="rounded-lg border border-vk-border-w bg-vk-bg-light p-3 text-xs text-vk-text-muted">
          Columnas esperadas (en este orden, o con encabezado):{" "}
          <code className="text-vk-text-secondary">{CSV_COLUMNS.join(", ")}</code>. La fecha en
          formato AAAA-MM-DD. Plataformas válidas: instagram, facebook, tiktok, other.
        </div>

        <div className="flex flex-wrap items-center gap-3">
          <input
            type="file"
            accept=".csv,text/csv"
            onChange={handleFile}
            className="text-sm text-vektor-body file:mr-3 file:rounded-lg file:border file:border-vk-border-w file:bg-vk-surface-w file:px-3 file:py-1.5 file:text-sm file:text-vk-text-primary"
          />
          <span className="text-xs text-vk-text-muted">o pegá el CSV abajo</span>
        </div>
        <UploadSizeHint />

        <textarea
          value={raw}
          onChange={(e) => setRaw(e.target.value)}
          rows={6}
          placeholder={"platform,metric_date,followers,reach,engagement,ads_spend_ars\ninstagram,2026-06-01,1200,5400,320,15000"}
          className="w-full rounded-xl border border-vk-border-w bg-vektor-surface px-3 py-2 font-mono text-xs text-vektor-white placeholder:text-vk-text-placeholder focus:outline-none focus:ring-2 focus:ring-vk-blue/15"
        />

        <div className="flex justify-end">
          <Button type="button" variant="secondary" size="sm" onClick={handlePreview}>
            Previsualizar
          </Button>
        </div>

        {parsed !== null && (
          <div className="space-y-3">
            {parsed.errors.length > 0 && (
              <div className="rounded-lg border border-vk-danger/20 bg-vk-danger-bg px-4 py-3 text-sm text-vk-danger">
                <p className="mb-1 font-medium">
                  {parsed.errors.length} problema(s) en el CSV:
                </p>
                <ul className="list-inside list-disc space-y-0.5 text-xs">
                  {parsed.errors.slice(0, 8).map((err, i) => (
                    <li key={i}>{err}</li>
                  ))}
                  {parsed.errors.length > 8 && <li>…y {parsed.errors.length - 8} más.</li>}
                </ul>
              </div>
            )}

            {parsed.rows.length > 0 ? (
              <div className="overflow-x-auto rounded-lg border border-vk-border-w">
                <table className="w-full text-left text-sm">
                  <thead className="bg-vk-bg-light text-xs uppercase tracking-wide text-vk-text-muted">
                    <tr>
                      <th className="px-3 py-2">Plataforma</th>
                      <th className="px-3 py-2">Fecha</th>
                      <th className="px-3 py-2">Seguidores</th>
                      <th className="px-3 py-2">Alcance</th>
                      <th className="px-3 py-2">Engagement</th>
                      <th className="px-3 py-2">Gasto ads</th>
                    </tr>
                  </thead>
                  <tbody>
                    {previewRows.map((r, i) => (
                      <tr key={i} className="border-t border-vk-border-w">
                        <td className="px-3 py-2 text-vk-text-primary">
                          {platformLabel(r.platform)}
                        </td>
                        <td className="px-3 py-2 text-vk-text-secondary">{r.metric_date}</td>
                        <td className="px-3 py-2 text-vk-text-secondary">{r.followers ?? "—"}</td>
                        <td className="px-3 py-2 text-vk-text-secondary">{r.reach ?? "—"}</td>
                        <td className="px-3 py-2 text-vk-text-secondary">
                          {r.engagement ?? "—"}
                        </td>
                        <td className="px-3 py-2 text-vk-text-secondary">
                          {r.ads_spend_ars ?? "—"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {parsed.rows.length > previewRows.length && (
                  <p className="border-t border-vk-border-w px-3 py-2 text-xs text-vk-text-muted">
                    Mostrando {previewRows.length} de {parsed.rows.length} filas válidas.
                  </p>
                )}
              </div>
            ) : (
              <p className="text-sm text-vk-text-muted">No hay filas válidas para importar.</p>
            )}
          </div>
        )}

        <div className="flex justify-end gap-2 pt-2">
          <Button type="button" variant="secondary" onClick={onClose}>
            Cancelar
          </Button>
          <Button
            type="button"
            variant="primary"
            loading={saving}
            disabled={!canImport}
            onClick={() => parsed && onConfirm(parsed.rows)}
          >
            {canImport ? `Importar ${parsed!.rows.length} fila(s)` : "Importar"}
          </Button>
        </div>
      </div>
    </Modal>
  );
}
