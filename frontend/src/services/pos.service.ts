import { api } from "@/lib/api";

export interface PosTerminal {
  id: string;
  name: string;
  created_at: string;
  last_seen_at: string | null;
  disabled_at: string | null;
}

export interface PosTerminalEnrolled extends PosTerminal {
  /** Se ve UNA sola vez: el servidor guarda sólo su hash. */
  secret: string;
}

export const posService = {
  async listTerminals(): Promise<PosTerminal[]> {
    const res = await api.get<PosTerminal[]>("/pos/terminals");
    return res.data;
  },
  async enrollTerminal(name: string): Promise<PosTerminalEnrolled> {
    const res = await api.post<PosTerminalEnrolled>("/pos/terminals", { name });
    return res.data;
  },
  async disableTerminal(id: string): Promise<PosTerminal> {
    const res = await api.post<PosTerminal>(`/pos/terminals/${id}/disable`);
    return res.data;
  },
};
