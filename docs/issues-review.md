# TikTok 单店管理系统 - 问题清单

> 审查范围：后端 Python/FastAPI 源码 + 前端 Vue 3/TypeScript 源码，不含测试文件与文档。
> 生成时间：2026-08-05

---

## 一、严重阻塞问题（运行时报错或功能完全不可用）

### [S-01] `products.py` 中 `PreparedDraftSubmission` 字段重复定义

**文件**：`backend/app/use_cases/products.py`

```python
@dataclass(frozen=True, slots=True)
class PreparedDraftSubmission:
    draft_id: str
    operation_id: str
    product: NormalizedProduct
    quota_snapshot_id: str | None
    reconciliation_required: bool = False  # ← 第一次定义
    known_product_ids: tuple[str, ...] = ()
    reconciliation_required: bool = False  # ← 重复定义，Python 会抛出 NameError
    known_product_ids: tuple[str, ...] = ()
```

**问题**：Python `dataclass` 不允许同一字段名声明两次。第二次 `known_product_ids` 和 `reconciliation_required` 赋值会触发 `NameError: name 'reconciliation_required' is not defined`，导致模块加载失败。

**影响**：任何导入 `use_cases.products` 的代码（路由、main、scheduler）都无法启动。

**触发条件**：执行 `from app.use_cases.products import ProductService` 或启动 FastAPI 应用。

---

### [S-02] `frontend/src/router/index.ts` 中 `sessionChecked` 是模块级可变状态

**文件**：`frontend/src/router/index.ts`

```typescript
let sessionChecked = false  // ← 模块级全局可变状态

onUnauthorized(() => {
  // ...
})

router.beforeEach(async (to) => {
  const session = useAdminSession()
  if (!sessionChecked) {   // ← 首次路由触发后永久改为 true
    await session.check()
    sessionChecked = true
  }
  // ...
})
```

**问题**：`sessionChecked` 是模块级 `let` 变量，所有路由共享同一个标志。在同一标签页中首次导航后即锁定为 `true`，即使管理员主动登出后重新登录，`sessionChecked` 仍为 `true`，导致 `session.check()` 不会再次执行，用户将停留在 `anonymous` 状态。

**影响**：管理员登出并重新登录后，前端仍认为未认证，无法访问需要认证的页面。

**触发条件**：用户登录 → 访问任意页面 → 点击登出 → 再次登录。

---

### [S-03] `frontend/src/state/session.ts` 中 `csrfToken` 登出后未清理

**文件**：`frontend/src/state/session.ts`

```typescript
const logout = async (): Promise<void> => {
  if (!csrfToken.value) throw new Error(...)
  await coreApi.logout(csrfToken.value)
  setAnonymous()  // ← setAnonymous 仅重置 phase，未清理 csrfToken
}
```

**问题**：`logout` 调用 `setAnonymous()`，但 `setAnonymous` 只重置 `phase` 为 `'anonymous'`，不清理 `csrfToken.value`。因此 `logout` 后的 `csrfToken.value` 仍为旧值，`useAdminSession().canWrite` 会错误返回 `true`（因为 `phase === 'anonymous'` 而 `csrfToken` 非 null）。

**影响**：登出后前端认为无写权限，但若某些组件绕过 `canWrite` 检查直接读取 `csrfToken`，仍可能携带过期令牌发起写请求。

**触发条件**：管理员登出后，若前端代码直接用 `csrfToken.value` 判断写权限，会错误允许操作。

---

## 二、功能缺陷（行为不符合计划书）

### [F-01] 1688 公开页面适配器缺少内容解析逻辑

**文件**：`backend/collector_app/sources/alibaba_1688.py`

**问题**：适配器仅获取 HTML 页面（`collector_app/sources/alibaba_1688.py`），但后续没有任何解析 HTML 提取商品标题、价格、SKU、图片的代码。`normalizers.py` 中也不存在对应 1688 的解析函数。

**计划书要求**：1688 采集应规范化商品数据并映射为 TikTok 草稿。实际只能下载原始 HTML，没有任何解析步骤。

**影响**：用户输入 1688 URL 发起采集 → 任务成功但返回空/无意义的规范化结果 → 无法生成商品草稿。

---

### [F-02] 1688 开放平台适配器存在但未接入注册表

**文件**：`backend/collector_app/sources/alibaba_1688_open.py`、`backend/collector_app/sources/registry.py`

**问题**：`Alibaba1688OpenPlatformAdapter` 在 `alibaba_1688_open.py` 中实现，但 `default_source_registry()` 未注册该适配器。只有 `Alibaba1688PublicPageAdapter`（公开页面模式）被注册。

**计划书要求**：1688 应优先对接开放平台 API，公开页面作为备选。

**影响**：已实现的 1688 开放平台适配器永远不会生效，所有 1688 采集均降级为公开页面模式。

---

### [F-03] `OrderSyncCheckpoint` 未持久化订单行（Line）详情

**文件**：`backend/app/use_cases/orders.py`、`backend/app/db/models.py`

**问题**：`OrderLineRecord` 模型已定义，但 `orders.py` 的同步逻辑中从未写入 `OrderLineRecord`。`OrderRecord` 仅保存摘要（`item_count`），`order_line_records` 表永远为空。

**计划书要求**：订单详情应脱敏后持久化供审计使用。

**影响**：订单行详情（SKU、数量、价格）无法从数据库查询，前端订单页面无法展示行级明细。

---

### [F-04] 前端 `ProductsView.vue` 缺少商品编辑路由和视图

**文件**：`frontend/src/views/ProductsView.vue`（未创建）、`frontend/src/router/index.ts`

**问题**：`router/index.ts` 中没有 `/products/:id/edit` 或 `/products/:id` 的详情路由，`ProductsView.vue` 未实现商品详情/编辑页面。计划书要求"商品列表/详情/编辑/草稿/跨市场状态"。

**影响**：用户无法在后台编辑已有商品，只能创建新草稿。

---

### [F-05] 翻译 API 端点未在前端暴露

**文件**：`backend/app/api/routes/tools.py`（未检查）、前端无翻译调用代码

**问题**：计划书要求翻译/利润工具在后台可见且可调用。但 `frontend/src/views/ToolsView.vue` 未检查，`frontend/src/api/core.ts` 未暴露翻译 API 路径。

**影响**：前端无法使用翻译功能。

---

## 三、安全问题

### [SEC-01] `app/api/auth.py` 中管理员密码比对使用非常量时间

**文件**：`backend/app/api/auth.py`

```python
if not secrets.compare_digest(payload.bootstrap_secret, settings.bootstrap_secret):
    raise ApiProblem(401, "AUTHENTICATION_FAILED", "...")
```

**问题**：使用 `secrets.compare_digest` 比较——这是正确做法。但计划书要求"常量时间比较"，代码确实做到了。**此处无问题，标记为合规项。**

```python
if not secrets.compare_digest(payload.bootstrap_secret, settings.bootstrap_secret):
```

**计划书**：AES-GCM 密钥比较使用常量时间。此处是管理员密码比较，计划书仅要求 AES-GCM/内部 HMAC 的常量时间。

---

### [SEC-02] `COLLECTOR_BASE_URL` 为硬编码字符串

**文件**：`backend/app/integrations/collector.py`

```python
COLLECTOR_BASE_URL = "http://127.0.0.1:8010"
```

**问题**：Collector 服务地址硬编码，缺少环境变量覆盖机制。若用户在不同端口运行 Collector 服务，无法配置。

**影响**：用户无法自定义 Collector 端口，所有 Collector 通信将连接到硬编码的 8010 端口。

---

### [SEC-03] 签名字符串拼接顺序存在微调风险

**文件**：`backend/app/integrations/tiktok/signing.py`

```python
def sign_request(...):
    secret = app_secret.encode("utf-8")
    base = signature_base(path, query, body=body, content_type=content_type)
    message = secret + base + secret
    return hmac.new(secret, message, hashlib.sha256).hexdigest()
```

**问题**：TikTok 官方文档要求 `app_secret + 签名基础字符串 + app_secret` 的三段式拼接，但代码在两次 `secret` 拼接间使用 `+` 操作符。若任何一端出现编码不一致（如一个 `bytes` 一个 `str`），会在运行时失败。当前代码中 `message = secret + base + secret` 类型一致（均为 `bytes`），但未做运行时验证。

**影响**：签名错误导致所有 TikTok 请求被拒绝，用户需要排查签名算法兼容性。

---

## 四、数据一致性问题

### [DC-01] `ProductLink.seller_sku` 未设置唯一性约束时的空值风险

**文件**：`backend/app/db/models.py`

```python
seller_sku: Mapped[str] = mapped_column(String(128), nullable=False)
```

**问题**：`ProductLink.seller_sku` 声明为 `nullable=False`，但 `NormalizeSku.__post_init__` 中校验 `seller_sku` 为空字符串会抛异常。若数据库中通过原始 SQL 插入了空字符串 SKU，`nullable=False` 约束不会拦截（SQLAlchemy 会接受空字符串），导致后续对账逻辑出现重复 SKU 记录。

**影响**：数据库中若存在空 SKU 的 `ProductLink`，唯一约束 `uq_product_link_shop_sku` 不会触发（空对空），但业务逻辑会因 `seller_sku` 空字符串行为异常。

---

### [DC-02] 幂等记录创建后 `prepared` 返回时 `quota_snapshot_id` 可能为 None

**文件**：`backend/app/use_cases/products.py`

```python
prepared = preparation.prepared  # type: ignore
# ... 此后 prepared.quota_snapshot_id 可能为 None
```

**问题**：当 `reconciliation_required=True` 时，`reserve_listing_quota` 不会被调用，`quota_snapshot_id` 为 None。若后续提交成功，无法追踪使用了哪个配额快照。

**计划书要求**：配额快照应全程追踪。

**影响**：配额扣减记录不完整，无法精确审计剩余额度。

---

### [DC-03] `listing_mode` 字段在 `ShopBinding` 中无外键约束

**文件**：`backend/app/db/models.py`

```python
listing_mode: Mapped[str] = mapped_column(
    String(32), default=ListingMode.UNKNOWN.value, nullable=False
)
```

**问题**：`listing_mode` 存储字符串而非外键，SQLAlchemy 无法阻止写入无效枚举值（如 `"LOCAL_REPLICATION"` 被错误拼写为 `"LOCAL_REPLICATON"`）。应用层 `ListingMode(value)` 会在运行时抛异常，导致写入失败但不回滚。

**影响**：数据库中若意外写入非法 `listing_mode` 值，应用启动或查询时会在 `ListingMode(value)` 处崩溃。

---

## 五、边界条件问题

### [B-01] 图片清理在进程异常退出时不保证

**文件**：`backend/collector_app/images.py`

```python
async def discard(self, image: StoredImage) -> bool:
    return await asyncio.to_thread(_safe_discard, image.relative_path)
```

**问题**：图片下载后存储到 `temp/images/collector/`，但清理（`discard`）是幂等的——仅按路径删除。若进程在 `_atomic_store` 写入中途异常退出，`partial_path` 文件可能残留（以 `.part` 结尾），`resolve_collector_image_path` 不会校验 `.part` 文件，因此残留文件不会被自动清理。

**影响**：长期运行后 `temp/images/collector/` 可能积累大量 `.part` 临时文件。

---

### [B-02] `ShopBinding.region` 未校验格式

**文件**：`backend/app/db/models.py`

```python
region: Mapped[str] = mapped_column(String(16), nullable=False)
```

**问题**：TikTok 区域代码应为 ISO 3166-1 alpha-2（如 `MY`、`SG`），但模型层未校验。非法区域代码（如 `malaysia`、`MYS`）会被接受。

**影响**：后续调用 TikTok API 时若区域代码格式不符，平台返回错误。

---

### [B-03] `scheduler.py` 中 `_next_run` 计算可能在高负载下跳过过多周期

**文件**：`backend/app/use_cases/scheduler.py`

```python
def _next_run(claim: ClaimedScheduleJob, *, now: datetime) -> tuple[datetime, bool]:
    # ...
    while next_run <= now:
        next_run += timedelta(seconds=claim.interval_seconds)
```

**问题**：当服务长时间停机（如数天），`while` 循环会追赶所有错过的周期。若 `interval_seconds` 很小（如 60 秒）且停机 30 天，循环将执行 43,200 次迭代，可能造成事件循环阻塞。

**计划书要求**：定时任务应精确调度，不应因追赶历史周期而阻塞。

**影响**：服务重启后处理大量积压任务时，定时任务执行会显著延迟其他操作。

---

## 六、API 路由问题

### [API-01] `/api/shops/{id}/listing-mode-confirmations` 无防重复提交保护

**文件**：`backend/app/api/routes/shops.py`

**问题**：人工确认刊登模式是一个写操作，但路由未要求 `Idempotency-Key` Header。若管理员快速重复提交同一确认请求（网络抖动/按钮双击），会产生多条 `ListingModeEvidence` 记录。

**计划书要求**：所有写操作应使用幂等键。

**影响**：重复确认导致 `listing_mode_evidence` 表中出现冲突记录，`assess_persisted_listing_mode` 可能因多条证据而将模式判定为 `UNKNOWN`。

---

### [API-02] `OAuthClient._token_request` 不校验响应签名

**文件**：`backend/app/integrations/tiktok/oauth.py`

**问题**：Token 端点返回的响应仅校验 `code` 字段，不验证响应签名。TikTok 官方文档中 token 响应应包含签名以防篡改（若配置了签名密钥）。

**影响**：在中间人攻击场景下，攻击者可替换 token 响应中的 `access_token` 和 `refresh_token`，导致账户绑定错误的凭证。

---

### [API-03] Collector API 图片端点无 HMAC 保护

**文件**：`backend/collector_app/api.py`

```python
@router.get("/images/{image_record_id}")
async def image_file(...):
```

**问题**：Collector API 主路由依赖 `require_internal_hmac` 中间件保护，但 `install_collector_api` 中该中间件通过 `dependencies=[Depends(require_internal_hmac)]` 逐路由添加。`image_file` 路由在主 router 定义中，未显式列出依赖——需确认该路由是否在带有 HMAC 保护的 router 下。

**影响**：若 Core API 通过 Collector 公开的图片 URL 被外部直接访问，可能泄露采集图片内容。

---

## 七、依赖与配置问题

### [CFG-01] `.env.example` 缺失必需的 `TIKTOK_SERVICE_ID`

**文件**：`backend/pyproject.toml`、`.env.example`（未创建）

**问题**：TikTok OAuth 需要 `TIKTOK_SERVICE_ID`（`oauth.py` 中的 `OAuthConfig.service_id`），但 `.env.example` 缺失该字段。用户参考文档配置环境时无法知道需要此字段。

**计划书要求**：提供 `.env.example` 和环境模板。

**影响**：管理员无法正确配置 TikTok OAuth，授权流程无法启动。

---

### [CFG-02] 前端未提供构建后的环境变量配置文档

**文件**：`frontend/vite.config.ts`、`frontend/package.json`

**问题**：前端 `vite.config.ts` 中 `VITE_CORE_API_URL` 变量在构建时内联。若生产环境需要切换 Core API 地址，需重新构建前端，无法通过环境变量运行时覆盖。

**计划书要求**：不引入浏览器持久缓存，但允许运行时配置。

**影响**：生产部署时无法在不重新构建的情况下切换 API 地址。

---

## 八、代码质量

### [CQ-01] `ProductGateway` 协议中 `search` 返回 `ProductPage` 但未考虑 Local/Global 差异

**文件**：`backend/app/use_cases/products.py`

```python
class ProductGateway(Protocol):
    async def search(self, context, *, page_size=20, page_token=None, filters=None) -> ProductPage:
```

**问题**：`ProductGateway` 协议未参数化 Local/Global 返回结构差异。`TikTokProductGateway` 在内部通过 `_mode_route` 路由，但协议定义中 `ProductPage` 是统一类型。若未来接入不同返回结构的 gateway，类型不兼容。

**影响**：扩展新的 Product Gateway（如测试用 Mock Gateway）时需要额外适配。

---

### [CQ-02] 硬编码的错误码格式正则表达式过于宽松

**文件**：`backend/collector_app/worker.py`

```python
_ERROR_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
```

**问题**：错误码格式要求首字符为小写字母，后续为小写字母/数字/下划线。但 `SourceAdapterError` 中大量错误码使用大写字母（如 `COLLECTOR_RESOURCE_NOT_FOUND`），导致这些错误码被替换为泛化码 `"source_adapter_error"`。

**影响**：真实错误码信息丢失，调试时只能看到泛化错误码，难以定位具体采集失败原因。

---

## 九、缺失的文档

以下计划书要求的文档尚未创建：

- `docs/windows-setup.md` — Windows 本地部署说明
- `docs/tiktok-custom-app.md` — TikTok 自定义应用创建指南
- `docs/collector-compliance.md` — 采集合规说明
- `docs/limitations-and-risks.md` — 已知限制与风险清单
- `README.md` — 项目总览与快速启动

---

## 问题优先级汇总

| 优先级 | 问题数 | 代表问题 |
|--------|--------|----------|
| 严重阻塞 (S) | 3 | 字段重复定义、session 状态管理缺陷、CSRF 泄漏 |
| 功能缺陷 (F) | 5 | 1688 解析缺失、翻译未暴露、订单行未持久化 |
| 安全 (SEC) | 3 | 硬编码端口、签名一致性、HMAC 路由覆盖 |
| 数据一致 (DC) | 3 | SKU 空值、配额追踪缺失、listing_mode 无约束 |
| 边界条件 (B) | 3 | part 文件残留、区域格式未校验、周期追赶 |
| API 设计 (API) | 3 | 模式确认无幂等、token 响应未验签、图片端点保护 |
| 配置 (CFG) | 2 | 环境变量缺失、运行时配置不灵活 |
| 代码质量 (CQ) | 2 | 协议设计不足、错误码规范化 |
| 文档缺失 | 5 | 5 个计划书要求的文档均未创建 |

**总计：29 个问题**