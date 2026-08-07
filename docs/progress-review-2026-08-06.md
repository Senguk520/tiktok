# TikTok 单店管理系统阶段进度复核

> 初始复核日期：2026-08-06
> 初始复核范围：当时工作树中的六项指定问题。
> 初始证据口径：下列六项保留当时的只读核实结论；文末“妙手平台只读接入里程碑”另行记录后续实际执行的测试、构建与联调状态，二者不得混读。

## 复核结论

六项原问题在当前工作树中均未按原描述继续存在。其中，前端 Session 修复缺少专项回归测试，状态记为“已修复待验证”；其余五项已有明确实现链路，状态记为“已确认”。

### 1. `PreparedDraftSubmission` 重复字段

**状态：已确认**

- `backend/app/use_cases/products.py:107-113` 中，`reconciliation_required` 与 `known_product_ids` 当前各定义一次，不存在重复字段。
- 当前未提交差异为这两个字段各新增一次，并不是删除重复定义的补丁。
- `HEAD` 版本原本只包含 `draft_id`、`operation_id`、`product`、`quota_snapshot_id`，也未发现重复字段；因此原审查中“重复字段导致模块无法导入”的结论与当前及 `HEAD` 代码均不一致。
- `backend/tests/test_scheduler.py:36,529-534` 已导入并构造 `PreparedDraftSubmission`，但没有字段唯一性专项断言。

**风险与缺口**

- `backend/app/use_cases/products.py` 及相关商品测试目前存在较大未提交修改；本轮没有执行导入、测试收集或完整测试，仍需在提交前通过虚拟环境验证。

### 2. 前端 Session 的 CSRF 清理与 `canWrite` 语义

**状态：已修复待验证**

- `frontend/src/state/session.ts:14-18` 的 `setAnonymous()` 会同时清空 `csrfToken` 与 `expiresAt`。
- `frontend/src/state/session.ts:59-62` 的 `logout()` 成功后调用 `setAnonymous()`；会话过期及检查失败路径同样复用该清理逻辑。
- `frontend/src/state/session.ts:73` 的 `canWrite` 同时要求：
  - `phase === 'authenticated'`；
  - `csrfToken` 非空。
- 因此匿名态、只读态或已登出状态不会仅因残留 Token 获得写权限。
- 该文件当前没有未提交变更，修复不是本轮并发补丁。

**缺失测试**

`frontend/tests/` 当前未发现 Session 状态专项测试，至少还需覆盖：

1. `logout()` 后 `csrfToken` 与 `expiresAt` 清零；
2. 收到 `401` 并执行 `expire()` 后 Token 清零；
3. `readonly` 状态即使存在 Token 也不可写；
4. 登出后重新登录时重新执行会话检查。

### 3. 1688 公共页面 JSON-LD 解析与 Open Platform 注册

**状态：已确认**

#### 公共页面解析链路

- `backend/collector_app/normalizers.py:86-110`：提取 `application/ld+json`。
- `backend/collector_app/normalizers.py:114-126`：将 1688 公共页面分派到来源专属规范化逻辑。
- `backend/collector_app/normalizers.py:326-401`：解析 Product JSON-LD 的标题、描述、图片、报价及 SKU 信息。
- `backend/collector_app/normalizers.py:404-435`：识别顶层 Product 以及 `@graph` 中的 Product 节点。

#### Open Platform 注册链路

- `backend/collector_app/main.py:42-59,76-80`：从环境配置构建采集服务及来源配置。
- `backend/collector_app/sources/__init__.py:28-49`：`default_source_registry()` 同时注册 `Alibaba1688OpenPlatformAdapter` 与公开页面适配器。

#### 现有测试证据

- `backend/tests/test_collector_worker.py:694-721`：覆盖合成 Product JSON-LD 的规范化。
- `backend/tests/test_collector_sources.py:213-220,223-233,320-369`：覆盖默认注册、缺少凭据时失败关闭及签名网关。

**风险与缺口**

- 当前 JSON-LD 测试使用合成文档，不代表真实 1688 页面契约已经验证。
- 若真实页面不提供受支持的 Product JSON-LD，当前实现会按设计返回 `source_layout_unsupported`，不会伪造采集成功。
- 上述相关文件当前没有未提交变更。

### 4. 订单行持久化

**状态：已确认**

#### 模型与迁移

- `backend/app/db/models.py:356-378`：定义 `OrderLineRecord`。
- `backend/migrations/core/__init__.py:56-57,67`：核心数据库 v3 迁移创建 `order_records` 与 `order_line_records`。

#### 写入链路

- `backend/app/use_cases/orders.py:121-220`：获取订单详情，并以 `detail=True` 调用 `upsert_orders()`。
- `backend/app/repositories/orders.py:20-42,45-95`：更新订单时删除旧行，再写入当前详情中的完整订单行字段。

#### 读取链路

- `backend/app/repositories/orders.py:147-176`：读取本地订单及其订单行。
- `backend/app/api/routes/orders.py:119-146,193-205`：通过 `/local/details` 返回持久化订单行。

#### 现有测试证据

- `backend/tests/test_order_features.py:234-268`：直接查询 `OrderLineRecord` 并断言 SKU。
- `backend/tests/test_persistence_domain.py:68-78`：检查订单表与订单行表已创建。

**回归风险**

- `backend/app/domain/orders.py:148` 会将缺失的 `line_items/items` 规范化为空列表。
- `backend/app/repositories/orders.py:84-87` 在 `detail=True` 时仍会先删除现有订单行。
- 如果上游返回不完整详情，已有订单行可能被清空；当前未发现该场景的专项测试。

### 5. 翻译后端路由与前端入口

**状态：已确认**

#### 后端链路

- `backend/app/api/routes/tools.py:164-294`：提供 `POST /api/shops/{shop_binding_id}/tools/translate`，包含 CSRF、幂等登记、Provider 配置检查、调用与审计。
- `backend/app/api/runtime.py:52-59`：装配 Azure Translator Provider。
- `backend/app/main.py:119-123`：注册 Tools Router。

#### 前端链路

- `frontend/src/api/core.ts:193-208,399-410`：定义翻译请求/响应类型并暴露 `coreApi.translate()`。
- `frontend/src/views/ToolsView.vue:62-85,131-198`：提供翻译输入、请求调用和结果展示。
- `frontend/src/router/index.ts:24`：注册工具页面路由。
- `frontend/src/views/WorkspaceLayout.vue:22`：提供导航入口。

#### 现有测试证据

- `backend/tests/test_value_tools.py:121-159`：覆盖 Azure Provider。
- `backend/tests/test_value_tools.py:236-272`：覆盖翻译 Provider 未配置时的失败关闭行为。

**未提交差异与缺口**

- `backend/app/api/routes/tools.py:14-19,165-168` 当前未提交差异仅为复用共享 `IdempotencyKey` 类型；翻译路由及前端入口并非本轮新增。
- 尚缺少 Provider 配置成功后的 HTTP 端到端测试。
- 尚缺少 `ToolsView.vue` 的前端交互测试。

### 6. Collector 图片路由的 HMAC 保护

**状态：已确认**

- `backend/collector_app/api.py:110-131`：`require_internal_hmac()` 校验时间戳、HTTP 方法、请求路径及正文。
- `backend/collector_app/api.py:134-138`：该依赖被设置为整个 `/internal/v1` Router 的公共依赖。
- `backend/collector_app/api.py:223-247`：图片读取路由位于同一 Router，因此继承 HMAC 验证，不需要在单个路由上重复声明依赖。
- `backend/app/integrations/collector.py:170-182,208-263`：Core 读取图片时通过统一 `_request()` 生成并附带签名。
- `backend/shared/security.py:258-324`：内部签名采用 HMAC-SHA256、时间窗口校验及常量时间比较。

#### 现有测试证据

- `backend/tests/test_security.py:46-81`：覆盖签名对方法、路径、正文和时间戳的绑定。
- `backend/tests/test_collector_http_boundary.py:231-304`：覆盖内部 Router 对无签名、过期签名及篡改请求的拒绝。
- `backend/tests/test_collector_http_boundary.py:337-373`：覆盖通过签名客户端读取图片。

**缺失测试**

- 当前没有直接向 `/internal/v1/images/{image_record_id}` 发起无签名请求并断言 `401` 的路由级负向测试。虽然 Router 公共依赖已经提供保护，仍建议补充此测试防止未来路由被移动到未受保护的 Router。

## 工作树说明

本次只读复核开始时：

- `main` 相对 `origin/main` 为 ahead 2；
- 暂存区为空；
- 工作树中存在 11 个后端文件修改及未跟踪 `docs/` 内容；
- 与本次六项直接相关的未提交文件为：
  - `backend/app/use_cases/products.py`；
  - `backend/app/api/routes/tools.py`。

本次核实只读取了 Git 状态、差异、`HEAD` 版本和源码，没有修改现有文件，也没有执行会生成缓存、临时数据库或构建产物的命令。

## 下一验证门槛

在将上述六项初始复核结论更新为“已执行验证”前，仍需分别补齐对应专项证据；本次妙手里程碑的验证结果不替代这些缺口：

1. 前端 Session 专项测试；
2. Collector 图片路由无签名负向测试；
3. 订单不完整详情不得误清空已有订单行的回归测试；
4. TikTok、采集与翻译真实凭据链路继续保持 `BLOCKED_LIVE_CREDENTIALS`，不得以 Mock 结果替代。

## 妙手平台只读接入里程碑（阶段更新）

### 实现范围与边界

- 新增可选的妙手 JCOP Provider，仅接入授权店铺列表 `POST /open/v1/product/shop/shop/get_shop_list`。
- Core API 新增管理员会话保护的 `GET /api/miaoshou/capabilities` 与 `GET /api/miaoshou/shops`；前端新增“妙手店铺”页面，支持 TikTok/TikTok Global、站点筛选和分页。
- 本阶段只读查询，不写 SQLite、不建立进程缓存、不回退到演示数据，也不替换 TikTok 官方 API 主链路。
- 采集箱编辑、认领、发布及任何真实店铺写操作不属于本阶段，继续失败关闭。

### 配置与安全边界

- Provider 默认关闭。启用需要显式设置 `MIAOSHOU_ENABLED=true`，并通过环境变量提供 `MIAOSHOU_APP_KEY`、`MIAOSHOU_APP_SECRET`；`MIAOSHOU_BASE_URL` 必须为无用户信息、查询串和片段的 HTTPS 地址，超时由 `MIAOSHOU_TIMEOUT_SECONDS` 控制。
- `.env.example` 仅包含空占位符和公开基址，没有真实凭据。
- 上游错误只映射为稳定错误类别；响应和异常不返回 App Secret、签名或原始上游正文。
- 本地 `docs/妙手/妙手接口文档.txt` 继续视为敏感材料，已加入 `.gitignore`；不复制到源码、测试、环境模板或本文。

### 已执行验证

- 后端全量 Ruff：`cd backend && .\.venv\Scripts\python.exe -m ruff check .`，结果 `All checks passed!`。
- 妙手及关键 API/平台/安全回归：`cd backend && .\.venv\Scripts\python.exe -m pytest -q tests/test_miaoshou.py tests/test_api_routes.py tests/test_tiktok_platform.py tests/test_security.py`，结果 `27 passed, 1 warning`。
- 后端完整测试：`cd backend && .\.venv\Scripts\python.exe -m pytest -q`，结果 `152 passed, 1 skipped, 1 warning`；跳过项是当前 Windows 账户缺少创建符号链接权限，warning 是测试依赖的既有弃用提示。
- 前端类型检查：`cd frontend && npm run typecheck`，通过。
- 前端测试：`cd frontend && npm test`，结果 `4 passed` 个测试文件、`6 passed` 个测试。
- 前端生产构建：`cd frontend && npm run build`，通过；保留既有的主包超过 500 kB 警告。
- 妙手专项离线测试覆盖签名确定性、配置失败关闭、HTTPS 基址限制、上游错误脱敏、店铺字段规范化、未登录拒绝、Provider 未启用阻断及受控 transport 下只读返回。

### 真实联调状态（2026-08-07 证据校正）

- 当前会话历史中曾出现“真实只读 Shop Query 返回 1 家 TikTok/MY 可用店铺”的执行报告，但仓库、终端记录和脱敏验收产物中没有找到足以独立复现该结论的持久证据。
- 因此正式状态既不继续断言“从未执行”，也不标记为“已验收通过”，统一记为：**`REQUIRES_REPRODUCIBLE_READONLY_RECHECK`**。
- 重新验收只允许 Shop Query，产物仅记录时间、能力、站点、店铺数量、不可逆脱敏标识、状态和错误类别；不得保存请求签名、Token、Secret、完整响应或调用任何写接口。
- Provider 默认关闭与缺配置 fail-closed 的结论不变；没有显式本机环境配置时仍返回 `MIAOSHOU_PROVIDER_DISABLED` / `BLOCKED_LIVE_CREDENTIALS`。

### 计划状态说明（已被最新版计划取代）

旧计划中八项全为 `pending` 的粗粒度状态不再适合当前代码库。2026-08-07 已按源码、测试和真实验收边界重构为 11 个可验证里程碑；详细状态以 [`tiktok_单店管理系统_84523573.plan.md`](tiktok_单店管理系统_84523573.plan.md) 为准。

## 最新版计划重构（2026-08-07 阶段更新）

### 盘点范围

- 当前 Git 基线及近七次提交；
- Core/Collector 入口、API Router、领域模型、迁移、集成层、用例和测试清单；
- Vue 路由、状态、页面与前端测试；
- 妙手 Core Provider 与七组 Skills 的职责边界；
- 旧问题清单、环境模板和交付文档缺口。

### 重构结果

- 删除“从空仓库构建”“当前只有一个接口文件”等历史假设，将当前双服务、双 SQLite、Vue 管理台、TikTok 官方主链路、Collector 隔离边界和妙手可选 Provider 作为新基线。
- Frontmatter 重拆为 11 个里程碑：5 个 `completed`、2 个 `in_progress`、4 个 `pending`。`completed` 只表示工程边界和离线证据完成，不代表真实平台验收。
- 新 Roadmap 分为 P0-P5：可信基线、TikTok 授权、商品生命周期、Provider 编排、订单/工具/自动化稳固、真实联调与 Windows 交付。
- 明确妙手 Shop Query 已进入 Core；Source Import、公共采集箱、认领、类目推荐、产品编辑和发布仍是 Skills/脚本资产，不得据此宣称 Core 已集成。
- 保留 TikTok 官方 API 作为店铺事实与刊登主链路；妙手写能力必须显式选择 Provider，并在预览、CSRF、幂等、审计和结果对账完成前保持禁用。

### 首个执行 Checkpoint

下一阶段为 **P0-A：Session 与边界测试**：

1. 修复前端模块级 `sessionChecked` 的重登风险；
2. 补齐 Session 登出、401、readonly 和重新登录专项测试；
3. 补充 Collector 图片路由直接无签名 `401` 回归；
4. 完成后运行前后端完整质量门槛并更新本文。

### 尚未解决的阻塞

- TikTok OAuth 领域能力尚未形成 HTTP + UI 闭环；
- 商品编辑、价格、库存、上下架和删除尚未形成完整 API + UI 生命周期；
- 订单不完整详情仍存在误清空已有行的风险；
- 翻译成功 HTTP E2E、`ToolsView` 交互、真实 TikTok/CJ/1688/Azure 联调仍缺证据；
- `.env.example` 未覆盖完整运行配置，README 与 Windows/合规/风险文档缺失；
- 妙手真实只读结果需要按脱敏格式重新执行并持久化证据。

### 本轮验证方式

- 本轮只修改计划、进度和外部记忆文档，没有重新运行 pytest、Ruff、前端测试、类型检查或构建。
- 通过当前源码结构、测试文件、近期 Git 提交和 Git Diff 进行状态核对；既有 `152 passed, 1 skipped` 与前端 `6 passed` 仅作为历史验证证据引用。
- `docs/remember.md` 保存本轮逐 Checkpoint 的可接续事实，但不替代本计划或本文的正式状态。

## P0-A 实际执行更新（2026-08-07）

### 状态

**P0-A：Session 与边界测试已完成（离线/模块级验证）。** 本更新只覆盖 P0-A，不提前变更 P0-B 订单、翻译或其他能力状态；主计划 frontmatter 中的 `p0-trustworthy-baseline` 仍保持 `in_progress`，但正文已将 P0-A 标记完成并把最近下一步推进到 P0-B。

### 实际修改文件

- `frontend/src/router/index.ts`：移除模块级一次性 `sessionChecked`，路由守卫改为消费 Session 状态的 `needsCheck`。
- `frontend/src/state/session.ts`：由 Session 状态模块管理检查生命周期；初始检查、登录、登出和 `expire()` 会重新标记需检查，`check()` 完成后清除标记；同时按 Core `authenticated` 状态判定只读/认证阶段。
- `frontend/tests/session.test.ts`：新增 4 个模块/状态行为回归，覆盖登出清理 `csrfToken/expiresAt`、401/expire 清理 token、readonly+token 不可写、登出后重新登录再次执行 session check。
- `backend/tests/test_collector_http_boundary.py`：新增直接无签名图片路由请求的 `401` 回归，沿用 `tmp_path` 数据库与 ASGI transport。
- `docs/tiktok_单店管理系统_84523573.plan.md`：将 P0-A 标记为已完成、关闭对应风险项，并把最近下一步推进到 P0-B。
- `docs/remember.md`：追加本轮阶段事实。
- `docs/progress-review-2026-08-06.md`：追加本次实际验证记录。

### 精确命令与结果

- `Set-Location 'H:\tiktok\frontend'; npm test -- --run tests/session.test.ts` → 1 个文件、4 个测试通过。
- `Set-Location 'H:\tiktok\frontend'; npm test` → 5 个测试文件、10 个测试通过。
- `Set-Location 'H:\tiktok\frontend'; npm run typecheck` → `vue-tsc --noEmit` 通过。
- `Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py -k unsigned_image_route` → 1 通过、6 个 deselected、1 个既有 Starlette/httpx 弃用 warning。
- `Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py` → 7 个边界测试通过、1 个既有 warning。
- `Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m ruff check tests/test_collector_http_boundary.py` → `All checks passed!`。
- `Set-Location 'H:\tiktok'; git diff --check` → 通过；仅报告 LF/CRLF 转换提示。

### 未执行项、阻塞与下一步

- 未执行后端全量 pytest、后端全量 Ruff、前端生产 build；也未执行会清理目录、删除固定磁盘文件或涉及业务缓存/持久化缓存的测试。故不能把本轮结果表述为全量质量门槛通过。
- 已知 warning 仅为测试依赖的既有 Starlette/httpx 弃用提示；本轮没有升级依赖。
- P0-A 未覆盖的 P0-B 风险仍包括不完整订单详情误清空订单行、翻译成功 HTTP E2E 与 `ToolsView` 交互；下一步按计划进入 P0-B，再在安全范围内决定全量门槛执行。
- `Set-Location 'H:\tiktok'; npm test`（以及同目录的首次 Session 命令）因仓库根目录没有 `package.json` 返回 `ENOENT`；随后显式切换到 `H:\tiktok\frontend` 重跑，结果如上，未把该失败计入测试通过数。
- 最终精确复跑：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py::test_unsigned_image_route_is_rejected_before_lookup` → `1 passed, 1 warning`。

## P0-B 实际执行更新（2026-08-07）

### 状态

**P0-B：订单与工具一致性已完成（离线/窄范围验证）。** 订单行 presence 三态、翻译成功/失败 HTTP 边界与 `ToolsView` 成功/失败交互均已有回归证据；这不代表真实 Azure 或真实 TikTok 验收。主计划 frontmatter 中 `p0-trustworthy-baseline` 继续保持 `in_progress`，最近下一步推进到 P0-C。

### 实际修改文件与语义

- `backend/app/domain/orders.py`：`NormalizedOrder` 新增显式 `lines_present`；规范化时分别识别 `line_items/items` 缺失、存在空数组、存在非空数组，并将 presence 纳入 PII-free 哈希载荷。非空 `lines` 若未声明 presence 会被领域校验拒绝。
- `backend/app/repositories/orders.py`：Repository 结合明确调用上下文与领域 presence 执行：详情缺字段时保留，详情字段存在时替换（空数组可清空）；列表非空行可更新，列表显式空数组不得删除已有详情。`detail` 同时记录详情同步时间。
- `backend/tests/test_order_features.py`：覆盖原始 payload → 领域对象 → SQLite 的四项回归：详情缺字段保留、详情明确空列表清空、正常列表替换，以及“列表显式空数组后详情缺字段”仍保留完整行；现有正常详情写入继续覆盖。
- `backend/tests/test_api_routes.py`：测试领域样例显式声明订单行 presence，与强化后的领域约束一致。
- `backend/tests/test_value_tools.py`：新增 Azure 配置成功/失败的离线 HTTP E2E；成功链穿过管理员 Session、CSRF、幂等登记、路由、真实 Azure Provider 适配器（`httpx.MockTransport`）、状态落库和审计，失败链验证稳定 502 与正文/密钥不泄露。API 数据库夹具改为 pytest `tmp_path` 测试 lifespan，移除项目数据目录下 UUID 临时文件和手动 unlink。
- `frontend/tests/tools-view.test.ts`：新增组件交互测试，以 `coreApi` 窄 spy 覆盖多行文本提交、成功结果/Provider Request ID 展示，以及失败错误码/请求 ID 提示；不建立 Mock Server，不持久化数据。
- `docs/remember.md`：按 P0-B-1/2/3 及收口验证记录可接续事实和精确结果。
- `docs/tiktok_单店管理系统_84523573.plan.md`：标记 P0-B 完成、关闭订单/工具风险，最近下一步推进到 P0-C；聚合 P0 状态仍为 `in_progress`。
- `docs/progress-review-2026-08-06.md`：追加本节实际执行记录。

### 精确命令与结果

- `Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py::test_order_update_without_line_field_preserves_stored_lines tests/test_order_features.py::test_order_update_with_explicit_empty_lines_clears_stored_lines tests/test_order_features.py::test_order_update_with_present_lines_replaces_stored_lines` → `3 passed in 0.81s`。
- `Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py` → `9 passed in 1.05s`。
- 翻译精确节点首次执行 → `2 errors, 1 warning`；原因是生产安全路径解析器拒绝项目外的 pytest `tmp_path`。改用测试 lifespan 直接装配 `tmp_path` SQLite 后复跑同一命令 → `2 passed, 1 warning in 1.67s`。首次失败未进入 Provider 调用，没有真实网络或手动清理。
- `Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_value_tools.py -k "not test_azure_translator_uses_verified_v3_contract_without_caching"` → `14 passed, 1 deselected, 1 warning in 1.85s`；唯一 deselected 为本轮明确禁止的缓存类测试。
- `Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m ruff check --no-cache app/domain/orders.py app/repositories/orders.py tests/test_order_features.py tests/test_value_tools.py tests/test_api_routes.py` → `All checks passed!`。
- `Set-Location 'H:\tiktok\frontend'; npm test -- --run tests/tools-view.test.ts` → 首次 `1 file / 2 tests passed`（`2.67s`）；类型修正后复跑仍为 `1 file / 2 tests passed`（`2.40s`）。
- `Set-Location 'H:\tiktok\frontend'; npm run typecheck` → 首次发现新测试 3 个类型错误；修正 `ShopSummary` 测试数据和 `ElMessage` mock 返回类型后复跑通过。
- `Set-Location 'H:\tiktok'; git diff --check -- <P0-B 文件>` → 通过，仅有 LF→CRLF 转换提示；任务文件 UTF-8 替换字符和常见长凭据模式扫描无命中。

### 未执行项、风险与下一步

- 未运行后端全量 pytest、前端全量 Vitest、前端 build，也未运行缓存类测试；`backend/tests/test_api_routes.py` 的 HTTP 节点未执行，因为其既有共享 fixture 仍包含项目数据目录临时文件和手动 SQLite/WAL/SHM 清理，与本轮约束冲突。不能把本轮表述为全量质量门槛通过。
- 所有 pytest 命令均显式禁用 cacheprovider；数据库只使用内存 SQLite 或 pytest `tmp_path`。没有固定盘符临时文件、目录清理、递归删除、真实网络或真实平台调用。
- 已知 warning 为既有 Starlette/httpx TestClient 弃用提示；未升级依赖。真实 Azure 成功链和真实 TikTok 订单部分响应仍待显式 live 验收，本轮 MockTransport 只证明离线边界一致性。
- 两个既有无关 BYOK 未跟踪文件保持未读取、未修改、未暂存、未删除。
- 下一步执行 P0-C：补齐完整但无真实值的环境模板，并为妙手只读 Shop Query 生成可复核脱敏证据或明确记录阻塞。

### P0-B 父级审阅回归封口（2026-08-07）

- 父级审阅确认首版 P0-B 存在回归：Repository 只要看到 `lines_present=True` 就替换订单行，导致 `detail=False` 的列表显式空数组错误清除已存完整详情；后续缺字段详情无法恢复这些行。
- 修复后语义为：`detail=True` 时缺字段保留、字段存在时替换或清空；`detail=False` 时非空行可更新、显式空行必须保留。Repository 只消费领域 presence、规范化行与明确调用上下文，没有接收原始平台 DTO。
- 新增专项顺序回归：先保存完整详情，再写入列表显式空 `items`，最后写入缺行字段的详情；最终状态更新到 `SHIPPED`，但原有行、数量均保持不变。
- 精确四节点：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py::test_order_update_without_line_field_preserves_stored_lines tests/test_order_features.py::test_order_list_empty_then_missing_detail_preserves_stored_lines tests/test_order_features.py::test_order_update_with_explicit_empty_lines_clears_stored_lines tests/test_order_features.py::test_order_update_with_present_lines_replaces_stored_lines` → `4 passed in 1.41s`。
- 完整订单文件：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py` → `10 passed in 1.65s`。
- 目标静态检查：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m ruff check --no-cache app/repositories/orders.py tests/test_order_features.py` → `All checks passed!`。
- 本次只使用现有内存 SQLite；未运行缓存类测试、固定磁盘清理、后端全量 pytest 或真实网络。`git diff --check` 通过，仅有既有 LF→CRLF 提示；修复文件替换字符扫描无命中。P0-B 主计划完成状态保持不变，下一步仍为 P0-C。
- 提交前父级合并复跑：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests/test_order_features.py tests/test_value_tools.py::test_translation_http_e2e_succeeds_with_controlled_azure_transport tests/test_value_tools.py::test_translation_http_e2e_fails_closed_without_upstream_leakage` → `12 passed, 1 warning in 1.88s`；cacheprovider 已禁用，warning 仍为既有 TestClient 弃用提示。

## P0-C 实际执行更新（2026-08-07）

### 状态

**P0-C：配置与证据基线已完成（静态配置核对 + 脱敏阻塞证据）。** 进程环境未显式启用妙手 Provider，因此没有执行真实网络请求；可提交产物稳定记录 `MIAOSHOU_PROVIDER_DISABLED`。这满足 P0-C 的“可操作阻塞”口径，但不代表妙手真实只读验收通过。由于缓存类测试、后端完整 pytest、前端完整 build 等 P0 总门槛未执行，主计划 `p0-trustworthy-baseline` 继续保持 `in_progress`。

### 实际修改文件

- `.env.example`：按九组重建 29 个运行时变量；所有 Secret/Token/Key/Bootstrap 值为空，仅保留源码公开安全默认值和仓库相对 SQLite 路径。
- `backend/pyproject.toml`：把最小 `live_checks` 包加入打包发现与 pyright 检查范围。
- `backend/live_checks/report.py`：定义严格 10 字段报告、稳定状态/notes、短 SHA-256 资源指纹与安全 JSON 序列化。
- `backend/live_checks/miaoshou.py`：仅在显式启用且凭据齐全时，复用现有 Client/Adapter/Query Service 读取 `platform=tiktok`、`site=MY` 的店铺列表；只映射稳定结果或阻塞类别。
- `backend/live_checks/writer.py`：随机同目录临时文件写入、flush/fsync 后，以 `os.replace` 原子替换一个指定 JSON 文件；不创建或删除目录。
- `backend/live_checks/__main__.py`、`backend/live_checks/__init__.py`：提供最小 runner 和公开报告类型，不加载仓库 `.env`。
- `backend/tests/test_live_checks.py`：12 个纯离线安全回归，覆盖字段 allowlist、不可逆短指纹、敏感字段拒绝、阻塞稳定、唯一只读查询和 `tmp_path` 单文件写出。
- `docs/live-checks/miaoshou-shop-list-my.json`：本次脱敏阻塞产物，仅含允许字段。
- `docs/tiktok_单店管理系统_84523573.plan.md`、`docs/remember.md`、`docs/progress-review-2026-08-06.md`：同步 P0-C 实际状态、边界和下一步。

### 变量分组与静态核对

- 数据库/路径：`DATABASE_URL`、`CORE_DATABASE_PATH`、`COLLECTOR_DATABASE_PATH`、`SQL_ECHO`。
- 管理员 Session：`ADMIN_BOOTSTRAP_SECRET`、`ADMIN_SESSION_TTL_SECONDS`、`ADMIN_SESSION_COOKIE_SECURE`。
- 应用加密/内部 HMAC：`APP_MASTER_KEY`、`COLLECTOR_INTERNAL_HMAC_SECRET`。
- CORS/host：`FRONTEND_ORIGINS`、`SERVICE_ALLOWED_HOSTS`；loopback 开关没有环境读取点。
- TikTok：`TIKTOK_SERVICE_ID`、`TIKTOK_APP_KEY`、`TIKTOK_APP_SECRET`、`TIKTOK_API_BASE_URL`。
- Collector 来源：`CJ_ACCESS_TOKEN`、`ALIBABA_1688_APP_KEY`、`ALIBABA_1688_APP_SECRET`、`ALIBABA_1688_ACCESS_TOKEN`。
- Azure Translator：`AZURE_TRANSLATOR_KEY`、`AZURE_TRANSLATOR_REGION`、`AZURE_TRANSLATOR_ENDPOINT`、`AZURE_TRANSLATOR_TIMEOUT_SECONDS`。
- 妙手：`MIAOSHOU_ENABLED`、`MIAOSHOU_APP_KEY`、`MIAOSHOU_APP_SECRET`、`MIAOSHOU_BASE_URL`、`MIAOSHOU_TIMEOUT_SECONDS`。
- 前端：`VITE_CORE_API_URL`；Vite 默认从 `frontend/.env` 读取，根模板只作为中央安全参考。
- 静态集合比较结果：`RUNTIME_ENV_COUNT=29 TEMPLATE_ENV_COUNT=29 MISSING=[] EXTRA=[]`。
- 未新增源码不存在的 worker/scheduler 开关。已记录技术债：Core→Collector 基址、Core/Collector worker lease/batch/poll delay、TikTok OAuth URL/Token URL、TikTok/Collector timeout 和 loopback 行为仍硬编码。

### 妙手进程环境与 live 状态

- 布尔检查结果：`MIAOSHOU_ENABLED` 不存在且未启用；`MIAOSHOU_APP_KEY`、`MIAOSHOU_APP_SECRET`、`MIAOSHOU_BASE_URL` 均不存在。命令只输出布尔，未读取 `.env`，也未输出值、长度、前后缀或 repr。
- runner 结果：`status=BLOCKED`、`site=MY`、`error_category=MIAOSHOU_PROVIDER_DISABLED`、`shop_count=0`。
- 未进入 Provider 网络路径；未执行任何真实写接口，也未把历史聊天中的成功报告当作本次验收。

### 已执行命令与结果

- 进程环境布尔检查：`& 'H:\tiktok\backend\.venv\Scripts\python.exe' -B -c "import json, os; raw=os.environ.get('MIAOSHOU_ENABLED'); print(json.dumps({'MIAOSHOU_ENABLED_PRESENT': raw is not None, 'MIAOSHOU_ENABLED_TRUE': isinstance(raw, str) and raw.strip().lower() == 'true', 'MIAOSHOU_APP_KEY_PRESENT': bool(os.environ.get('MIAOSHOU_APP_KEY','').strip()), 'MIAOSHOU_APP_SECRET_PRESENT': bool(os.environ.get('MIAOSHOU_APP_SECRET','').strip()), 'MIAOSHOU_BASE_URL_PRESENT': bool(os.environ.get('MIAOSHOU_BASE_URL','').strip())}, sort_keys=True))"` → 五项均为 `false`。
- 变量集合比较：使用仓库 Python `-B -c` 读取明确列出的已提交设置文件及 `.env.example` 键集合 → `RUNTIME_ENV_COUNT=29 TEMPLATE_ENV_COUNT=29 MISSING=[] EXTRA=[]`。
- `Set-Location 'H:\tiktok\backend'; $env:PYTHONDONTWRITEBYTECODE='1'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -B -m live_checks` → `MIAOSHOU_PROVIDER_DISABLED`，原子写出阻塞产物。
- 首次 `ruff check --no-cache live_checks tests/test_live_checks.py` → 发现 1 个未使用 import、1 个 import 排序问题；手工修正后 `& 'H:\tiktok\backend\.venv\Scripts\python.exe' -B -m ruff check --no-cache live_checks tests/test_live_checks.py` → `All checks passed!`。
- `Set-Location 'H:\tiktok\backend'; $env:PYTHONDONTWRITEBYTECODE='1'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -B -m pytest -p no:cacheprovider -q tests/test_live_checks.py` → 多次复跑均为 `12 passed`；最终一轮 `12 passed in 0.22s`。
- 产物反序列化校验 → `ARTIFACT_VALID=true FIELD_COUNT=10 STATUS=BLOCKED ERROR_CATEGORY=MIAOSHOU_PROVIDER_DISABLED`。
- `.env.example` 安全值校验 → `ENV_ENTRIES=29 SECRET_PLACEHOLDERS_EMPTY=true DEFAULTS_ALLOWLISTED=true`。
- `backend/pyproject.toml` TOML/包范围校验 → `LIVE_CHECKS_PACKAGED=true`。
- 对 12 个显式本任务文件执行 UTF-8 严格解码、替换字符、行尾空白和常见长凭据模式扫描 → `SAFETY_FILE_COUNT=12 ISSUE_COUNT=0 ISSUES=[]`；扫描列表不含两个 BYOK 文件。
- `Set-Location 'H:\tiktok'; git diff --check` → 通过，仅有 `.env.example`、`backend/pyproject.toml` 和两份文档的 LF→CRLF 工作副本提示；新增未跟踪任务文件由上述显式扫描覆盖。

### 未执行项、风险与下一步

- 未执行真实妙手请求，因为进程环境没有显式启用 Provider；没有读取根 `.env`、被 Git 忽略的妙手接口资料、`docs/妙手`、凭据文件或无关 BYOK 文件。
- 未运行任何缓存功能/缓存类测试、pytest cache、npm cache、后端完整 pytest、前端全量 test/typecheck/build、构建缓存、目录清理、递归删除或固定磁盘临时文件；本节只声明精确窄测结果。
- 短资源指纹用于仓库内脱敏关联，不应被当作授权标识或外部稳定 ID；真实妙手读验收仍需在未来由显式进程环境触发同一只读 runner。
- 最近下一步为 **P0 安全质量门槛收口/评估**：逐项判断剩余完整测试与前端门槛能否在当前安全约束下执行；不能安全执行的门槛继续保持未完成，不直接跳入 P1。
- 提交前父级复跑：`Set-Location 'H:\tiktok\backend'; $env:PYTHONDONTWRITEBYTECODE='1'; .\.venv\Scripts\python.exe -B -m pytest -p no:cacheprovider -q tests/test_live_checks.py` → `14 passed in 0.19s`；目标 Ruff `-B -m ruff check --no-cache live_checks tests/test_live_checks.py` → `All checks passed!`。本次未执行 runner、未改写产物或调用网络。

### P0-C 父级审阅回归封口（2026-08-07）

- 父级审阅确认首版存在验收漏洞：请求虽然固定为 TikTok/MY，但上游归一化结果未再次校验作用域；若响应保留其他平台或站点，报告可能仍用固定作用域生成成功指纹并误报通过。
- 修复后，所有归一化店铺必须逐项精确属于 `platform=tiktok`、`site=MY`，才可进入成功计数与指纹生成；任一越界项不静默过滤，整次检查稳定返回 `FAILED / INVALID_RESPONSE`、`shop_count=0`、空指纹。
- 离线回归分别覆盖其他 platform 与其他 site，并保留唯一只读路径和请求参数断言；成功多项场景同时证明重复身份去重后 `shop_count`、指纹数量与唯一指纹数量一致。
- 目标静态检查：`Set-Location 'H:\tiktok\backend'; & '.\.venv\Scripts\python.exe' -B -m ruff check --no-cache live_checks/miaoshou.py tests/test_live_checks.py` → `All checks passed!`。
- 完整 live-check 离线文件：`Set-Location 'H:\tiktok\backend'; $env:PYTHONDONTWRITEBYTECODE='1'; & '.\.venv\Scripts\python.exe' -B -m pytest -p no:cacheprovider -q tests/test_live_checks.py` → `14 passed in 0.22s`。
- 修复后只检查 `MIAOSHOU_ENABLED` 是否为 true，结果为 `false`；随后以仓库虚拟环境和 `-B` 重生成证据，runner 返回 `MIAOSHOU_PROVIDER_DISABLED`。严格反序列化得到 `BLOCKED / MIAOSHOU_PROVIDER_DISABLED / shop_count=0 / fingerprint_count=0`，未进入真实网络路径。
- 12 个显式 P0-C 文件的 UTF-8、替换字符、行尾空白与定向凭据模式扫描结果为 `ISSUE_COUNT=0`；`git diff --check` 通过，仅保留既有 LF→CRLF 提示。工作区仍只有 12 个 P0-C 文件和两个既有无关 BYOK 未跟踪文件，后两者未读取、修改、暂存或删除。
- Provider 边界全部由测试替身截断，没有真实网络、真实凭据、缓存类测试、目录清理或 Git commit。主计划 P0-C 完成状态保持，`p0-trustworthy-baseline` 仍为 `in_progress`。
