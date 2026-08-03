import { ApiError } from '@/api/client'

const blockerLabels: Readonly<Record<string, string>> = {
  BLOCKED_LIVE_CREDENTIALS: 'TikTok 平台凭据未配置',
  BLOCKED_MASTER_KEY: '主密钥未配置',
  BLOCKED_UNVERIFIED_IMAGE_UPLOAD_ENDPOINT: '图片上传端点尚未完成真实核验',
  BLOCKED_UNVERIFIED_LIVE_PRODUCT_VALIDATION: '商品写入契约尚未完成真实核验',
  BLOCKED_AZURE_TRANSLATOR_CONFIGURATION: 'Azure Translator 未配置',
  BLOCKED_SHOP_AUTHORIZATION: '店铺授权不是可用状态',
  BLOCKED_UNVERIFIED_TIKTOK_WEBHOOK_SIGNATURE_CONTRACT: 'Webhook 签名契约尚未核验',
}

export const blockerLabel = (code: string): string => blockerLabels[code] ?? code

export const errorText = (error: unknown): string => {
  if (error instanceof ApiError) {
    return error.requestId ? `${error.message} · 请求 ${error.requestId}` : error.message
  }
  return error instanceof Error ? error.message : '操作失败，请稍后重试'
}

export const formatDateTime = (value: string | null | undefined): string => {
  if (!value) return '—'
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN', { hour12: false })
}

export const toIsoString = (value: Date): string => value.toISOString()

export const commaSeparated = (value: string): string[] =>
  value
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)