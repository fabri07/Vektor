import { api } from "@/lib/api";

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

export const expensesService = {
  async createExpense(
    payload: CreateExpensePayload,
  ): Promise<ExpenseEntryResponse> {
    const res = await api.post<ExpenseEntryResponse>("/expenses/", payload);
    return res.data;
  },
};
