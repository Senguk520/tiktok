import { apiRequest } from './client'

export interface ServiceStatus {
  service: string
  status: string
  database: string
}

export interface SessionStatus {
  authenticated: boolean
  expires_at: string
}

export interface SessionCreated extends SessionStatus {
  csrf_token: string
}

export interface SessionEnded {
  authenticated: false
}

export interface ShopQuota {
  stage: string | null
  listing_limit: number | null
  locally_submitted_count: number
  confirmed_at: string
  expires_at: string
}

export interface ShopSummary {
  id: string
  shop_id: string
  shop_code: string | null
  region: string
  seller_type: string | null
  shop_status: string
  kyc_status: string
  listing_mode: 'LOCAL_REPLICATION' | 'GLOBAL_LEGACY' | 'UNKNOWN'
  authorization_status: string
  granted_scopes: string[]
  missing_scopes: string[]
  scope_captured_at: string | null
  access_expires_at: string | null
  quota: ShopQuota | null
  selectable: boolean
  product_read_enabled: boolean
  product_write_preconditions_met: boolean
  order_read_enabled: boolean
  product_read_blockers: string[]
  product_write_blockers: string[]
  order_read_blockers: string[]
}

export interface MiaoshouCapabilities {
  provider: 'miaoshou'
  configured: boolean
  shop_query_enabled: boolean
  blockers: string[]
}

export interface MiaoshouShop {
  shop_id: string
  shop_name: string | null
  platform: string
  site: string
  site_name: string | null
  status: string | null
  authorization_expires_at: string | null
  last_authorized_at: string | null
  parent_shop_id: string | null
  is_cross_border: boolean | null
  is_global: boolean | null
}

export interface MiaoshouShopPage {
  provider: 'miaoshou'
  platform: string
  site: string
  page_no: number
  page_size: number
  next_page_no: number | null
  items: MiaoshouShop[]
}

export interface ProductCapabilities {
  platform_configured: boolean
  master_key_configured: boolean
  image_upload_enabled: boolean
  product_submission_enabled: boolean
  blockers: string[]
}

export interface ProductSkuInput {
  seller_sku: string
  price: string
  currency: string
  inventory_by_warehouse: Record<string, number>
  attributes: Record<string, string>
}

export interface ProductDraftInput {
  title: string
  description: string
  category_id: string | null
  skus: ProductSkuInput[]
  images: Array<{ source_ref: string; role: string }>
  attributes: Record<string, string>
  unmapped_warnings: string[]
}

export interface ProductIntent extends ProductDraftInput {
  images: Array<{ source_ref: string; role: string; platform_image_bound: boolean }>
}

export interface ProductDraft {
  id: string
  status: string
  human_confirmed: boolean
  created: boolean | null
  product: ProductIntent
}

export interface QuotaConfirmationInput {
  listing_limit: number | null
  locally_submitted_count: number
  confirmed_at: string
  expires_at: string
  stage: string | null
}

export interface QuotaSnapshot {
  id: string
  region: string
  listing_limit: number | null
  locally_submitted_count: number
  confirmed_at: string
  expires_at: string
  source: string
}

export interface ProductSubmission {
  mode: string
  product_id: string
  operation_id: string
  request_id: string | null
  replayed: boolean
}

export interface RemoteProduct {
  product_id: string
  title: string | null
  status: string | null
  seller_skus: string[]
}

export interface RemoteProductPage {
  mode: string
  items: RemoteProduct[]
  next_page_token: string | null
  total_count: number | null
  request_id: string | null
}

export interface OrderLine {
  line_id: string
  product_id: string | null
  sku_id: string | null
  seller_sku: string | null
  status: string | null
  quantity: number
  currency: string | null
  sale_price: string | null
}

export interface Order {
  order_id: string
  status: string
  fulfillment_type: string | null
  shipping_type: string | null
  currency: string | null
  total_amount: string | null
  item_count: number
  created_at: string | null
  updated_at: string | null
  lines: OrderLine[]
}

export interface OrderPage {
  orders: Order[]
  next_page_token: string | null
  total_count: number | null
  request_id: string | null
}

export interface OrderDetails {
  orders: Order[]
}

export interface OrderSyncInput {
  window_start: string
  window_end: string
  page_size: number
  max_pages: number
}

export interface OrderSyncResult {
  pages: number
  listed_orders: number
  detailed_orders: number
  completed: boolean
  next_page_token: string | null
  window_start: string
  window_end: string
}

export interface ToolsCapabilities {
  translation_configured: boolean
  translation_provider: string | null
  supported_translation_languages: string[]
  translation_cache_enabled: false
  blockers: string[]
}

export interface TranslationInput {
  texts: string[]
  source_language: string
  target_language: string
}

export interface TranslationResult {
  texts: string[]
  source_language: string
  target_language: string
  provider: string
  provider_request_id: string | null
  cached: false
}

export interface ProfitInput {
  product_cost: string
  source_currency: string
  settlement_currency: string
  exchange_rate: string
  shipping_cost: string
  other_fixed_cost: string
  commission_rate: string
  payment_fee_rate: string
  target_margin_rate: string
  selling_price?: string | null
}

export interface ProfitResult {
  settlement_currency: string
  converted_product_cost: string
  total_fixed_cost: string
  suggested_price: string
  commission_amount: string
  payment_fee_amount: string
  estimated_profit: string
  realized_margin_rate: string
  realized_margin_percent: string
  exchange_rate: string
}

export interface ScheduleCapabilities {
  worker_enabled: boolean
  publish_draft_enabled: boolean
  order_sync_enabled: boolean
  blockers: string[]
}

export type ScheduleJobType = 'PUBLISH_DRAFT' | 'SYNC_ORDERS'
export type ScheduleKind = 'ONCE' | 'INTERVAL'

export interface ScheduleCreateInput {
  job_type: ScheduleJobType
  schedule_kind: ScheduleKind
  run_at: string
  interval_seconds: number | null
  payload: Record<string, unknown>
}

export interface ScheduleJob {
  id: string
  job_type: ScheduleJobType
  schedule_kind: ScheduleKind
  interval_seconds: number | null
  run_at: string | null
  next_run_at: string
  enabled: boolean
  payload: Record<string, unknown>
  required_scopes: string[]
  required_listing_mode: string | null
  quota_cost: number
  created_at: string
  updated_at: string
}

export interface ScheduleRun {
  id: string
  state: string
  worker_id: string
  started_at: string
  finished_at: string | null
  summary: Record<string, unknown>
  error_code: string | null
}

export interface AuditFact {
  id: string
  event_type: string
  request_id: string | null
  resource_type: string | null
  resource_id: string | null
  outcome: string
  details: Record<string, unknown>
  created_at: string
}

export interface WebhookCapabilities {
  receiver_enabled: boolean
  signature_contract_verified: boolean
  state_changes_enabled: boolean
  blockers: string[]
}

const encoded = (value: string): string => encodeURIComponent(value)
const shopBase = (shopBindingId: string): string => `/api/shops/${encoded(shopBindingId)}`
const idempotencyKey = (operation: string): string => `${operation}:${crypto.randomUUID()}`

const pageQuery = (pageSize: number, pageToken?: string | null): string => {
  const query = new URLSearchParams({ page_size: String(pageSize) })
  if (pageToken) query.set('page_token', pageToken)
  return query.toString()
}

const idsQuery = (ids: readonly string[]): string => {
  const query = new URLSearchParams()
  ids.forEach((id) => query.append('ids', id))
  return query.toString()
}

export const coreApi = {
  health: (): Promise<ServiceStatus> => apiRequest<ServiceStatus>('/healthz'),

  sessionStatus: (): Promise<SessionStatus> => apiRequest<SessionStatus>('/api/session'),
  login: (bootstrapSecret: string): Promise<SessionCreated> =>
    apiRequest<SessionCreated, { bootstrap_secret: string }>('/api/session', {
      method: 'POST',
      body: { bootstrap_secret: bootstrapSecret },
    }),
  logout: (csrfToken: string): Promise<SessionEnded> =>
    apiRequest<SessionEnded>('/api/session', { method: 'DELETE', csrfToken }),

  shops: async (): Promise<ShopSummary[]> =>
    (await apiRequest<{ items: ShopSummary[] }>('/api/shops')).items,

  miaoshouCapabilities: (): Promise<MiaoshouCapabilities> =>
    apiRequest<MiaoshouCapabilities>('/api/miaoshou/capabilities'),
  miaoshouShops: (
    platform: 'tiktok' | 'tiktokGlobal',
    site: string,
    pageNo = 1,
    pageSize = 100,
  ): Promise<MiaoshouShopPage> => {
    const query = new URLSearchParams({
      platform,
      site,
      page_no: String(pageNo),
      page_size: String(pageSize),
    })
    return apiRequest<MiaoshouShopPage>(`/api/miaoshou/shops?${query.toString()}`)
  },

  productCapabilities: (shopBindingId: string): Promise<ProductCapabilities> =>
    apiRequest<ProductCapabilities>(`${shopBase(shopBindingId)}/products/capabilities`),
  createDraft: (
    shopBindingId: string,
    payload: ProductDraftInput,
    csrfToken: string,
  ): Promise<ProductDraft> =>
    apiRequest<ProductDraft, ProductDraftInput>(`${shopBase(shopBindingId)}/products/drafts`, {
      method: 'POST',
      body: payload,
      csrfToken,
    }),
  confirmDraft: (shopBindingId: string, draftId: string, csrfToken: string): Promise<ProductDraft> =>
    apiRequest<ProductDraft>(`${shopBase(shopBindingId)}/products/drafts/${encoded(draftId)}/confirm`, {
      method: 'POST',
      csrfToken,
    }),
  confirmQuota: (
    shopBindingId: string,
    payload: QuotaConfirmationInput,
    csrfToken: string,
  ): Promise<QuotaSnapshot> =>
    apiRequest<QuotaSnapshot, QuotaConfirmationInput>(
      `${shopBase(shopBindingId)}/products/quota-confirmations`,
      { method: 'POST', body: payload, csrfToken },
    ),
  submitDraft: (
    shopBindingId: string,
    draftId: string,
    csrfToken: string,
  ): Promise<ProductSubmission> =>
    apiRequest<ProductSubmission>(`${shopBase(shopBindingId)}/products/drafts/${encoded(draftId)}/submit`, {
      method: 'POST',
      csrfToken,
      idempotencyKey: idempotencyKey('product-submit'),
    }),
  remoteProducts: (
    shopBindingId: string,
    pageSize = 20,
    pageToken?: string | null,
  ): Promise<RemoteProductPage> =>
    apiRequest<RemoteProductPage>(
      `${shopBase(shopBindingId)}/products/remote?${pageQuery(pageSize, pageToken)}`,
    ),
  remoteProduct: (shopBindingId: string, productId: string): Promise<RemoteProduct> =>
    apiRequest<RemoteProduct>(`${shopBase(shopBindingId)}/products/remote/${encoded(productId)}`),

  remoteOrders: (
    shopBindingId: string,
    pageSize = 20,
    pageToken?: string | null,
  ): Promise<OrderPage> =>
    apiRequest<OrderPage>(
      `${shopBase(shopBindingId)}/orders/remote?${pageQuery(pageSize, pageToken)}`,
    ),
  remoteOrderDetails: (shopBindingId: string, ids: readonly string[]): Promise<OrderDetails> =>
    apiRequest<OrderDetails>(`${shopBase(shopBindingId)}/orders/remote/details?${idsQuery(ids)}`),
  localOrderDetails: (shopBindingId: string, ids: readonly string[]): Promise<OrderDetails> =>
    apiRequest<OrderDetails>(`${shopBase(shopBindingId)}/orders/local/details?${idsQuery(ids)}`),
  syncOrders: (
    shopBindingId: string,
    payload: OrderSyncInput,
    csrfToken: string,
  ): Promise<OrderSyncResult> =>
    apiRequest<OrderSyncResult, OrderSyncInput>(`${shopBase(shopBindingId)}/orders/sync`, {
      method: 'POST',
      body: payload,
      csrfToken,
    }),

  toolsCapabilities: (shopBindingId: string): Promise<ToolsCapabilities> =>
    apiRequest<ToolsCapabilities>(`${shopBase(shopBindingId)}/tools/capabilities`),
  translate: (
    shopBindingId: string,
    payload: TranslationInput,
    csrfToken: string,
  ): Promise<TranslationResult> =>
    apiRequest<TranslationResult, TranslationInput>(`${shopBase(shopBindingId)}/tools/translate`, {
      method: 'POST',
      body: payload,
      csrfToken,
      idempotencyKey: idempotencyKey('translate'),
    }),
  profit: (shopBindingId: string, payload: ProfitInput, csrfToken: string): Promise<ProfitResult> =>
    apiRequest<ProfitResult, ProfitInput>(`${shopBase(shopBindingId)}/tools/profit`, {
      method: 'POST',
      body: payload,
      csrfToken,
    }),

  scheduleCapabilities: (shopBindingId: string): Promise<ScheduleCapabilities> =>
    apiRequest<ScheduleCapabilities>(`${shopBase(shopBindingId)}/schedules/capabilities`),
  schedules: async (shopBindingId: string): Promise<ScheduleJob[]> =>
    (
      await apiRequest<{ items: ScheduleJob[] }>(`${shopBase(shopBindingId)}/schedules`)
    ).items,
  createSchedule: (
    shopBindingId: string,
    payload: ScheduleCreateInput,
    csrfToken: string,
  ): Promise<ScheduleJob> =>
    apiRequest<ScheduleJob, ScheduleCreateInput>(`${shopBase(shopBindingId)}/schedules`, {
      method: 'POST',
      body: payload,
      csrfToken,
      idempotencyKey: idempotencyKey('schedule-create'),
    }),
  setScheduleState: (
    shopBindingId: string,
    scheduleId: string,
    enabled: boolean,
    csrfToken: string,
  ): Promise<ScheduleJob> =>
    apiRequest<ScheduleJob, { enabled: boolean }>(
      `${shopBase(shopBindingId)}/schedules/${encoded(scheduleId)}`,
      {
        method: 'PATCH',
        body: { enabled },
        csrfToken,
        idempotencyKey: idempotencyKey('schedule-state'),
      },
    ),
  scheduleRuns: async (shopBindingId: string, scheduleId: string): Promise<ScheduleRun[]> =>
    (
      await apiRequest<{ items: ScheduleRun[] }>(
        `${shopBase(shopBindingId)}/schedules/${encoded(scheduleId)}/runs`,
      )
    ).items,

  audits: async (shopBindingId: string, limit = 100, before?: string): Promise<AuditFact[]> => {
    const query = new URLSearchParams({ limit: String(limit) })
    if (before) query.set('before', before)
    return (
      await apiRequest<{ items: AuditFact[] }>(
        `${shopBase(shopBindingId)}/audits?${query.toString()}`,
      )
    ).items
  },

  webhookCapabilities: (): Promise<WebhookCapabilities> =>
    apiRequest<WebhookCapabilities>('/api/webhooks/tiktok/capabilities'),
}