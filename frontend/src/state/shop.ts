import { computed, readonly, ref } from 'vue'

import { coreApi, type ShopSummary } from '@/api/core'
import { errorText } from '@/ui'

const shops = ref<ShopSummary[]>([])
const shopBindingId = ref('')
const loading = ref(false)
const lastError = ref('')

const selectedShop = computed(
  () => shops.value.find((shop) => shop.id === shopBindingId.value) ?? null,
)

const selectShop = (value: string): boolean => {
  const selected = shops.value.find((shop) => shop.id === value && shop.selectable)
  if (!selected) return false
  shopBindingId.value = selected.id
  return true
}

const clearShop = (): void => {
  shopBindingId.value = ''
}

const refreshShops = async (): Promise<void> => {
  loading.value = true
  lastError.value = ''
  try {
    const loaded = await coreApi.shops()
    shops.value = loaded
    const retained = loaded.some(
      (shop) => shop.id === shopBindingId.value && shop.selectable,
    )
    if (!retained) {
      const selectable = loaded.filter((shop) => shop.selectable)
      shopBindingId.value = selectable.length === 1 ? selectable[0]?.id ?? '' : ''
    }
  } catch (error) {
    shops.value = []
    clearShop()
    lastError.value = errorText(error)
  } finally {
    loading.value = false
  }
}

const resetShops = (): void => {
  shops.value = []
  clearShop()
  lastError.value = ''
}

export const useShopContext = () => ({
  shops: readonly(shops),
  shopBindingId: readonly(shopBindingId),
  selectedShop,
  loading: readonly(loading),
  lastError: readonly(lastError),
  hasShop: computed(() => selectedShop.value !== null),
  selectShop,
  clearShop,
  refreshShops,
  resetShops,
})
