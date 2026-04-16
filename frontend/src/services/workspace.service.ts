import { api } from "@/lib/api";
import type {
  WorkspaceStatusResponse,
  WorkspaceConnectStartResponse,
} from "@/types/api";

export const workspaceService = {
  async getStatus(): Promise<WorkspaceStatusResponse> {
    const res = await api.get<WorkspaceStatusResponse>("/workspace/google/status");
    return res.data;
  },

  async getConnectUrl(appIds?: string[]): Promise<WorkspaceConnectStartResponse> {
    const res = await api.post<WorkspaceConnectStartResponse>(
      "/workspace/google/connect/start",
      appIds ? { app_ids: appIds } : undefined,
    );
    return res.data;
  },

  async exchangeSession(exchange_session_id: string): Promise<{ connected: true }> {
    const res = await api.post<{ connected: true }>(
      "/workspace/google/connect/exchange",
      { exchange_session_id },
    );
    return res.data;
  },

  async disconnect(): Promise<{ disconnected: true }> {
    const res = await api.delete<{ disconnected: true }>("/workspace/google/disconnect");
    return res.data;
  },
};
