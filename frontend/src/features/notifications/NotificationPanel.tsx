"use client";

import { useRef, useEffect } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import {
  markNotificationRead,
  markAllNotificationsRead,
} from "@/services/notifications.service";
import type { NotificationItem } from "@/types/api";

interface NotificationPanelProps {
  notifications: NotificationItem[];
  onClose: () => void;
}

function timeAgo(isoDate: string): string {
  const diff = Date.now() - new Date(isoDate).getTime();
  const minutes = Math.floor(diff / 60_000);
  if (minutes < 1) return "Ahora";
  if (minutes < 60) return `hace ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `hace ${hours} h`;
  const days = Math.floor(hours / 24);
  return `hace ${days} d`;
}

export function NotificationPanel({ notifications, onClose }: NotificationPanelProps) {
  const panelRef = useRef<HTMLDivElement>(null);
  const queryClient = useQueryClient();

  const markRead = useMutation({
    mutationFn: markNotificationRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const markAll = useMutation({
    mutationFn: markAllNotificationsRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  // Close on outside click
  useEffect(() => {
    function handleClick(e: MouseEvent) {
      if (panelRef.current && !panelRef.current.contains(e.target as Node)) {
        onClose();
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [onClose]);

  const unread = notifications.filter((n) => !n.is_read);

  return (
    <div
      ref={panelRef}
      className="absolute right-0 top-14 z-50 w-80 rounded-xl border border-white/10 bg-[#1A1A2E] shadow-2xl"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-white/10 px-4 py-3">
        <span className="text-sm font-semibold text-white">Notificaciones</span>
        {unread.length > 0 && (
          <button
            onClick={() => markAll.mutate()}
            disabled={markAll.isPending}
            className="text-xs text-white/40 hover:text-white/70 transition-colors"
          >
            Marcar todo leído
          </button>
        )}
      </div>

      {/* List */}
      <div className="max-h-96 overflow-y-auto">
        {notifications.length === 0 ? (
          <p className="py-8 text-center text-sm text-white/30">
            Sin notificaciones nuevas.
          </p>
        ) : (
          notifications.map((n) => (
            <div
              key={n.id}
              className={`flex gap-3 border-b border-white/5 px-4 py-3 transition-colors ${
                n.is_read ? "opacity-50" : "hover:bg-white/5"
              }`}
            >
              <div
                className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                  n.is_read ? "bg-transparent" : "bg-[#E63946]"
                }`}
              />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-white leading-snug">{n.title}</p>
                <p className="mt-0.5 text-xs text-white/40 leading-snug line-clamp-2">
                  {n.body}
                </p>
                <p className="mt-1 text-xs text-white/25">{timeAgo(n.created_at)}</p>
              </div>
              {!n.is_read && (
                <button
                  onClick={() => markRead.mutate(n.id)}
                  disabled={markRead.isPending}
                  className="shrink-0 self-start mt-1 text-xs text-white/30 hover:text-white/60 transition-colors"
                  aria-label="Marcar como leída"
                >
                  ✓
                </button>
              )}
            </div>
          ))
        )}
      </div>
    </div>
  );
}
