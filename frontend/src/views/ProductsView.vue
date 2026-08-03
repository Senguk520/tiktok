<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, reactive, ref, watch } from 'vue'

import {
  coreApi,
  type ProductCapabilities,
  type ProductDraft,
  type ProductDraftInput,
  type ProductSubmission,
  type QuotaSnapshot,
  type RemoteProduct,
} from '@/api/core'
import CapabilityPanel from '@/components/CapabilityPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import { commaSeparated, errorText, formatDateTime } from '@/ui'

interface SkuEditor {
  sellerSku: string
  price: string
  currency: string
  inventory: string
  attributes: string
}

const shop = useShopContext()
const session = useAdminSession()
const capabilities = ref<ProductCapabilities | null>(null)
const currentDraft = ref<ProductDraft | null>(null)
const draftId = ref('')
const submission = ref<ProductSubmission | null>(null)
const quota = ref<QuotaSnapshot | null>(null)
const remoteProducts = ref<RemoteProduct[]>([])
const remoteMode = ref('')
const remoteNextToken = ref<string | null>(null)
const loading = ref(false)
const remoteLoading = ref(false)
const error = ref('')
const activeTab = ref('draft')

const readBlockers = computed(() => [
  ...(capabilities.value?.blockers.filter(
    (blocker) => blocker === 'BLOCKED_LIVE_CREDENTIALS' || blocker === 'BLOCKED_MASTER_KEY',
  ) ?? []),
  ...(shop.selectedShop.value?.product_read_blockers ?? []),
])
const writeBlockers = computed(() => [
  ...(capabilities.value?.blockers ?? []),
  ...(shop.selectedShop.value?.product_write_blockers ?? []),
])
const remoteReadEnabled = computed(
  () => Boolean(capabilities.value) && readBlockers.value.length === 0,
)
const submissionEnabled = computed(
  () => Boolean(capabilities.value) && writeBlockers.value.length === 0,
)
const commerceContextEnabled = computed(
  () =>
    Boolean(capabilities.value?.platform_configured) &&
    Boolean(capabilities.value?.master_key_configured) &&
    Boolean(shop.selectedShop.value?.selectable),
)
const quotaConfirmationEnabled = computed(
  () => commerceContextEnabled.value && shop.selectedShop.value?.region === 'MY',
)

const draftForm = reactive({
  title: '',
  description: '',
  categoryId: '',
  images: '',
  attributes: '{}',
  warnings: '',
  skus: [
    {
      sellerSku: '',
      price: '0.00',
      currency: 'MYR',
      inventory: '{"warehouse-id": 0}',
      attributes: '{}',
    },
  ] as SkuEditor[],
})

const now = new Date()
const later = new Date(now.getTime() + 60 * 60 * 1000)
const quotaForm = reactive({
  listingLimit: null as number | null,
  submittedCount: 0,
  confirmedAt: now.toISOString().slice(0, 16),
  expiresAt: later.toISOString().slice(0, 16),
  stage: '',
})

const csrf = (): string => {
  if (!session.csrfToken.value) throw new Error('当前页面没有写入令牌，请重新认证')
  return session.csrfToken.value
}

const objectJson = (value: string, field: string): Record<string, string> => {
  const parsed: unknown = JSON.parse(value || '{}')
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error(`${field} 必须是 JSON 对象`)
  }
  const entries = Object.entries(parsed)
  if (entries.some(([, item]) => typeof item !== 'string')) {
    throw new Error(`${field} 的值必须是字符串`)
  }
  return Object.fromEntries(entries) as Record<string, string>
}

const inventoryJson = (value: string): Record<string, number> => {
  const parsed: unknown = JSON.parse(value)
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    throw new Error('仓库库存必须是 JSON 对象')
  }
  const entries = Object.entries(parsed)
  if (
    entries.some(
      ([warehouse, quantity]) =>
        !warehouse.trim() ||
        typeof quantity !== 'number' ||
        !Number.isInteger(quantity) ||
        quantity < 0,
    )
  ) {
    throw new Error('仓库库存必须使用非空仓库 ID 和非负整数')
  }
  return Object.fromEntries(entries) as Record<string, number>
}

const draftPayload = (): ProductDraftInput => ({
  title: draftForm.title.trim(),
  description: draftForm.description,
  category_id: draftForm.categoryId.trim() || null,
  skus: draftForm.skus.map((sku) => ({
    seller_sku: sku.sellerSku.trim(),
    price: sku.price,
    currency: sku.currency.trim().toUpperCase(),
    inventory_by_warehouse: inventoryJson(sku.inventory),
    attributes: objectJson(sku.attributes, 'SKU 属性'),
  })),
  images: commaSeparated(draftForm.images).map((sourceRef, index) => ({
    source_ref: sourceRef,
    role: index === 0 ? 'MAIN' : 'DETAIL',
  })),
  attributes: objectJson(draftForm.attributes, '商品属性'),
  unmapped_warnings: commaSeparated(draftForm.warnings),
})

const loadCapabilities = async (): Promise<void> => {
  capabilities.value = null
  if (!shop.shopBindingId.value) return
  try {
    capabilities.value = await coreApi.productCapabilities(shop.shopBindingId.value)
  } catch (reason) {
    error.value = errorText(reason)
  }
}

const createDraft = async (): Promise<void> => {
  loading.value = true
  error.value = ''
  submission.value = null
  try {
    currentDraft.value = await coreApi.createDraft(
      shop.shopBindingId.value,
      draftPayload(),
      csrf(),
    )
    draftId.value = currentDraft.value.id
    ElMessage.success(currentDraft.value.created ? '商品草稿已创建' : '已返回相同内容的现有草稿')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const confirmDraft = async (): Promise<void> => {
  if (!draftId.value) return
  loading.value = true
  error.value = ''
  try {
    currentDraft.value = await coreApi.confirmDraft(shop.shopBindingId.value, draftId.value, csrf())
    ElMessage.success('草稿已人工确认')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const submitDraft = async (): Promise<void> => {
  if (!draftId.value) return
  loading.value = true
  error.value = ''
  try {
    submission.value = await coreApi.submitDraft(shop.shopBindingId.value, draftId.value, csrf())
    ElMessage.success(submission.value.replayed ? '返回已有刊登结果' : '刊登请求已提交')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const confirmQuota = async (): Promise<void> => {
  loading.value = true
  error.value = ''
  try {
    quota.value = await coreApi.confirmQuota(
      shop.shopBindingId.value,
      {
        listing_limit: quotaForm.listingLimit,
        locally_submitted_count: quotaForm.submittedCount,
        confirmed_at: new Date(quotaForm.confirmedAt).toISOString(),
        expires_at: new Date(quotaForm.expiresAt).toISOString(),
        stage: quotaForm.stage.trim() || null,
      },
      csrf(),
    )
    await shop.refreshShops()
    ElMessage.success('Seller Center 配额快照已确认')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    loading.value = false
  }
}

const loadRemote = async (pageToken?: string | null): Promise<void> => {
  remoteLoading.value = true
  error.value = ''
  try {
    const page = await coreApi.remoteProducts(shop.shopBindingId.value, 20, pageToken)
    remoteProducts.value = page.items
    remoteMode.value = page.mode
    remoteNextToken.value = page.next_page_token
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    remoteLoading.value = false
  }
}

const addSku = (): void => {
  draftForm.skus.push({
    sellerSku: '',
    price: '0.00',
    currency: 'MYR',
    inventory: '{"warehouse-id": 0}',
    attributes: '{}',
  })
}

const removeSku = (index: number): void => {
  if (draftForm.skus.length > 1) draftForm.skus.splice(index, 1)
}

onMounted(loadCapabilities)
watch(shop.shopBindingId, () => {
  currentDraft.value = null
  draftId.value = ''
  submission.value = null
  quota.value = null
  remoteProducts.value = []
  remoteMode.value = ''
  remoteNextToken.value = null
  error.value = ''
  loadCapabilities()
})
</script>

<template>
  <section>
    <PageHeader title="商品工作台" description="草稿、人工确认、配额确认和刊登是四个独立步骤；不会在失败时切换另一条刊登链路。">
      <el-tag v-if="shop.selectedShop.value" effect="plain">
        {{ shop.selectedShop.value.listing_mode }}
      </el-tag>
      <el-tag v-if="remoteMode" type="info" effect="plain">远端响应 {{ remoteMode }}</el-tag>
    </PageHeader>

    <ShopGate>
      <CapabilityPanel
        v-if="capabilities"
        :blockers="writeBlockers"
        ready-text="当前店铺商品写入前置条件已就绪"
      />
      <el-alert v-if="error" class="section-alert" :title="error" type="error" :closable="false" show-icon />

      <el-tabs v-model="activeTab" class="work-tabs">
        <el-tab-pane label="草稿与刊登" name="draft">
          <div class="two-column-grid">
            <section class="content-card">
              <div class="card-heading">
                <div>
                  <p class="page-kicker">NORMALIZED INTENT</p>
                  <h2>商品意图</h2>
                </div>
                <el-button text @click="addSku">新增 SKU</el-button>
              </div>

              <el-form label-position="top" @submit.prevent="createDraft">
                <div class="form-grid">
                  <el-form-item label="标题" class="span-2">
                    <el-input v-model="draftForm.title" maxlength="255" show-word-limit />
                  </el-form-item>
                  <el-form-item label="类目 ID">
                    <el-input v-model="draftForm.categoryId" placeholder="平台类目 ID" />
                  </el-form-item>
                  <el-form-item label="图片来源（每行一个受控引用）">
                    <el-input v-model="draftForm.images" type="textarea" :rows="2" />
                  </el-form-item>
                  <el-form-item label="描述" class="span-2">
                    <el-input v-model="draftForm.description" type="textarea" :rows="4" maxlength="20000" />
                  </el-form-item>
                  <el-form-item label="商品属性 JSON">
                    <el-input v-model="draftForm.attributes" type="textarea" :rows="3" />
                  </el-form-item>
                  <el-form-item label="未映射警告（逗号或换行分隔）">
                    <el-input v-model="draftForm.warnings" type="textarea" :rows="3" />
                  </el-form-item>
                </div>

                <article v-for="(sku, index) in draftForm.skus" :key="index" class="sku-editor">
                  <div class="sku-heading">
                    <strong>SKU {{ index + 1 }}</strong>
                    <el-button v-if="draftForm.skus.length > 1" text type="danger" @click="removeSku(index)">
                      移除
                    </el-button>
                  </div>
                  <div class="form-grid">
                    <el-form-item label="Seller SKU">
                      <el-input v-model="sku.sellerSku" maxlength="128" />
                    </el-form-item>
                    <el-form-item label="价格 / 币种">
                      <el-input v-model="sku.price">
                        <template #append>
                          <el-input v-model="sku.currency" class="currency-input" maxlength="3" />
                        </template>
                      </el-input>
                    </el-form-item>
                    <el-form-item label="仓库库存 JSON">
                      <el-input v-model="sku.inventory" type="textarea" :rows="2" />
                    </el-form-item>
                    <el-form-item label="SKU 属性 JSON">
                      <el-input v-model="sku.attributes" type="textarea" :rows="2" />
                    </el-form-item>
                  </div>
                </article>

                <el-button
                  type="primary"
                  native-type="submit"
                  :loading="loading"
                  :disabled="!session.canWrite.value || !commerceContextEnabled || !draftForm.title || !draftForm.skus[0]?.sellerSku"
                >
                  保存规范化草稿
                </el-button>
              </el-form>
            </section>

            <section class="content-card sticky-card">
              <div class="card-heading">
                <div>
                  <p class="page-kicker">WRITE PIPELINE</p>
                  <h2>确认与刊登</h2>
                </div>
              </div>
              <el-form label-position="top">
                <el-form-item label="草稿 UUID">
                  <el-input v-model="draftId" maxlength="36" placeholder="创建后自动填入，也可手动输入" />
                </el-form-item>
              </el-form>
              <div class="pipeline-steps">
                <div :class="{ done: Boolean(currentDraft) }"><span>1</span>草稿已保存</div>
                <div :class="{ done: currentDraft?.human_confirmed }"><span>2</span>人工确认</div>
                <div :class="{ done: Boolean(quota) }"><span>3</span>配额已确认</div>
                <div :class="{ done: Boolean(submission) }"><span>4</span>提交平台</div>
              </div>
              <div class="button-stack">
                <el-button :disabled="!session.canWrite.value || !commerceContextEnabled || !draftId" @click="confirmDraft">确认草稿</el-button>
                <el-button
                  type="primary"
                  :loading="loading"
                  :disabled="!session.canWrite.value || !draftId || !submissionEnabled"
                  @click="submitDraft"
                >
                  提交刊登
                </el-button>
              </div>
              <el-descriptions v-if="submission" class="result-panel" :column="1" border>
                <el-descriptions-item label="模式">{{ submission.mode }}</el-descriptions-item>
                <el-descriptions-item label="Product ID">{{ submission.product_id }}</el-descriptions-item>
                <el-descriptions-item label="Operation ID">{{ submission.operation_id }}</el-descriptions-item>
                <el-descriptions-item label="Request ID">{{ submission.request_id ?? '—' }}</el-descriptions-item>
              </el-descriptions>
            </section>
          </div>
        </el-tab-pane>

        <el-tab-pane label="配额确认" name="quota">
          <section class="content-card narrow-card">
            <div class="card-heading">
              <div>
                <p class="page-kicker">SELLER CENTER FACT</p>
                <h2>人工确认实时配额</h2>
              </div>
            </div>
            <el-alert
              title="Core API 不会猜测 Seller Center 的实时刊登额度。请按目标店铺实际页面填写。"
              type="info"
              :closable="false"
              show-icon
            />
            <el-form class="spaced-form" label-position="top" @submit.prevent="confirmQuota">
              <div class="form-grid">
                <el-form-item label="刊登上限（未知可留空）">
                  <el-input-number v-model="quotaForm.listingLimit" :min="0" controls-position="right" />
                </el-form-item>
                <el-form-item label="本地已提交数量">
                  <el-input-number v-model="quotaForm.submittedCount" :min="0" controls-position="right" />
                </el-form-item>
                <el-form-item label="确认时间">
                  <el-input v-model="quotaForm.confirmedAt" type="datetime-local" />
                </el-form-item>
                <el-form-item label="过期时间">
                  <el-input v-model="quotaForm.expiresAt" type="datetime-local" />
                </el-form-item>
                <el-form-item label="店铺阶段">
                  <el-input v-model="quotaForm.stage" placeholder="BEGINNER / STANDARD / PREMIUM / PRO" />
                </el-form-item>
              </div>
              <el-button type="primary" native-type="submit" :loading="loading" :disabled="!session.canWrite.value || !quotaConfirmationEnabled">
                保存配额快照
              </el-button>
            </el-form>
            <el-descriptions v-if="quota" class="result-panel" :column="2" border>
              <el-descriptions-item label="区域">{{ quota.region }}</el-descriptions-item>
              <el-descriptions-item label="上限">{{ quota.listing_limit ?? '未知' }}</el-descriptions-item>
              <el-descriptions-item label="已提交">{{ quota.locally_submitted_count }}</el-descriptions-item>
              <el-descriptions-item label="有效至">{{ formatDateTime(quota.expires_at) }}</el-descriptions-item>
            </el-descriptions>
          </section>
        </el-tab-pane>

        <el-tab-pane label="平台商品" name="remote">
          <section class="content-card">
            <div class="card-heading">
              <div>
                <p class="page-kicker">LIVE CATALOG</p>
                <h2>远端商品</h2>
              </div>
              <el-button :loading="remoteLoading" :disabled="!remoteReadEnabled" @click="loadRemote(null)">
                查询第一页
              </el-button>
            </div>
            <CapabilityPanel
              v-if="capabilities && readBlockers.length"
              class="section-alert"
              :blockers="readBlockers"
            />
            <el-table v-loading="remoteLoading" :data="remoteProducts" empty-text="暂无远端商品或尚未查询">
              <el-table-column prop="product_id" label="Product ID" min-width="180" />
              <el-table-column prop="title" label="标题" min-width="240" />
              <el-table-column prop="status" label="状态" width="150" />
              <el-table-column label="Seller SKU" min-width="220">
                <template #default="scope">{{ scope.row.seller_skus.join(', ') || '—' }}</template>
              </el-table-column>
            </el-table>
            <div class="table-footer">
              <span>{{ remoteProducts.length }} 条 · {{ remoteMode || '模式未返回' }}</span>
              <el-button
                v-if="remoteNextToken"
                :loading="remoteLoading"
                @click="loadRemote(remoteNextToken)"
              >
                下一页
              </el-button>
            </div>
          </section>
        </el-tab-pane>
      </el-tabs>
    </ShopGate>
  </section>
</template>