import { api } from "@/lib/api";

export type CommunicationChannel = "email" | "whatsapp";

export interface ChannelAvailability {
  channel: CommunicationChannel;
  available: boolean;
}

export interface SendMessagePayload {
  channel: CommunicationChannel;
  subject?: string;
  body: string;
}

export type RecipientType = "customer" | "supplier";

export interface CommunicationLogResponse {
  id: string;
  recipient_type: RecipientType;
  recipient_id: string;
  channel: CommunicationChannel;
  subject: string | null;
  body: string;
  status: "sent" | "failed";
  error: string | null;
  created_at: string;
}

export const communicationService = {
  async getChannels(): Promise<ChannelAvailability[]> {
    const res = await api.get<ChannelAvailability[]>("/communication/channels");
    return res.data;
  },

  async sendToCustomer(
    id: string,
    payload: SendMessagePayload,
  ): Promise<CommunicationLogResponse> {
    const res = await api.post<CommunicationLogResponse>(
      `/communication/customers/${id}/send`,
      payload,
    );
    return res.data;
  },

  async getCustomerHistory(id: string): Promise<CommunicationLogResponse[]> {
    const res = await api.get<CommunicationLogResponse[]>(
      `/communication/customers/${id}/history`,
    );
    return res.data;
  },

  async sendToSupplier(
    id: string,
    payload: SendMessagePayload,
  ): Promise<CommunicationLogResponse> {
    const res = await api.post<CommunicationLogResponse>(
      `/communication/suppliers/${id}/send`,
      payload,
    );
    return res.data;
  },

  async getSupplierHistory(id: string): Promise<CommunicationLogResponse[]> {
    const res = await api.get<CommunicationLogResponse[]>(
      `/communication/suppliers/${id}/history`,
    );
    return res.data;
  },
};
