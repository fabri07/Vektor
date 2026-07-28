export interface PaginatedResponse<T> {
  items: T[];
  total: number;
  page: number;
  size: number;
  pages: number;
}

export interface PydanticError {
  type: string;
  loc: (string | number)[];
  msg: string;
  input: unknown;
  ctx?: Record<string, unknown>;
}

export interface ApiError {
  detail: string | PydanticError[];
}

export interface TokenResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
}

export interface AuthUserResponse {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  tenant_id: string;
  phone: string | null;
}

/** Respuesta de /users/* (UserResponse del backend). */
export interface UserResponse extends AuthUserResponse {
  is_active: boolean;
  created_at: string;
  last_login_at: string | null;
}

export interface MeResponse {
  user_id: string;
  email: string;
  full_name: string;
  role_code: string;
  tenant_id: string;
  phone: string | null;
  subscription: {
    plan_code: string;
    status: string;
  } | null;
  onboarding_completed: boolean;
}

export interface AuthResponse {
  access_token: string;
  refresh_token: string;
  token_type: "bearer";
  expires_in: number;
  user: AuthUserResponse;
}

export interface ForecastPoint {
  date: string;
  income: number;
  expense: number;
  net: number;
}

export interface CashForecastResponse {
  tier: number;
  confidence: string;
  data_days: number;
  horizon_days: number;
  points: ForecastPoint[];
  message: string | null;
}

export interface CategoryBreakdownItem {
  category: string;
  total: number;
  pct: number;
}

export interface ProductStockItem {
  product_id: string;
  name: string;
  stock_units: number;
  low_stock_threshold_units: number | null;
  sale_price_ars: number;
  /** Plata inmovilizada: stock_units × unit_cost_ars (0 si no hay costo). */
  immobilized_value: number;
}

export interface SupplierBreakdownProductItem {
  product_id: string;
  name: string;
  brand: string;
  total_amount: number;
  total_qty: number;
}

export interface SupplierPurchaseBreakdownItem {
  supplier_id: string | null;
  supplier_name: string;
  is_unassigned: boolean;
  total_purchased: number;
  /** % de compras del proveedor con costo unitario conocido. */
  coverage_pct: number;
  products: SupplierBreakdownProductItem[];
}

export interface BusinessBreakdownResponse {
  period_days: number;
  from_date: string;
  to_date: string;
  expenses_by_category: CategoryBreakdownItem[];
  /** Compras de mercadería agrupadas por proveedor real (acordeón). */
  suppliers_breakdown: SupplierPurchaseBreakdownItem[];
  low_stock_products: ProductStockItem[];
  no_rotation_products: ProductStockItem[];
  low_stock_count: number;
  no_rotation_count: number;
  total_products: number;
  /** Plata parada total en productos sin rotación + cuántos no se pudieron valuar. */
  no_rotation_value_total: number;
  no_rotation_unvalued_count: number;
  /** FASE D: gasto operativo vs compra de mercadería del período. */
  opex_total: number;
  cogs_total: number;
  /** Valor actual del stock (stock_units × unit_cost_ars de productos activos). */
  stock_value_total: number;
}

export interface HealthScoreV2Response {
  id: string;
  tenant_id: string;
  score_total: number;
  score_cash: number;
  score_margin: number;
  score_stock: number;
  score_supplier: number;
  score_growth: number | null;   // null = snapshot v1 (pre-Stage-5a)
  primary_risk_code: string;
  confidence_level: string;
  data_completeness_score: number;
  level: string;
  created_at: string;
  is_demo_data?: boolean;
  // Fuente de la caja: "arqueo" | "onboarding" | "flujo" | "desconocido" | null.
  // "flujo" → mostrar recordatorio de arqueo. null = snapshot viejo.
  cash_source?: "arqueo" | "onboarding" | "flujo" | "desconocido" | null;
}

export interface CalculatingResponse {
  status: "CALCULATING";
}

export type LatestScoreResponse = HealthScoreV2Response | CalculatingResponse;

export interface InsightResponse {
  id: string;
  title: string;
  description: string;
  insight_type: string;
  severity_code: string;
  heuristic_version: string;
  created_at: string;
}

export interface ActionSuggestionResponse {
  id: string;
  title: string;
  description: string;
  action_type: string;
  risk_level: string;
  status: string;
  created_at: string;
}

export interface CurrentInsightResponse {
  insight: InsightResponse;
  action_suggestion: ActionSuggestionResponse | null;
}

// ── Notifications ─────────────────────────────────────────────────────────────

export interface NotificationItem {
  id: string;
  title: string;
  body: string;
  notification_type: string;
  channel: string;
  is_read: boolean;
  created_at: string;
  action_url: string | null;
}

export interface NotificationListResponse {
  notifications: NotificationItem[];
  unread_count: number;
}

// ── Momentum ──────────────────────────────────────────────────────────────────

export interface WeeklyHistoryItem {
  week_start: string;
  week_end: string;
  avg_score: number;
  delta: number | null;
  trend_label: string | null;
}

export interface ActiveGoalResponse {
  weak_dimension: string;
  goal: string;
  action: string;
  estimated_delta: number;
  estimated_weeks: number;
}

export interface MilestoneItem {
  code: string;
  label: string;
  unlocked_at: string;
}

export interface MomentumProfileResponse {
  best_score_ever: number | null;
  best_score_date: string | null;
  active_goal: ActiveGoalResponse | null;
  milestones_unlocked: MilestoneItem[];
  estimated_value_protected_ars: number;
  improving_streak_weeks: number;
  weekly_history: WeeklyHistoryItem[];
}

// ── Sales ─────────────────────────────────────────────────────────────────────

export interface SaleSummaryResponse {
  total_ars: number;
  entry_count: number;
  period_covered: string;
}

// ── Expenses ──────────────────────────────────────────────────────────────────

export interface ExpenseSummaryResponse {
  total_ars: number;
  entry_count: number;
  period_covered: string;
}

// ── Files ─────────────────────────────────────────────────────────────────────

export interface UploadedFileResponse {
  id: string;
  original_filename: string;
  content_type: string;
  size_bytes: number;
  purpose: string;
  status: string;
}

// ── OAuth / Google Login ──────────────────────────────────────────────────────

export interface OAuthStartResponse {
  authorization_url: string;
}

export interface OAuthLinkRequiredResponse {
  status: "link_required";
  pending_oauth_session_id: string;
  email: string;
  provider: "google";
}


export interface GoogleConnectStartResponse {
  auth_url: string;
  required_scopes: string[];
  state: string;
}

export interface GoogleAppStatusLegacy {
  id: string;
  label: string;
  description: string;
  available: boolean;
  connected: boolean;
  needs_reconnect: boolean;
  required_scopes: string[];
}

export interface GoogleStatusResponseLegacy {
  connected: boolean;
  scopes_granted: string[];
  apps: GoogleAppStatusLegacy[];
  connected_at: string | null;
  last_error_code: string | null;
}

// ── Field Definitions ─────────────────────────────────────────────────────────

export interface EnumOption {
  value: string;
  label: string;
}

export interface FieldDefinition {
  field_key: string;
  entity_type: "sale" | "expense" | "product" | "inventory";
  label: string;
  data_type: "text" | "number" | "date" | "enum" | "boolean";
  enum_options: EnumOption[] | null;
  is_required: boolean;
  display_order: number;
  is_base_field: boolean;
  affects_scoring: boolean;
}

export interface CreateCustomFieldPayload {
  entity_type: string;
  field_key: string;
  label: string;
  data_type?: string;
  enum_options?: EnumOption[];
  display_order?: number;
}

export interface FieldChangeLog {
  id: string;
  field_key: string;
  entity_type: string;
  action: "enabled" | "disabled" | "modified" | "created" | "undo";
  previous_state: Record<string, unknown> | null;
  new_state: Record<string, unknown>;
  changed_at: string;
}
