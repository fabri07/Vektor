import { api } from "@/lib/api";

export interface CreateCustomerPayload {
  name: string;
  email?: string | null;
  phone?: string | null;
  telegram_username?: string | null;
  notes?: string | null;
  custom_fields?: Record<string, unknown>;
}

export type UpdateCustomerPayload = Partial<CreateCustomerPayload>;

export interface CustomerResponse {
  id: string;
  tenant_id: string;
  name: string;
  email: string | null;
  phone: string | null;
  telegram_username: string | null;
  notes: string | null;
  custom_fields?: Record<string, unknown>;
  is_active: boolean;
  created_at: string;
}

export interface CustomersListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const customersService = {
  async createCustomer(
    payload: CreateCustomerPayload,
    idempotencyKey?: string,
  ): Promise<CustomerResponse> {
    const res = await api.post<CustomerResponse>(
      "/customers",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateCustomer(id: string, payload: UpdateCustomerPayload): Promise<CustomerResponse> {
    const res = await api.patch<CustomerResponse>(`/customers/${id}`, payload);
    return res.data;
  },

  async deleteCustomer(id: string): Promise<{ message: string }> {
    const res = await api.delete<{ message: string }>(`/customers/${id}`);
    return res.data;
  },

  async getCustomer(id: string): Promise<CustomerResponse> {
    const res = await api.get<CustomerResponse>(`/customers/${id}`);
    return res.data;
  },

  async getCustomers(params?: CustomersListParams): Promise<CustomerResponse[]> {
    const res = await api.get<CustomerResponse[]>("/customers", { params });
    return res.data;
  },

  async getAllCustomers(
    params?: Omit<CustomersListParams, "limit" | "offset">,
  ): Promise<CustomerResponse[]> {
    const items: CustomerResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await customersService.getCustomers({
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
