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

export const productsService = {
  async createProduct(payload: CreateProductPayload): Promise<ProductResponse> {
    const res = await api.post<ProductResponse>("/products/", payload);
    return res.data;
  },
};
