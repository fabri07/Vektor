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
  acquired_at?: string | null;
  custom_fields?: Record<string, unknown>;
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
  // FASE 3 (B2): producto auto-creado por import al que le faltan precio/costo.
  requires_completion?: boolean;
  margin_pct: number | null;
  is_low_stock: boolean;
  stock_status: "in_stock" | "low_stock" | "out_of_stock";
  // F6-B4: estado de vencimiento a nivel producto (null = sin vencimiento conocido).
  expiry_status?: "expired" | "expiring_soon" | "ok" | null;
  expiry_date?: string | null;
  custom_fields?: Record<string, unknown>;
  acquired_at?: string | null;
  created_at?: string;
}

export interface ProductsListParams {
  is_active?: boolean;
  limit?: number;
  offset?: number;
}

/** Categoría canónica del catálogo del vertical del tenant. */
export interface ProductCategoryOption {
  code: string;
  label: string;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;
// Corrección C4 (revisión externa 2026-08-19): `getAllProducts` acumula hasta
// esta cantidad y corta en silencio — el caller necesita saber el techo para
// poder avisar si lo alcanzó de verdad (ver `countProducts`).
export const PRODUCTS_MAX_ACCUMULATED = PAGE_SIZE * MAX_PAGES;

export const productsService = {
  async createProduct(
    payload: CreateProductPayload,
    idempotencyKey?: string,
  ): Promise<ProductResponse> {
    const res = await api.post<ProductResponse>(
      "/products",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateProduct(id: string, payload: UpdateProductPayload): Promise<ProductResponse> {
    const res = await api.patch<ProductResponse>(`/products/${id}`, payload);
    return res.data;
  },

  async deleteProduct(id: string): Promise<void> {
    await api.delete(`/products/${id}`);
  },

  async getCategories(): Promise<ProductCategoryOption[]> {
    const res = await api.get<ProductCategoryOption[]>("/products/categories");
    return res.data;
  },

  // Crea una categoría de producto personalizada del tenant (reutilizable).
  async createCategory(label: string): Promise<ProductCategoryOption> {
    const res = await api.post<ProductCategoryOption>("/products/custom-categories", { label });
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

  // Corrección C4: total real del catálogo (mismos filtros que `getProducts`/
  // `getAllProducts`, sin `limit`/`offset`) — permite avisar si `getAllProducts`
  // cortó en `PRODUCTS_MAX_ACCUMULATED` sin haber traído todo.
  async countProducts(
    params?: Omit<ProductsListParams, "limit" | "offset">,
  ): Promise<number> {
    const res = await api.get<{ total: number }>("/products/count", { params });
    return res.data.total;
  },
};
