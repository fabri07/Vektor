"use client";

import { useEffect, useState } from "react";
import { useRouter, usePathname } from "next/navigation";
import { Sidebar } from "@/components/layout/Sidebar";
import { Header } from "@/components/layout/Header";
import { EconomicTicker } from "@/components/dashboard/EconomicTicker";
import { ActionNotificationToast } from "@/features/notifications/ActionNotificationToast";
import { ChatWidget } from "@/features/chat/ChatWidget";
import { PinGateModal } from "@/components/ui/PinGateModal";
import { useAuthStore } from "@/stores/authStore";
import { useSessionSync } from "@/hooks/useSessionSync";
import { CASHIER_ROLE } from "@/lib/roles";

export default function ProtectedLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const router = useRouter();
  const pathname = usePathname();
  const token = useAuthStore((s) => s.token);
  const role = useAuthStore((s) => s.user?.role);
  const sesion = useSessionSync();
  const [mobileOpen, setMobileOpen] = useState(false);
  const isChatPage = pathname === "/chat";
  const isDashboardRoute = pathname.startsWith("/dashboard");

  const isPosRoute = pathname === "/pos" || pathname.startsWith("/pos/");
  const isCashier = role === CASHIER_ROLE;

  useEffect(() => {
    if (!token) {
      router.replace("/login");
    }
  }, [token, router]);

  // Un cajero sólo tiene la caja (B5/B13): el backend le deniega todo lo demás,
  // así que cualquier otra página sería una pantalla de 403.
  useEffect(() => {
    if (token && sesion !== "pending" && isCashier && !isPosRoute) {
      router.replace("/pos");
    }
  }, [token, sesion, isCashier, isPosRoute, router]);

  if (!token) {
    return null;
  }

  // Hasta validar la sesión no se muestra nada: el rol guardado puede estar
  // viejo, y renderizar la app del dueño con datos cacheados es justo lo que
  // hay que evitar. Sin red se sigue con el rol guardado (ver useSessionSync).
  if (sesion === "pending") {
    return null;
  }

  if (isCashier && !isPosRoute) {
    return null;
  }

  // La caja va a pantalla completa, sin barra lateral ni chat (B13). El modal
  // del PIN sí: anular un ticket lo pide.
  if (isPosRoute) {
    return (
      <>
        {children}
        <PinGateModal />
      </>
    );
  }

  return (
    <div className="flex h-screen overflow-hidden bg-vk-bg-light">
      {/* Mobile overlay */}
      {mobileOpen && (
        <div
          className="fixed inset-0 z-40 bg-black/50 md:hidden"
          onClick={() => setMobileOpen(false)}
        />
      )}

      <Sidebar
        mobileOpen={mobileOpen}
        onClose={() => setMobileOpen(false)}
      />

      <div className="flex flex-1 flex-col overflow-hidden">
        <Header onMenuToggle={() => setMobileOpen((v) => !v)} />
        {isDashboardRoute ? <EconomicTicker /> : null}
        {isChatPage ? (
          <main className="flex flex-1 flex-col overflow-hidden bg-vk-surface-w">
            {children}
          </main>
        ) : (
          <main className="flex-1 overflow-y-auto scrollbar-thin bg-vk-bg-light p-4 sm:p-6">
            <div className="mx-auto max-w-[1200px]">
              {children}
            </div>
          </main>
        )}
      </div>
      <ActionNotificationToast />
      <ChatWidget />
      <PinGateModal />
    </div>
  );
}
