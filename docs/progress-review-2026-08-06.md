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

### 真实联调状态

- **未执行真实 Shop Query，不计为通过。** 当前进程中 `MIAOSHOU_ENABLED`、`MIAOSHOU_APP_KEY`、`MIAOSHOU_APP_SECRET`、`MIAOSHOU_BASE_URL` 均未设置，仓库中也没有本地 `resources/config.json` 凭据文件。
- 在线 Apifox 文档当前需要密码，无法独立复核官方鉴权契约；本阶段实现依据仅为本地接口材料、Skill reference 和脚本的一致描述。
- 因缺少显式环境凭据且官方在线契约不可独立核验，未从敏感文档自动提取或尝试任何凭据，也未向真实上游发起请求。精确阻塞为 `MIAOSHOU_PROVIDER_DISABLED`；即使只打开开关，缺少凭据仍会转为 `BLOCKED_LIVE_CREDENTIALS`。

### 计划状态说明

计划文件 YAML 中八项 todo 仍保持 `pending`：它们是全项目验收项，本次可选只读 Provider 里程碑没有完成任一整项，故不将局部实现虚报为项目级完成。