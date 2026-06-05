import { api } from "@/lib/api";

export interface CreateProductPayload {
  name: string;
  sale_price_ars: number;
  unit_cost_ars?: number | null;
  stock_units?: number;
  // null = usar DEFAULT_LOW_STOCK_THRESHOLD_UNITS del servidor; 0 = umbral explícito
  low_stock_threshold_units?: number | null;
  category?: string | null;
  sku?: string | null;
  description?: string | null;
}

export type UpdateProductPayload = Partial<CreateProductPayload> & {
  is_active?: boolean;
};

export interface ProductResponse {
  id: string;
  tenant_id: string;
  name: string;
  sku: string | null;
  description: string | null;
  category: string | null;
  sale_price_ars: number;
  unit_cost_ars: number | null;
  stock_units: number;
  // null = umbral no configurado (servidor aplica DEFAULT_LOW_STOCK_THRESHOLD_UNITS = 5)
  // 0   = umbral explícito: solo sin-stock aplica
  low_stock_threshold_units: number | null;
  is_active: boolean;
  margin_pct: number | null;
  is_low_stock: boolean;
  stock_status: "in_stock" | "low_stock" | "out_of_stock";
  custom_fields?: Record<string, unknown>;
}

export interface ProductsListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const productsService = {
  async createProduct(payload: CreateProductPayload): Promise<ProductResponse> {
    const res = await api.post<ProductResponse>("/products", payload);
    return res.data;
  },

  async updateProduct(id: string, payload: UpdateProductPayload): Promise<ProductResponse> {
    const res = await api.patch<ProductResponse>(`/products/${id}`, payload);
    return res.data;
  },

  async deleteProduct(id: string): Promise<void> {
    await api.delete(`/products/${id}`);
  },

  async getProducts(params?: ProductsListParams): Promise<ProductResponse[]> {
    const res = await api.get<ProductResponse[]>("/products", { params });
    return res.data;
  },

  async getAllProducts(
    params?: Omit<ProductsListParams, "limit" | "offset">,
  ): Promise<ProductResponse[]> {
    const items: ProductResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await productsService.getProducts({
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
