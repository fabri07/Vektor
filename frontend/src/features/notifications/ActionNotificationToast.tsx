"use client";

import { useMemo } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Bell } from "lucide-react";
import {
  fetchNotifications,
  markNotificationRead,
} from "@/services/notifications.service";

export function ActionNotificationToast() {
  const router = useRouter();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ["notifications"],
    queryFn: fetchNotifications,
    staleTime: 60 * 1000,
    retry: 1,
  });

  const markRead = useMutation({
    mutationFn: markNotificationRead,
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["notifications"] }),
  });

  const notification = useMemo(
    () =>
      data?.notifications.find(
        (item) =>
          !item.is_read &&
          item.notification_type === "health_action" &&
          Boolean(item.action_url),
      ),
    [data?.notifications],
  );

  if (!notification) return null;

  return (
    <button
      type="button"
      onClick={() => {
        markRead.mutate(notification.id);
        router.push(notification.action_url ?? "/dashboard?focus=health");
      }}
      className="fixed bottom-4 right-4 z-50 flex w-[min(360px,calc(100vw-32px))] gap-3 rounded-xl border border-vk-border-dark bg-vk-surface-1 p-4 text-left shadow-vk-lg transition-transform hover:-translate-y-0.5"
    >
      <span className="mt-0.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg bg-vk-blue/15 text-vk-blue-light">
        <Bell className="h-4 w-4" />
      </span>
      <span className="min-w-0">
        <span className="block text-sm font-semibold text-vk-text-light">
          {notification.title}
        </span>
        <span className="mt-1 line-clamp-2 block text-xs leading-5 text-vk-text-muted">
          {notification.body}
        </span>
      </span>
    </button>
  );
}
