"use client";

import { useAuthStore } from "@/stores/authStore";

export function AuthHydrationBoundary({
  children,
}: {
  children: React.ReactNode;
}) {
  const hasHydrated = useAuthStore((s) => s._hasHydrated);

  if (!hasHydrated) {
    return (
      <div className="flex h-screen items-center justify-center bg-primary">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-300 border-t-gray-600" />
      </div>
    );
  }

  return <>{children}</>;
}
