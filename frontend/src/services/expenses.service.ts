import { api } from "@/lib/api";
import type { ExpenseSummaryResponse } from "@/types/api";

export type { ExpenseSummaryResponse };

export interface CreateExpensePayload {
  amount: number;
  category: string;
  expense_date: string; // YYYY-MM-DD
  description?: string;
  is_recurring?: boolean;
  payment_method?: string;
  supplier_name?: string | null;
  notes?: string | null;
}

export interface ExpenseEntryResponse {
  id: string;
  tenant_id: string;
  amount: number;
  category: string;
  transaction_date: string;
  description: string;
  is_recurring: boolean;
  payment_method: string;
  supplier_name: string | null;
  notes: string | null;
  created_at: string;
}

export interface ExpensesListParams {
  from_date?: string;
  to_date?: string;
  category?: string;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const expensesService = {
  async createExpense(
    payload: CreateExpensePayload,
  ): Promise<ExpenseEntryResponse> {
    const res = await api.post<ExpenseEntryResponse>("/expenses", payload);
    return res.data;
  },

  async getSummary(): Promise<ExpenseSummaryResponse> {
    const res = await api.get<ExpenseSummaryResponse>("/expenses/summary");
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
