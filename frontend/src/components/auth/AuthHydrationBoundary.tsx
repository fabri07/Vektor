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
      <div className="flex h-screen items-center justify-center bg-vk-bg-light">
        <div className="h-8 w-8 animate-spin rounded-full border-2 border-gray-200 border-t-vk-navy" />
      </div>
    );
  }

  return <>{children}</>;
}
