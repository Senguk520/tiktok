<script setup lang="ts">
import { ElMessage } from 'element-plus'
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'

import type { ShopSummary } from '@/api/core'
import { useAdminSession } from '@/state/session'
import { useShopContext } from '@/state/shop'
import { formatDateTime } from '@/ui'

const route = useRoute()
const router = useRouter()
const session = useAdminSession()
const shop = useShopContext()
const shopInput = ref(shop.shopBindingId.value)
const mobileNavigation = ref(false)

const navigation = [
  { path: '/overview', label: '总览', mark: '01' },
  { path: '/miaoshou', label: '妙手店铺', mark: '02' },
  { path: '/products', label: '商品', mark: '03' },
  { path: '/orders', label: '订单', mark: '04' },
  { path: '/tools', label: '运营工具', mark: '05' },
  { path: '/schedules', label: '自动调度', mark: '06' },
  { path: '/audits', label: '审计记录', mark: '07' },
]

const activePath = computed(() => route.path)

const optionLabel = (
  item: Readonly<Pick<ShopSummary, 'shop_code' | 'shop_id' | 'region' | 'listing_mode'>>,
): string => `${item.shop_code || item.shop_id} · ${item.region} · ${item.listing_mode}`

const applyShop = (): void => {
  if (!shopInput.value) {
    clearShop()
    return
  }
  if (!shop.selectShop(shopInput.value)) {
    ElMessage.error('只能选择注册表中授权可用的本人店铺')
    return
  }
  shopInput.value = shop.shopBindingId.value
  ElMessage.success('工作区店铺已切换；选择状态仅保存在当前页面内存')
}

const clearShop = (): void => {
  shop.clearShop()
  shopInput.value = ''
}

const refreshShops = async (): Promise<void> => {
  await shop.refreshShops()
  shopInput.value = shop.shopBindingId.value
}

watch(shop.shopBindingId, (value) => {
  shopInput.value = value
})

onMounted(refreshShops)

const navigate = async (path: string): Promise<void> => {
  mobileNavigation.value = false
  await router.push(path)
}

const reauthenticate = async (): Promise<void> => {
  await router.push({ path: '/login', query: { reauth: '1' } })
}

const logout = async (): Promise<void> => {
  try {
    await session.logout()
    shop.resetShops()
    shopInput.value = ''
    await router.replace('/login')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '退出失败')
  }
}
</script>

<template>
  <div class="workspace-shell">
    <aside class="workspace-sidebar" :class="{ open: mobileNavigation }">
      <div class="sidebar-brand">
        <span class="brand-symbol">TT</span>
        <div>
          <strong>Single Store</strong>
          <small>Operations Console</small>
        </div>
      </div>

      <nav class="workspace-nav" aria-label="主导航">
        <button
          v-for="item in navigation"
          :key="item.path"
          type="button"
          :class="{ active: activePath === item.path }"
          @click="navigate(item.path)"
        >
          <span>{{ item.mark }}</span>
          {{ item.label }}
        </button>
      </nav>

      <div class="sidebar-security">
        <span class="security-dot" />
        <div>
          <strong>{{ session.canWrite.value ? '写入会话可用' : '只读会话' }}</strong>
          <small>{{ formatDateTime(session.expiresAt.value) }}</small>
        </div>
      </div>
    </aside>

    <section class="workspace-main">
      <header class="workspace-topbar">
        <button class="mobile-menu" type="button" @click="mobileNavigation = !mobileNavigation">
          菜单
        </button>
        <div class="shop-selector">
          <label for="shop-binding">本人店铺</label>
          <el-select
            id="shop-binding"
            v-model="shopInput"
            :loading="shop.loading.value"
            placeholder="选择已注册店铺"
            filterable
            clearable
            @change="applyShop"
            @clear="clearShop"
          >
            <el-option
              v-for="item in shop.shops.value"
              :key="item.id"
              :label="optionLabel(item)"
              :value="item.id"
              :disabled="!item.selectable"
            >
              <div class="shop-option">
                <span>{{ item.shop_code || item.shop_id }}</span>
                <small>{{ item.region }} · {{ item.authorization_status }} · {{ item.listing_mode }}</small>
              </div>
            </el-option>
          </el-select>
          <el-button :loading="shop.loading.value" @click="refreshShops">刷新</el-button>
        </div>
        <div class="session-actions">
          <el-button v-if="!session.canWrite.value" type="warning" plain @click="reauthenticate">
            重新认证写入
          </el-button>
          <el-button v-else text @click="logout">退出</el-button>
        </div>
      </header>

      <el-alert
        v-if="shop.lastError.value"
        class="readonly-alert"
        :title="shop.lastError.value"
        type="error"
        :closable="false"
        show-icon
      />

      <div v-if="shop.selectedShop.value" class="shop-context-bar">
        <strong>{{ shop.selectedShop.value.shop_code || shop.selectedShop.value.shop_id }}</strong>
        <el-tag size="small" effect="plain">{{ shop.selectedShop.value.region }}</el-tag>
        <el-tag
          size="small"
          :type="shop.selectedShop.value.authorization_status === 'ACTIVE' ? 'success' : 'danger'"
        >
          授权 {{ shop.selectedShop.value.authorization_status }}
        </el-tag>
        <el-tag size="small" type="info">刊登 {{ shop.selectedShop.value.listing_mode }}</el-tag>
        <span>店铺 {{ shop.selectedShop.value.shop_status }} · KYC {{ shop.selectedShop.value.kyc_status }}</span>
      </div>

      <el-alert
        v-if="!session.canWrite.value"
        class="readonly-alert"
        title="页面刷新后 CSRF 令牌不会被浏览器持久化；当前会话只允许读取。重新认证后可恢复写操作。"
        type="warning"
        :closable="false"
        show-icon
      />

      <main class="workspace-content">
        <router-view />
      </main>
    </section>
  </div>
</template>