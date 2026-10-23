import { Suspense } from "react";
import { VerifyEmailSentPage } from "@/features/auth/VerifyEmailSentPage";

export default function VerifyEmailSentRoute() {
  return (
    <main className="min-h-screen md:grid md:grid-cols-2">
      {/* Left panel — desktop only */}
      <div className="hidden md:flex flex-col bg-vk-bg-dark px-12 py-12">
        <div className="flex-1">
          <span className="text-[28px] font-bold tracking-tight text-white">
            VÉKTOR
          </span>
          <p className="mt-3 text-base text-vk-text-muted">
            No trabajes más. Decidí mejor.
          </p>
        </div>
      </div>

      {/* Right panel */}
      <div className="flex min-h-screen items-center justify-center bg-vk-surface-w px-8 py-12 md:min-h-0 md:overflow-y-auto">
        <div className="w-full max-w-[400px]">
          {/* Mobile logo */}
          <div className="mb-8 md:hidden">
            <span className="text-2xl font-bold tracking-tight text-vk-navy">VÉKTOR</span>
          </div>

          <Suspense>
            <VerifyEmailSentPage />
          </Suspense>
        </div>
      </div>
    </main>
  );
}
