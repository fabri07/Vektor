import { api } from "@/lib/api";
import type { NotificationListResponse } from "@/types/api";

export async function fetchNotifications(): Promise<NotificationListResponse> {
  const { data } = await api.get<NotificationListResponse>("/notifications");
  return data;
}

export async function markNotificationRead(id: string): Promise<void> {
  await api.patch(`/notifications/${id}/read`);
}

export async function markAllNotificationsRead(): Promise<void> {
  await api.post("/notifications/read-all");
}
