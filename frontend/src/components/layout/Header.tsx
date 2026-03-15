"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { Button } from "@/components/ui/Button";
import { NotificationPanel } from "@/features/notifications/NotificationPanel";
import { fetchNotifications } from "@/services/notifications.service";

export function Header() {
  const { user, logout } = useAuthStore();
  const [panelOpen, setPanelOpen] = useState(false);

  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: fetchNotifications,
    staleTime: 60 * 1000, // 1 minute
    retry: 1,
  });

  const unreadCount = data?.unread_count ?? 0;

  return (
    <header className="relative flex h-14 items-center justify-between border-b border-white/10 bg-primary/80 px-6 backdrop-blur">
      <div />
      <div className="flex items-center gap-4">
        {/* Notification bell */}
        <button
          onClick={() => setPanelOpen((v) => !v)}
          className="relative rounded-lg p-1.5 text-white/50 hover:bg-white/5 hover:text-white transition-colors"
          aria-label="Notificaciones"
        >
          <svg
            xmlns="http://www.w3.org/2000/svg"
            width="18"
            height="18"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9" />
            <path d="M13.73 21a2 2 0 0 1-3.46 0" />
          </svg>
          {unreadCount > 0 && (
            <span className="absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-[#E63946] text-[10px] font-bold text-white leading-none">
              {unreadCount > 9 ? "9+" : unreadCount}
            </span>
          )}
        </button>

        {user && (
          <span className="text-sm text-white/50">{user.email}</span>
        )}
        <Button variant="ghost" size="sm" onClick={logout}>
          Salir
        </Button>
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
