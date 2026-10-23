interface StatCardProps {
  label: string;
  value: string | number;
  trend?: "up" | "down" | "neutral";
  trendValue?: string;
  sublabel?: string;
}

function TrendBadge({
  trend,
  trendValue,
}: {
  trend: NonNullable<StatCardProps["trend"]>;
  trendValue?: string;
}) {
  if (trend === "up") {
    return (
      <span className="flex items-center gap-0.5 text-sm font-medium text-vk-success">
        ↑ {trendValue}
      </span>
    );
  }
  if (trend === "down") {
    return (
      <span className="flex items-center gap-0.5 text-sm font-medium text-vk-danger">
        ↓ {trendValue}
      </span>
    );
  }
  return (
    <span className="flex items-center gap-0.5 text-sm font-medium text-vk-text-muted">
      → {trendValue}
    </span>
  );
}

export function StatCard({ label, value, trend, trendValue, sublabel }: StatCardProps) {
  return (
    <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm transition-[box-shadow,transform] duration-200 hover:shadow-vk-md hover:-translate-y-0.5">
      <p className="mb-1 text-xs font-medium uppercase tracking-widest text-vk-text-muted">
        {label}
      </p>

      <div className="flex items-end gap-3">
        <span className="text-2xl font-bold text-vk-text-primary leading-none">
          {value}
        </span>
        {trend != null && (
          <TrendBadge trend={trend} trendValue={trendValue} />
        )}
      </div>

      {sublabel && (
        <p className="mt-1.5 text-xs text-vk-text-muted">{sublabel}</p>
      )}
    </div>
  );
}
