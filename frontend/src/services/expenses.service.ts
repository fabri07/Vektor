import { api } from "@/lib/api";
import type { ExpenseSummaryResponse } from "@/types/api";
import type { DateRangeResponse } from "@/services/sales.service";

export type { ExpenseSummaryResponse };

export interface CreateExpensePayload {
  amount: number;
  category: string;
  /** Nombre personalizado cuando category === "OTHER". */
  category_label?: string;
  expense_date: string; // YYYY-MM-DD
  description?: string;
  is_recurring?: boolean;
  payment_method?: string;
  supplier_name?: string | null;
  /** Vínculo opcional a un proveedor del catálogo (entidad Supplier). */
  supplier_id?: string | null;
  notes?: string | null;
  /** OPEX = gasto operativo; COGS = compra de mercadería. */
  expense_type?: "OPEX" | "COGS";
  custom_fields?: Record<string, unknown>;
}

export type UpdateExpensePayload = Partial<CreateExpensePayload>;

export interface ExpenseEntryResponse {
  id: string;
  tenant_id: string;
  // FASE 3 (B1): vínculo opcional al producto del catálogo (compras de mercadería).
  product_id?: string | null;
  amount: number;
  category: string;
  /** Nombre personalizado cuando category === "OTHER" (si el usuario lo cargó). */
  category_label?: string | null;
  /** OPEX = gasto operativo; COGS = compra de mercadería. */
  expense_type?: "OPEX" | "COGS";
  transaction_date: string;
  description: string;
  is_recurring: boolean;
  payment_method: string;
  supplier_name: string | null;
  /** Vínculo opcional al proveedor del catálogo (entidad Supplier). */
  supplier_id: string | null;
  notes: string | null;
  custom_fields?: Record<string, unknown>;
  created_at: string;
}

export interface ExpensesListParams {
  from_date?: string;
  to_date?: string;
  category?: string;
  expense_type?: "OPEX" | "COGS";
  supplier_id?: string;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const expensesService = {
  async createExpense(
    payload: CreateExpensePayload,
    idempotencyKey?: string,
  ): Promise<ExpenseEntryResponse> {
    const res = await api.post<ExpenseEntryResponse>(
      "/expenses",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  /** Retiro de ganancias anticipadas (sueldo/retiro del dueño) → gasto PAYROLL/OPEX. */
  async profitWithdrawal(payload: {
    amount: number;
    withdrawal_date: string; // YYYY-MM-DD
    payment_method?: string;
    notes?: string | null;
  }): Promise<ExpenseEntryResponse> {
    const res = await api.post<ExpenseEntryResponse>("/expenses/profit-withdrawal", payload);
    return res.data;
  },

  // Categorías de gasto personalizadas del tenant (además de las canónicas).
  async getCustomCategories(): Promise<string[]> {
    const res = await api.get<string[]>("/expenses/custom-categories");
    return res.data;
  },

  async updateExpense(
    id: string,
    payload: UpdateExpensePayload,
  ): Promise<ExpenseEntryResponse> {
    const res = await api.patch<ExpenseEntryResponse>(`/expenses/${id}`, payload);
    return res.data;
  },

  async deleteExpense(id: string): Promise<void> {
    await api.delete(`/expenses/${id}`);
  },

  async getSummary(): Promise<ExpenseSummaryResponse> {
    const res = await api.get<ExpenseSummaryResponse>("/expenses/summary");
    return res.data;
  },

  async getDateRange(): Promise<DateRangeResponse> {
    const res = await api.get<DateRangeResponse>("/expenses/date-range");
    return res.data;
  },

  async getEntries(params?: ExpensesListParams): Promise<ExpenseEntryResponse[]> {
    const res = await api.get<ExpenseEntryResponse[]>("/expenses", { params });
    return res.data;
  },

  async getAllEntries(
    params?: Omit<ExpensesListParams, "limit" | "offset">,
  ): Promise<ExpenseEntryResponse[]> {
    const items: ExpenseEntryResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await expensesService.getEntries({
        ...params,
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
      });
      items.push(...batch);

      if (batch.length < PAGE_SIZE) {
        break;
      }
    }

    return items;
  },
};
