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
    <div className={`vektor-card p-6 ${className ?? ""}`}>
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

      <div className="grid grid-cols-1 gap-4 xl:grid-cols-2">
        {["Caja", "Margen", "Stock", "Proveedores"].map((label) => (
          <SkeletonCard key={label}>
            <ShimmerBlock className="h-4 w-24" />
            <ShimmerBlock className="mt-4 h-8 w-32" />
            <div className="mt-4 space-y-3">
              <ShimmerBlock className="h-12 w-full rounded-xl" />
              <ShimmerBlock className="h-12 w-full rounded-xl" />
              <ShimmerBlock className="h-12 w-full rounded-xl" />
            </div>
          </SkeletonCard>
        ))}
      </div>

      <p className="text-center text-sm text-vk-text-muted">Analizando tu negocio...</p>
    </div>
  );
}
