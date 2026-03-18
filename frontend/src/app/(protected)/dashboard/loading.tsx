import { DashboardSkeleton } from "@/features/dashboard/DashboardSkeleton";

export default function DashboardLoading() {
  return (
    <div className="space-y-4">
      <div className="h-6 w-24 animate-shimmer rounded" />
      <DashboardSkeleton />
    </div>
  );
}
