function SkeletonBlock({ className }: { className?: string }) {
  return (
    <div className={`animate-pulse rounded bg-gray-100 ${className ?? ""}`} />
  );
}

function CardShell({
  colSpan2,
  children,
}: {
  colSpan2?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div
      className={`rounded-xl border border-gray-200 bg-white p-6 shadow-sm ${
        colSpan2 ? "col-span-2" : ""
      }`}
    >
      {children}
    </div>
  );
}

export function DashboardSkeleton() {
  return (
    <div className="grid grid-cols-4 gap-4">
      {/* Health Score skeleton */}
      <CardShell colSpan2>
        <SkeletonBlock className="mb-2 h-3 w-24" />
        <SkeletonBlock className="mb-4 h-20 w-36" />
        <SkeletonBlock className="mb-2 h-3 w-32" />
        <SkeletonBlock className="h-2 w-full" />
        <p className="mt-5 text-xs text-gray-400">Calculando...</p>
      </CardShell>

      {/* Risk skeleton */}
      <CardShell>
        <SkeletonBlock className="mb-3 h-3 w-24" />
        <div className="flex gap-3">
          <SkeletonBlock className="h-9 w-9 rounded-lg" />
          <div className="flex-1 space-y-2">
            <SkeletonBlock className="h-4 w-3/4" />
            <SkeletonBlock className="h-3 w-full" />
            <SkeletonBlock className="h-3 w-2/3" />
          </div>
        </div>
      </CardShell>

      {/* Action skeleton */}
      <CardShell>
        <SkeletonBlock className="mb-3 h-3 w-28" />
        <div className="space-y-2">
          <SkeletonBlock className="h-4 w-full" />
          <SkeletonBlock className="h-4 w-4/5" />
        </div>
        <SkeletonBlock className="mt-5 h-8 w-32 rounded-lg" />
      </CardShell>

      {/* Subscores skeleton */}
      <CardShell colSpan2>
        <SkeletonBlock className="mb-5 h-3 w-24" />
        {["Caja", "Margen", "Stock", "Proveedores"].map((label) => (
          <div key={label} className="mb-4">
            <div className="mb-1.5 flex justify-between">
              <SkeletonBlock className="h-3 w-20" />
              <SkeletonBlock className="h-3 w-6" />
            </div>
            <SkeletonBlock className="h-2 w-full rounded-full" />
          </div>
        ))}
      </CardShell>
    </div>
  );
}
