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
      className="absolute right-0 top-14 z-50 w-80 rounded-xl border border-vk-border-dark bg-vk-surface-1 shadow-vk-lg"
    >
      {/* Header */}
      <div className="flex items-center justify-between border-b border-vk-border-dark px-4 py-3">
        <span className="text-sm font-semibold text-vk-text-light">Notificaciones</span>
        {unread.length > 0 && (
          <button
            onClick={() => markAll.mutate()}
            disabled={markAll.isPending}
            className="text-xs text-vk-text-muted hover:text-vk-text-light transition-colors"
          >
            Marcar todo leído
          </button>
        )}
      </div>

      {/* List */}
      <div className="max-h-96 overflow-y-auto">
        {notifications.length === 0 ? (
          <p className="py-8 text-center text-sm text-vk-text-muted/60">
            Sin notificaciones nuevas.
          </p>
        ) : (
          notifications.map((n) => (
            <div
              key={n.id}
              className={`flex gap-3 border-b border-vk-border-dark/60 px-4 py-3 transition-colors ${
                n.is_read ? "opacity-50" : "hover:bg-vk-surface-2/50"
              }`}
            >
              <div
                className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${
                  n.is_read ? "bg-transparent" : "bg-vk-danger"
                }`}
              />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium text-vk-text-light leading-snug">{n.title}</p>
                <p className="mt-0.5 text-xs text-vk-text-muted leading-snug line-clamp-2">
                  {n.body}
                </p>
                <p className="mt-1 text-xs text-vk-text-muted/60">{timeAgo(n.created_at)}</p>
              </div>
              {!n.is_read && (
                <button
                  onClick={() => markRead.mutate(n.id)}
                  disabled={markRead.isPending}
                  className="shrink-0 self-start mt-1 text-xs text-vk-text-muted hover:text-vk-text-light transition-colors"
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
