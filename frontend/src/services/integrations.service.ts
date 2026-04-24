import { api } from "@/lib/api";

export type GoogleConnectionState =
  | "DISCONNECTED"
  | "CONNECTING"
  | "CONNECTED"
  | "INSUFFICIENT_SCOPE"
  | "RECONNECT_REQUIRED"
  | "ERROR";

export interface GoogleAppStatus {
  id: string;
  label: string;
  description: string;
  available: boolean;
  connected: boolean;
  needs_reconnect: boolean;
  required_scopes: string[];
}

export interface GoogleIntegrationStatusResponse {
  provider: "google";
  mcp_enabled: boolean;
  mcp_server_configured: boolean;
  connection_flow_available: boolean;
  connection_state: GoogleConnectionState;
  connected: boolean;
  scopes_granted: string[];
  connected_at: string | null;
  last_error_code: string | null;
  apps: GoogleAppStatus[];
  message: string;
}

export interface ConnectStartResponse {
  auth_url: string;
  required_scopes: string[];
  state: string;
}

export const integrationsService = {
  async getGoogleStatus(): Promise<GoogleIntegrationStatusResponse> {
    const res = await api.get<GoogleIntegrationStatusResponse>("/integrations/google/status");
    return res.data;
  },

  async startGoogleConnect(): Promise<ConnectStartResponse> {
    const res = await api.post<ConnectStartResponse>("/integrations/google/connect/start");
    return res.data;
  },

  async disconnectGoogle(): Promise<void> {
    await api.post("/integrations/google/disconnect");
  },
};
