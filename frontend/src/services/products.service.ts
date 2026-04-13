import { api } from "@/lib/api";

export interface CreateProductPayload {
  name: string;
  sale_price_ars: number;
  unit_cost_ars?: number | null;
  stock_units?: number;
  low_stock_threshold_units?: number;
  category?: string | null;
  sku?: string | null;
  description?: string | null;
}

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
  low_stock_threshold_units: number;
  is_active: boolean;
  margin_pct: number | null;
  is_low_stock: boolean;
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
