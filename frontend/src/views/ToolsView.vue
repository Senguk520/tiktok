<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { reactive, ref, watch } from 'vue'

import {
  coreApi,
  type ProfitResult,
  type ToolsCapabilities,
  type TranslationResult,
} from '@/api/core'
import CapabilityPanel from '@/components/CapabilityPanel.vue'
import PageHeader from '@/components/PageHeader.vue'
import ShopGate from '@/components/ShopGate.vue'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import { errorText } from '@/ui'

const shop = useShopContext()
const session = useAdminSession()
const capabilities = ref<ToolsCapabilities | null>(null)
const translation = ref<TranslationResult | null>(null)
const profit = ref<ProfitResult | null>(null)
const translationLoading = ref(false)
const profitLoading = ref(false)
const error = ref('')

const translationForm = reactive({
  sourceLanguage: 'zh-Hans',
  targetLanguage: 'ms',
  texts: '',
})

const profitForm = reactive({
  productCost: '100.00',
  sourceCurrency: 'CNY',
  settlementCurrency: 'MYR',
  exchangeRate: '0.65',
  shippingCost: '10.00',
  otherFixedCost: '0.00',
  commissionRate: '0.10',
  paymentFeeRate: '0.02',
  targetMarginRate: '0.20',
  sellingPrice: '',
})

const csrf = (): string => {
  if (!session.csrfToken.value) throw new Error('当前页面没有写入令牌，请重新认证')
  return session.csrfToken.value
}

const loadCapabilities = async (): Promise<void> => {
  capabilities.value = null
  if (!shop.shopBindingId.value) return
  error.value = ''
  try {
    capabilities.value = await coreApi.toolsCapabilities(shop.shopBindingId.value)
  } catch (reason) {
    error.value = errorText(reason)
  }
}

const translate = async (): Promise<void> => {
  const texts = translationForm.texts
    .split('\n')
    .map((text) => text.trim())
    .filter(Boolean)
  translationLoading.value = true
  translation.value = null
  error.value = ''
  try {
    translation.value = await coreApi.translate(
      shop.shopBindingId.value,
      {
        texts,
        source_language: translationForm.sourceLanguage,
        target_language: translationForm.targetLanguage,
      },
      csrf(),
    )
    ElMessage.success(`已翻译 ${translation.value.texts.length} 条文本；结果未写入缓存`)
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    translationLoading.value = false
  }
}

const calculateProfit = async (): Promise<void> => {
  profitLoading.value = true
  profit.value = null
  error.value = ''
  try {
    profit.value = await coreApi.profit(
      shop.shopBindingId.value,
      {
        product_cost: profitForm.productCost,
        source_currency: profitForm.sourceCurrency,
        settlement_currency: profitForm.settlementCurrency,
        exchange_rate: profitForm.exchangeRate,
        shipping_cost: profitForm.shippingCost,
        other_fixed_cost: profitForm.otherFixedCost,
        commission_rate: profitForm.commissionRate,
        payment_fee_rate: profitForm.paymentFeeRate,
        target_margin_rate: profitForm.targetMarginRate,
        selling_price: profitForm.sellingPrice || null,
      },
      csrf(),
    )
    ElMessage.success('利润估算完成')
  } catch (reason) {
    error.value = errorText(reason)
  } finally {
    profitLoading.value = false
  }
}

watch(shop.shopBindingId, () => {
  translation.value = null
  profit.value = null
  loadCapabilities()
}, { immediate: true })
</script>

<template>
  <section>
    <PageHeader title="运营工具" description="翻译正文只经过后端提供商边界；利润计算使用明确币种和 Decimal 舍入规则。" />

    <ShopGate>
      <el-alert v-if="error" class="section-alert" :title="error" type="error" :closable="false" show-icon />
      <div class="two-column-grid tools-grid">
        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">TRANSLATION</p>
              <h2>受控文本翻译</h2>
            </div>
            <el-tag :type="capabilities?.translation_configured ? 'success' : 'warning'">
              {{ capabilities?.translation_configured ? capabilities.translation_provider : '未配置' }}
            </el-tag>
          </div>

          <CapabilityPanel
            v-if="capabilities"
            :blockers="capabilities.blockers"
            ready-text="Azure Translator v3 已配置"
          />
          <el-alert
            class="section-alert"
            title="翻译结果不缓存；相同幂等键不会重放正文。每次最多 25 条、合计 5,000 字符。"
            type="info"
            :closable="false"
          />
          <el-form class="spaced-form" label-position="top" @submit.prevent="translate">
            <div class="form-grid">
              <el-form-item label="源语言">
                <el-select v-model="translationForm.sourceLanguage">
                  <el-option
                    v-for="language in capabilities?.supported_translation_languages ?? ['zh-Hans', 'en', 'ms']"
                    :key="language"
                    :label="language"
                    :value="language"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="目标语言">
                <el-select v-model="translationForm.targetLanguage">
                  <el-option
                    v-for="language in capabilities?.supported_translation_languages ?? ['zh-Hans', 'en', 'ms']"
                    :key="language"
                    :label="language"
                    :value="language"
                  />
                </el-select>
              </el-form-item>
              <el-form-item label="待翻译文本（每行一条）" class="span-2">
                <el-input v-model="translationForm.texts" type="textarea" :rows="8" maxlength="5000" show-word-limit />
              </el-form-item>
            </div>
            <el-button
              type="primary"
              native-type="submit"
              :loading="translationLoading"
              :disabled="!session.canWrite.value || !capabilities?.translation_configured || !translationForm.texts.trim()"
            >
              发送翻译
            </el-button>
          </el-form>
          <div v-if="translation" class="translation-results">
            <article v-for="(text, index) in translation.texts" :key="index">
              <span>{{ index + 1 }}</span>
              <p>{{ text }}</p>
            </article>
            <small>Provider Request ID: {{ translation.provider_request_id ?? '—' }}</small>
          </div>
        </section>

        <section class="content-card">
          <div class="card-heading">
            <div>
              <p class="page-kicker">DECIMAL PROFIT</p>
              <h2>利润与定价估算</h2>
            </div>
          </div>
          <el-form label-position="top" @submit.prevent="calculateProfit">
            <div class="form-grid">
              <el-form-item label="商品成本">
                <el-input v-model="profitForm.productCost" />
              </el-form-item>
              <el-form-item label="成本币种">
                <el-input v-model="profitForm.sourceCurrency" maxlength="3" />
              </el-form-item>
              <el-form-item label="结算币种">
                <el-input v-model="profitForm.settlementCurrency" maxlength="3" />
              </el-form-item>
              <el-form-item label="汇率（成本币种 → 结算币种）">
                <el-input v-model="profitForm.exchangeRate" />
              </el-form-item>
              <el-form-item label="运费">
                <el-input v-model="profitForm.shippingCost" />
              </el-form-item>
              <el-form-item label="其他固定成本">
                <el-input v-model="profitForm.otherFixedCost" />
              </el-form-item>
              <el-form-item label="平台佣金率（0-1）">
                <el-input v-model="profitForm.commissionRate" />
              </el-form-item>
              <el-form-item label="支付费率（0-1）">
                <el-input v-model="profitForm.paymentFeeRate" />
              </el-form-item>
              <el-form-item label="目标利润率（0-1）">
                <el-input v-model="profitForm.targetMarginRate" />
              </el-form-item>
              <el-form-item label="指定售价（留空则反推建议价）">
                <el-input v-model="profitForm.sellingPrice" clearable />
              </el-form-item>
            </div>
            <el-button
              type="primary"
              native-type="submit"
              :loading="profitLoading"
              :disabled="!session.canWrite.value"
            >
              计算利润
            </el-button>
          </el-form>

          <div v-if="profit" class="profit-result">
            <div>
              <span>建议 / 指定售价</span>
              <strong>{{ profit.suggested_price }} {{ profit.settlement_currency }}</strong>
            </div>
            <div>
              <span>预计利润</span>
              <strong>{{ profit.estimated_profit }} {{ profit.settlement_currency }}</strong>
            </div>
            <div>
              <span>实际利润率</span>
              <strong>{{ profit.realized_margin_percent }}%</strong>
            </div>
            <dl>
              <dt>折算商品成本</dt><dd>{{ profit.converted_product_cost }}</dd>
              <dt>总固定成本</dt><dd>{{ profit.total_fixed_cost }}</dd>
              <dt>佣金</dt><dd>{{ profit.commission_amount }}</dd>
              <dt>支付费</dt><dd>{{ profit.payment_fee_amount }}</dd>
            </dl>
          </div>
        </section>
      </div>
    </ShopGate>
  </section>
</template>