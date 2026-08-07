import { afterEach, describe, expect, it, vi } from 'vitest'
import { defineComponent } from 'vue'
import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { ElMessage } from 'element-plus'

import { ApiError } from '@/api/client'
import { coreApi, type ShopSummary, type ToolsCapabilities } from '@/api/core'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import ToolsView from '@/views/ToolsView.vue'

const SHOP_ID = '11111111-1111-4111-8111-111111111111'
const CSRF_TOKEN = 'csrf-tools-view'

const shopSummary: ShopSummary = {
  id: SHOP_ID,
  shop_id: 'platform-shop-tools',
  shop_code: 'MY-TOOLS',
  region: 'MY',
  seller_type: null,
  shop_status: 'ACTIVE',
  kyc_status: 'VERIFIED',
  listing_mode: 'LOCAL_REPLICATION',
  authorization_status: 'ACTIVE',
  granted_scopes: [],
  missing_scopes: [],
  scope_captured_at: null,
  access_expires_at: null,
  quota: null,
  selectable: true,
  product_read_enabled: false,
  product_write_preconditions_met: false,
  order_read_enabled: false,
  product_read_blockers: [],
  product_write_blockers: [],
  order_read_blockers: [],
}

const capabilities: ToolsCapabilities = {
  translation_configured: true,
  translation_provider: 'AZURE_TRANSLATOR_V3',
  supported_translation_languages: ['zh-Hans', 'en', 'ms'],
  translation_cache_enabled: false,
  blockers: [],
}

const ElInputStub = defineComponent({
  props: {
    modelValue: { type: String, default: '' },
    type: { type: String, default: 'text' },
  },
  emits: ['update:modelValue'],
  setup(_props, { emit }) {
    const update = (event: Event): void => {
      emit('update:modelValue', (event.target as HTMLInputElement).value)
    }
    return { update }
  },
  template: `
    <textarea v-if="type === 'textarea'" :value="modelValue" @input="update" />
    <input v-else :value="modelValue" @input="update" />
  `,
})

const ElButtonStub = defineComponent({
  props: {
    disabled: Boolean,
    loading: Boolean,
    nativeType: { type: String, default: 'button' },
  },
  template: '<button :type="nativeType" :disabled="disabled"><slot /></button>',
})

const globalStubs = {
  PageHeader: true,
  CapabilityPanel: true,
  'el-alert': {
    props: ['title'],
    template: '<div role="alert">{{ title }}</div>',
  },
  'el-button': ElButtonStub,
  'el-empty': { template: '<div><slot /><slot name="image" /></div>' },
  'el-form': { template: '<form><slot /></form>' },
  'el-form-item': { template: '<label><slot /></label>' },
  'el-input': ElInputStub,
  'el-option': { template: '<option />' },
  'el-select': { template: '<select><slot /></select>' },
  'el-tag': { template: '<span><slot /></span>' },
}

let wrapper: VueWrapper | null = null

const prepareWritableShop = async (): Promise<void> => {
  vi.spyOn(coreApi, 'shops').mockResolvedValue([shopSummary])
  vi.spyOn(coreApi, 'login').mockResolvedValue({
    authenticated: true,
    csrf_token: CSRF_TOKEN,
    expires_at: '2026-08-07T12:00:00.000Z',
  })
  await useShopContext().refreshShops()
  await useAdminSession().login('test-bootstrap-secret')
}

const mountTools = async (): Promise<VueWrapper> => {
  wrapper = mount(ToolsView, {
    global: { stubs: globalStubs },
  })
  await flushPromises()
  return wrapper
}

afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  useAdminSession().expire()
  useShopContext().resetShops()
  vi.restoreAllMocks()
})

describe('ToolsView 翻译交互', () => {
  it('提交去空白后的多行文本并展示成功结果', async () => {
    vi.spyOn(coreApi, 'toolsCapabilities').mockResolvedValue(capabilities)
    const translate = vi.spyOn(coreApi, 'translate').mockResolvedValue({
      texts: ['Hello', 'Product title'],
      source_language: 'zh-Hans',
      target_language: 'ms',
      provider: 'AZURE_TRANSLATOR_V3',
      provider_request_id: 'provider-request-ui-1',
      cached: false,
    })
    const successMessage = vi.spyOn(ElMessage, 'success').mockReturnValue(
      { close: vi.fn() } as ReturnType<typeof ElMessage.success>,
    )
    await prepareWritableShop()
    const view = await mountTools()

    await view.get('textarea').setValue(' 你好\n\n 商品标题 ')
    await view.findAll('form')[0]!.trigger('submit')
    await flushPromises()

    expect(translate).toHaveBeenCalledWith(
      SHOP_ID,
      {
        texts: ['你好', '商品标题'],
        source_language: 'zh-Hans',
        target_language: 'ms',
      },
      CSRF_TOKEN,
    )
    expect(view.text()).toContain('Hello')
    expect(view.text()).toContain('Product title')
    expect(view.text()).toContain('provider-request-ui-1')
    expect(successMessage).toHaveBeenCalledTimes(1)
  })

  it('翻译失败时展示稳定错误且不渲染旧结果', async () => {
    vi.spyOn(coreApi, 'toolsCapabilities').mockResolvedValue(capabilities)
    vi.spyOn(coreApi, 'translate').mockRejectedValue(
      new ApiError(
        502,
        'translation provider is unavailable（TRANSLATION_UPSTREAM_FAILED）',
        {
          error: {
            code: 'TRANSLATION_UPSTREAM_FAILED',
            message: 'translation provider is unavailable',
            request_id: 'request-ui-failure',
          },
        },
      ),
    )
    vi.spyOn(ElMessage, 'success').mockReturnValue(
      { close: vi.fn() } as ReturnType<typeof ElMessage.success>,
    )
    await prepareWritableShop()
    const view = await mountTools()

    await view.get('textarea').setValue('需要翻译')
    await view.findAll('form')[0]!.trigger('submit')
    await flushPromises()

    expect(view.text()).toContain('TRANSLATION_UPSTREAM_FAILED')
    expect(view.text()).toContain('request-ui-failure')
    expect(view.find('.translation-results').exists()).toBe(false)
  })
})