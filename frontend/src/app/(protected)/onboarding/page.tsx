"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { OnboardingWizard } from "@/features/onboarding/OnboardingWizard";
import { onboardingService } from "@/services/onboarding.service";

export default function OnboardingPage() {
  const router = useRouter();
  const [checking, setChecking] = useState(true);

  useEffect(() => {
    onboardingService
      .getStatus()
      .then((status) => {
        if (status.completed) {
          router.replace("/dashboard");
        } else {
          setChecking(false);
        }
      })
      .catch(() => {
        // If status check fails, show the wizard anyway
        setChecking(false);
      });
  }, [router]);

  if (checking) {
    return (
      <div className="fixed inset-0 z-50 flex items-center justify-center bg-[#F4F4F8]">
        <div className="h-8 w-8 animate-spin rounded-full border-4 border-gray-200 border-t-[#1A1A2E]" />
      </div>
    );
  }

  return <OnboardingWizard />;
}
