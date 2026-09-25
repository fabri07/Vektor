import { api } from "@/lib/api";
import type { PosOperationPayload, PosOperationResult, PosProduct } from "@/lib/pos/cart";

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

export interface ScanLookup {
  code_type: string;
  matched_by: string;
  product: PosProduct;
}

/** Un candidato de `SCAN_AMBIGUOUS`: trae la vista de caja del producto. */
export interface ScanCandidate {
  product_id: string;
  name: string;
  matched_by: string;
  product: PosProduct;
}

export interface PosCustomer {
  id: string;
  name: string;
}

export const posService = {
  async lookup(code: string): Promise<ScanLookup> {
    const res = await api.get<ScanLookup>("/products/lookup", { params: { code } });
    return res.data;
  },
  async searchCatalog(q: string, limit = 20): Promise<PosProduct[]> {
    const res = await api.get<{ items: PosProduct[] }>("/pos/catalog", { params: { q, limit } });
    return res.data.items;
  },
  async searchCustomers(q: string): Promise<PosCustomer[]> {
    const res = await api.get<PosCustomer[]>("/pos/customers", { params: { q, limit: 20 } });
    return res.data;
  },
  async learnBarcode(productId: string, barcode: string): Promise<PosProduct> {
    const res = await api.post<PosProduct>(`/products/${productId}/barcode`, { barcode });
    return res.data;
  },
  async createOperation(payload: PosOperationPayload): Promise<PosOperationResult> {
    const res = await api.post<PosOperationResult>("/pos/operations", payload);
    return res.data;
  },
  async voidOperation(id: string, reason: string): Promise<PosOperationResult> {
    const res = await api.post<PosOperationResult>(`/pos/operations/${id}/void`, { reason });
    return res.data;
  },

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
