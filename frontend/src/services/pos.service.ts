import { api } from "@/lib/api";
import type { PosReceipt } from "@/features/pos/ticketTemplate";
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

export interface PosCatalogPage {
  items: PosProduct[];
  next_cursor: string | null;
  business_name: string;
}

export interface PosStockDiscrepancy {
  id: string;
  operation_id: string;
  product_id: string | null;
  product_name: string | null;
  /** La cantidad COMPLETA de la línea, no el faltante. */
  pending_units: number;
  stock_at_capture: number;
  operation_date: string;
  status: string;
  resolution: string | null;
  reason: string | null;
  resolved_at: string | null;
  created_at: string;
}

export type ResolucionDeDiferencia = "ajuste_de_entrada" | "anular_ticket";

export interface PosCustomer {
  id: string;
  name: string;
}

/** Una fila de "Últimos tickets". */
export interface PosOperationSummary {
  id: string;
  number: string;
  operation_date: string;
  total_ars: string;
  status: string;
  cashier_name: string | null;
}

/** Una venta que llegó después de que su turno cerrara (no movió el arqueo). */
export interface PosLateSale {
  operation_id: string;
  cash_ars: string;
  created_at: string;
}

export type PosCashSessionStatus = "OPEN" | "CLOSED" | "ABANDONED";

/** El turno de un cajero en una caja (B8). */
export interface PosCashSession {
  id: string;
  client_session_id: string;
  terminal_id: string | null;
  user_id: string | null;
  cashier_name: string | null;
  opening_float_ars: string;
  float_declared: boolean;
  opened_at: string;
  closed_at: string | null;
  closed_by_user_id: string | null;
  /** `null` mientras está abierto, y siempre en un turno ABANDONED. */
  expected_cash_ars: string | null;
  counted_cash_ars: string | null;
  difference_ars: string | null;
  notes: string | null;
  status: PosCashSessionStatus;
  /** Parcial de un turno abierto. Al cajero NO le llega (arqueo ciego). */
  expected_cash_now_ars: string | null;
  late_sales: PosLateSale[];
  /** Tickets vivos. Sólo en el listado del encargado. */
  sales_count: number | null;
}

export const posService = {
  async getReceipt(operationId: string): Promise<PosReceipt> {
    const res = await api.get<PosReceipt>(`/pos/operations/${operationId}/receipt`);
    return res.data;
  },
  async listOperations(limit = 20): Promise<PosOperationSummary[]> {
    const res = await api.get<PosOperationSummary[]>("/pos/operations", { params: { limit } });
    return res.data;
  },
  async lookup(code: string): Promise<ScanLookup> {
    const res = await api.get<ScanLookup>("/products/lookup", { params: { code } });
    return res.data;
  },
  async searchCatalog(q: string, limit = 20): Promise<PosProduct[]> {
    const res = await api.get<PosCatalogPage>("/pos/catalog", { params: { q, limit } });
    return res.data.items;
  },
  /** Una página del catálogo completo, para bajarlo entero a la PC (B7). */
  async catalogPage(after: string | null, limit = 500): Promise<PosCatalogPage> {
    const params: Record<string, string | number> = { limit };
    if (after) params.after = after;
    const res = await api.get<PosCatalogPage>("/pos/catalog", { params });
    return res.data;
  },
  async searchCustomers(q: string): Promise<PosCustomer[]> {
    const res = await api.get<PosCustomer[]>("/pos/customers", { params: { q, limit: 20 } });
    return res.data;
  },
  /**
   * `offline: true` lo manda sólo el motor de sincronización, para un código
   * que se aprendió sin red: queda en la auditoría (`learned_offline`).
   */
  async learnBarcode(
    productId: string,
    barcode: string,
    { offline = false }: { offline?: boolean } = {},
  ): Promise<PosProduct> {
    const res = await api.post<PosProduct>(
      `/products/${productId}/barcode`,
      { barcode },
      { headers: offline ? { "X-POS-Offline": "1" } : {} },
    );
    return res.data;
  },
  async createOperation(payload: PosOperationPayload): Promise<PosOperationResult> {
    const res = await api.post<PosOperationResult>("/pos/operations", payload);
    return res.data;
  },
  /**
   * El reenvío de una venta que YA se cobró y quedó en la cola.
   *
   * `X-POS-Offline` va en header y no en el cuerpo a propósito: la huella de
   * idempotencia se calcula sobre el cuerpo, así que agregarle un campo al
   * reintento haría que el servidor conteste `IDEMPOTENCY_KEY_REUSED` con la
   * venta existiendo. Le dice al servidor "esto ya se cobró": no la rechaces
   * por stock, registrá la salida pendiente.
   */
  async syncOperation(
    payload: PosOperationPayload,
    { offline = true }: { offline?: boolean } = {},
  ): Promise<PosOperationResult> {
    // `offline: false` para RECLAMAR con el contenido original: una venta
    // migrada de B13 puede existir ya en el servidor, y lo único que la
    // recupera es mandar exactamente lo que se mandó la primera vez. El header
    // no entra en la huella, pero `charged_at` —que el header exige— sí.
    const res = await api.post<PosOperationResult>("/pos/operations", payload, {
      headers: offline ? { "X-POS-Offline": "1" } : {},
    });
    return res.data;
  },
  async openCashSession(clientSessionId: string, openingFloatArs: string): Promise<PosCashSession> {
    const res = await api.post<PosCashSession>("/pos/cash-sessions", {
      client_session_id: clientSessionId,
      opening_float_ars: openingFloatArs,
    });
    return res.data;
  },
  async listCashSessions(
    params: { open?: boolean; terminalId?: string | null; limit?: number } = {},
  ): Promise<PosCashSession[]> {
    const query: Record<string, string | number | boolean> = { limit: params.limit ?? 50 };
    if (params.open) query.open = true;
    if (params.terminalId) query.terminal_id = params.terminalId;
    const res = await api.get<PosCashSession[]>("/pos/cash-sessions", { params: query });
    return res.data;
  },
  async getCashSession(id: string): Promise<PosCashSession> {
    const res = await api.get<PosCashSession>(`/pos/cash-sessions/${id}`);
    return res.data;
  },
  async closeCashSession(
    id: string,
    countedCashArs: string,
    notes: string | null,
  ): Promise<PosCashSession> {
    const res = await api.post<PosCashSession>(`/pos/cash-sessions/${id}/close`, {
      counted_cash_ars: countedCashArs,
      notes,
    });
    return res.data;
  },
  async abandonCashSession(id: string, reason: string): Promise<PosCashSession> {
    const res = await api.post<PosCashSession>(`/pos/cash-sessions/${id}/abandon`, { reason });
    return res.data;
  },
  async voidOperation(id: string, reason: string): Promise<PosOperationResult> {
    const res = await api.post<PosOperationResult>(`/pos/operations/${id}/void`, { reason });
    return res.data;
  },

  async listStockDiscrepancies(abiertas = true): Promise<PosStockDiscrepancy[]> {
    const res = await api.get<PosStockDiscrepancy[]>("/pos/stock-discrepancies", {
      params: { open: abiertas },
    });
    return res.data;
  },
  async resolveStockDiscrepancy(
    id: string,
    resolution: ResolucionDeDiferencia,
    reason: string,
  ): Promise<PosStockDiscrepancy> {
    const res = await api.post<PosStockDiscrepancy>(
      `/pos/stock-discrepancies/${id}/resolve`,
      { resolution, reason },
    );
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
  /** El dueño, con PIN y desde la caja nueva, autoriza que suba las ventas
   *  pendientes que cobró la caja que esta PC reemplazó. */
  async adoptPendingOperations(
    previousTerminalId: string | null,
    clientOperationIds: string[],
  ): Promise<{ terminal_id: string; adopted: number }> {
    const res = await api.post<{ terminal_id: string; adopted: number }>(
      "/pos/terminals/adopt-pending",
      { previous_terminal_id: previousTerminalId, client_operation_ids: clientOperationIds },
    );
    return res.data;
  },
  async disableTerminal(id: string): Promise<PosTerminal> {
    const res = await api.post<PosTerminal>(`/pos/terminals/${id}/disable`);
    return res.data;
  },
};
