"use client";

import { useState } from "react";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { Menu, Bell } from "lucide-react";
import { useAuthStore } from "@/stores/authStore";
import { NotificationPanel } from "@/features/notifications/NotificationPanel";
import { fetchNotifications } from "@/services/notifications.service";

interface HeaderProps {
  onMenuToggle: () => void;
}

const PAGE_LABELS: Record<string, string> = {
  "/dashboard":  "Dashboard",
  "/sales":      "Ventas",
  "/expenses":   "Gastos",
  "/products":   "Productos",
  "/ingestion":  "Cargar datos",
  "/settings":   "Configuración",
  "/onboarding": "Onboarding",
};

function getPageLabel(pathname: string): string {
  // exact match first, then prefix match
  if (PAGE_LABELS[pathname]) return PAGE_LABELS[pathname];
  for (const [key, label] of Object.entries(PAGE_LABELS)) {
    if (pathname.startsWith(key + "/")) return label;
  }
  return "";
}

function getInitials(name: string): string {
  return name
    .split(" ")
    .map((n) => n[0] ?? "")
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

export function Header({ onMenuToggle }: HeaderProps) {
  const pathname = usePathname();
  const user = useAuthStore((s) => s.user);
  const [panelOpen, setPanelOpen] = useState(false);

  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: fetchNotifications,
    staleTime: 60 * 1000,
    retry: 1,
  });

  const unreadCount = data?.unread_count ?? 0;
  const pageLabel = getPageLabel(pathname);
  const initials = getInitials(user?.full_name ?? user?.email ?? "U");

  return (
    <header className="relative flex h-14 shrink-0 items-center justify-between border-b border-vk-border-dark bg-vk-bg-dark px-4 md:px-6">
      {/* Left: hamburger (mobile) + breadcrumb */}
      <div className="flex items-center gap-3">
        {/* Hamburger — mobile only */}
        <button
          onClick={onMenuToggle}
          className="flex h-8 w-8 items-center justify-center rounded-md text-vk-text-muted hover:bg-vk-surface-1 hover:text-vk-text-light transition-colors md:hidden focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
          aria-label="Abrir menú"
        >
          <Menu className="h-5 w-5" />
        </button>

        {pageLabel && (
          <span className="text-sm font-semibold text-vk-text-light">
            {pageLabel}
          </span>
        )}
      </div>

      {/* Right: notifications + avatar */}
      <div className="flex items-center gap-2">
        {/* Notification bell */}
        <button
          onClick={() => setPanelOpen((v) => !v)}
          className="relative flex h-8 w-8 items-center justify-center rounded-md text-vk-text-muted hover:bg-vk-surface-1 hover:text-vk-text-light transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-vk-blue/60"
          aria-label="Notificaciones"
        >
          <Bell className="h-[18px] w-[18px]" />
          {unreadCount > 0 && (
            <span
              className={[
                "absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-vk-danger text-[10px] font-bold text-white leading-none",
                !panelOpen ? "animate-vk-pulse" : "",
              ].join(" ")}
            >
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </button>

        {/* User avatar */}
        <div className="flex h-8 w-8 items-center justify-center rounded-full bg-vk-blue/20 text-xs font-bold text-vk-blue-light select-none">
          {initials}
        </div>
      </div>

      {/* Notification panel */}
      {panelOpen && (
        <NotificationPanel
          notifications={data?.notifications ?? []}
          onClose={() => setPanelOpen(false)}
        />
      )}
    </header>
  );
}
