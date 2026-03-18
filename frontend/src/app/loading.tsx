function ShimmerBlock({ className }: { className?: string }) {
  return <div className={`animate-shimmer rounded ${className ?? ""}`} />;
}

export default function GlobalLoading() {
  return (
    <div className="space-y-4 p-6">
      {/* Bloque 1 — hero genérico */}
      <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm">
        <ShimmerBlock className="mb-3 h-3 w-24" />
        <ShimmerBlock className="mb-4 h-12 w-48" />
        <ShimmerBlock className="h-2 w-full rounded-full" />
      </div>

      {/* Bloques 2 y 3 — grid 2 columnas */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm space-y-3">
          <ShimmerBlock className="h-3 w-28" />
          <ShimmerBlock className="h-4 w-3/4" />
          <ShimmerBlock className="h-4 w-full" />
          <ShimmerBlock className="h-4 w-2/3" />
        </div>
        <div className="rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm space-y-3">
          <ShimmerBlock className="h-3 w-24" />
          <ShimmerBlock className="h-4 w-full" />
          <ShimmerBlock className="h-4 w-4/5" />
          <ShimmerBlock className="h-8 w-32 rounded-lg" />
        </div>
      </div>
    </div>
  );
}
