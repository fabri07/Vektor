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

  async getConnectUrl(appIds?: string[], loginHint?: string): Promise<WorkspaceConnectStartResponse> {
    const body: Record<string, unknown> = {};
    if (appIds?.length) body.app_ids = appIds;
    if (loginHint) body.login_hint = loginHint;
    const res = await api.post<WorkspaceConnectStartResponse>(
      "/workspace/google/connect/start",
      Object.keys(body).length ? body : undefined,
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
