import { api } from "@/lib/api";

export interface CreateSupplierPayload {
  name: string;
  last_name?: string | null;
  cuil?: string | null;
  payment_method?: string | null;
  email?: string | null;
  phone?: string | null;
  notes?: string | null;
  catalog_url?: string | null;
  api_url?: string | null;
  custom_fields?: Record<string, unknown>;
}

export type UpdateSupplierPayload = Partial<CreateSupplierPayload>;

export interface SupplierResponse {
  id: string;
  tenant_id: string;
  name: string;
  last_name: string | null;
  cuil: string | null;
  payment_method: string | null;
  email: string | null;
  phone: string | null;
  notes: string | null;
  catalog_url: string | null;
  api_url: string | null;
  custom_fields?: Record<string, unknown>;
  is_active: boolean;
  /** True si es el proveedor centinela "No identificado". Computado en el backend. */
  is_sentinel: boolean;
  /**
   * True si es un proveedor provisional derivado de una marca por un script de
   * reparación. El usuario debe validarlo o reasignarlo. Computado en el backend.
   */
  is_provisional: boolean;
  /**
   * True si era una marca confundida con proveedor y colapsada por error.
   * El backend nunca lo lista ni lo deja reactivar (409 BRAND_COLLAPSED).
   */
  is_brand_collapsed?: boolean;
  created_at: string;
}

export interface SupplierProductPurchase {
  product_id: string;
  name: string;
  last_purchase_at: string | null;
  total_qty: number;
  unit_price: number;
}

/** Grupo de productos de un proveedor bajo una misma marca (o sin marca). */
export interface SupplierBrandGroup {
  /** Nombre de la marca; null = productos sin marca ("Productos genéricos"). */
  brand: string | null;
  /** True si el proveedor es "proveedor oficial" de esta marca (razón social == marca). */
  is_official: boolean;
  products: SupplierProductPurchase[];
}

export interface SupplierProductsGrouped {
  groups: SupplierBrandGroup[];
}

export interface ReceiptLinePayload {
  product_name: string;
  sku?: string;
  qty: number;
  unit_price: number;
}

export interface UploadReceiptPayload {
  lines: ReceiptLinePayload[];
  shipping_cost?: number;
  currency: "ARS";
  transaction_date?: string;
  source_upload_id?: string;
}

/** Línea extraída por IA/parser de un remito. Las cantidades/precios son sugerencias editables. */
export interface ReceiptExtractionLine {
  product_name: string;
  sku: string | null;
  qty: number;
  unit_price: number;
}

/**
 * Resultado de leer el archivo del remito. La IA (foto/PDF) o el parser
 * (planilla) transcribe las líneas; el usuario las revisa antes de confirmar.
 */
export interface ReceiptExtraction {
  lines: ReceiptExtractionLine[];
  shipping_cost: number | null;
  currency: "ARS";
  confidence: "HIGH" | "MEDIUM" | "LOW";
  warnings: string[];
  source_upload_id: string | null;
}

export interface SuppliersListParams {
  is_active?: boolean;
  /** Incluir proveedores dados de baja (se muestran en rojo). */
  include_inactive?: boolean;
  limit?: number;
  offset?: number;
}

const PAGE_SIZE = 200;
const MAX_PAGES = 25;

export const suppliersService = {
  async createSupplier(
    payload: CreateSupplierPayload,
    idempotencyKey?: string,
  ): Promise<SupplierResponse> {
    const res = await api.post<SupplierResponse>(
      "/suppliers",
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async updateSupplier(id: string, payload: UpdateSupplierPayload): Promise<SupplierResponse> {
    const res = await api.patch<SupplierResponse>(`/suppliers/${id}`, payload);
    return res.data;
  },

  async deleteSupplier(id: string, force = false): Promise<{ message: string }> {
    const res = await api.delete<{ message: string }>(`/suppliers/${id}`, {
      params: force ? { force: true } : undefined,
    });
    return res.data;
  },

  async reactivateSupplier(id: string): Promise<SupplierResponse> {
    const res = await api.post<SupplierResponse>(`/suppliers/${id}/reactivate`);
    return res.data;
  },

  async getSupplier(id: string): Promise<SupplierResponse> {
    const res = await api.get<SupplierResponse>(`/suppliers/${id}`);
    return res.data;
  },

  async getSuppliers(params?: SuppliersListParams): Promise<SupplierResponse[]> {
    const res = await api.get<SupplierResponse[]>("/suppliers", { params });
    return res.data;
  },

  async getAllSuppliers(
    params?: Omit<SuppliersListParams, "limit" | "offset">,
  ): Promise<SupplierResponse[]> {
    const items: SupplierResponse[] = [];

    for (let page = 0; page < MAX_PAGES; page += 1) {
      const batch = await suppliersService.getSuppliers({
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

  async getSupplierProducts(id: string): Promise<SupplierProductsGrouped> {
    const res = await api.get<SupplierProductsGrouped>(`/suppliers/${id}/products`);
    return res.data;
  },

  async uploadReceipt(
    supplierId: string,
    payload: UploadReceiptPayload,
    idempotencyKey?: string,
  ): Promise<unknown> {
    const res = await api.post<unknown>(
      `/suppliers/${supplierId}/receipts`,
      payload,
      idempotencyKey ? { headers: { "Idempotency-Key": idempotencyKey } } : undefined,
    );
    return res.data;
  },

  async extractReceipt(supplierId: string, file: File): Promise<ReceiptExtraction> {
    const formData = new FormData();
    formData.append("file", file);
    // NO setear Content-Type a mano: el browser/axios lo ponen con el boundary del
    // multipart. Forzar "multipart/form-data" sin boundary rompe el parseo en FastAPI.
    const res = await api.post<ReceiptExtraction>(
      `/suppliers/${supplierId}/receipts/extract`,
      formData,
    );
    return res.data;
  },
};
