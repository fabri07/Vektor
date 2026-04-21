import { api } from "@/lib/api";

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
  connected: boolean;
  connected_at: string | null;
  last_error_code: string | null;
  apps: GoogleAppStatus[];
  message: string;
}

export const integrationsService = {
  async getGoogleStatus(): Promise<GoogleIntegrationStatusResponse> {
    const res = await api.get<GoogleIntegrationStatusResponse>("/integrations/google/status");
    return res.data;
  },
};
