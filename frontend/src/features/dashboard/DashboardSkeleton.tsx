function ShimmerBlock({ className }: { className?: string }) {
  return (
    <div className={`animate-shimmer rounded ${className ?? ""}`} />
  );
}

function SkeletonCard({
  className,
  children,
}: {
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={`rounded-lg border border-vk-border-w bg-vk-surface-w p-6 shadow-vk-sm ${className ?? ""}`}>
      {children}
    </div>
  );
}

export function DashboardSkeleton() {
  return (
    <div className="space-y-4">
      {/* Hero skeleton — ancho completo */}
      <SkeletonCard>
        <div className="mb-4 flex items-start justify-between">
          <ShimmerBlock className="h-3 w-24" />
          <ShimmerBlock className="h-5 w-28 rounded-full" />
        </div>
        <div className="flex items-end gap-4">
          <ShimmerBlock className="h-16 w-32" />
          <ShimmerBlock className="mb-1 h-5 w-40" />
        </div>
        <div className="mt-5">
          <div className="mb-1.5 flex justify-between">
            <ShimmerBlock className="h-3 w-28" />
            <ShimmerBlock className="h-3 w-8" />
          </div>
          <ShimmerBlock className="h-1 w-full rounded-full" />
        </div>
        <ShimmerBlock className="mt-4 h-3 w-40" />
      </SkeletonCard>

      {/* Grid 2 columnas: Risk + Action */}
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
        {/* Risk skeleton */}
        <SkeletonCard>
          <ShimmerBlock className="mb-3 h-3 w-24" />
          <div className="flex gap-3">
            <ShimmerBlock className="h-9 w-9 flex-shrink-0 rounded-lg" />
            <div className="flex-1 space-y-2">
              <ShimmerBlock className="h-4 w-3/4" />
              <ShimmerBlock className="h-3 w-full" />
              <ShimmerBlock className="h-3 w-2/3" />
            </div>
          </div>
        </SkeletonCard>

        {/* Action skeleton */}
        <SkeletonCard>
          <ShimmerBlock className="mb-3 h-3 w-28" />
          <div className="space-y-2">
            <ShimmerBlock className="h-4 w-full" />
            <ShimmerBlock className="h-4 w-4/5" />
            <ShimmerBlock className="h-4 w-3/5" />
          </div>
          <ShimmerBlock className="mt-5 h-8 w-32 rounded-lg" />
        </SkeletonCard>

        {/* Subscores skeleton — ancho completo */}
        <SkeletonCard className="md:col-span-2">
          <ShimmerBlock className="mb-5 h-3 w-24" />
          <div className="space-y-4">
            {["Caja", "Margen", "Stock", "Proveedores"].map((label) => (
              <div key={label}>
                <div className="mb-1.5 flex justify-between">
                  <ShimmerBlock className="h-3 w-20" />
                  <ShimmerBlock className="h-3 w-6" />
                </div>
                <ShimmerBlock className="h-2 w-full rounded-full" />
              </div>
            ))}
          </div>
        </SkeletonCard>
      </div>

      <p className="text-center text-sm text-vk-text-muted">Analizando tu negocio...</p>
    </div>
  );
}
