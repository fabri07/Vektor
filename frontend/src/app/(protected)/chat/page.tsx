"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useChatWidgetStore } from "@/stores/chatWidgetStore";

/**
 * El chat dejó de ser una página y pasó a ser el widget flotante (disponible en
 * todas las pantallas). Cualquier navegación a /chat abre el widget y lleva al
 * dashboard — así los links existentes a "/chat" siguen funcionando.
 */
export default function Page() {
  const router = useRouter();
  useEffect(() => {
    useChatWidgetStore.getState().open();
    router.replace("/dashboard");
  }, [router]);
  return null;
}
