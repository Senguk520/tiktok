# 最新版项目计划重构：外部记忆

> 用途：记录本次计划重构的可接续事实，不替代正式计划与进度文档。
> 日期：2026-08-07

## Checkpoint 0：任务启动

- 目标：以当前源码和测试为事实基线，重建 `docs/tiktok_单店管理系统_84523573.plan.md`，并同步阶段进度。
- Git 基线：`main` 相对 `origin/main` ahead 7；最近提交已包含妙手只读店铺查询与妙手 Skills。
- 工作区发现：旧计划文件当前处于删除状态；本任务将按最新版架构重新创建，不回退为旧内容。
- 无关未跟踪文件：两个 Cursor/BYOK 调查文件，保持不动。
- 安全约束：不读取或传播妙手明文凭据，不调用妙手写接口，不把 Mock/transport 测试记为真实验收。
- 下一步：盘点现行模块、运行边界、测试和已有缺口。

## Checkpoint A：现行基线盘点完成

- 运行架构：Vue 3 SPA → loopback-only Core FastAPI → TikTok 官方 API；Core 通过带 HMAC 的 `/internal/v1` HTTP 边界调用独立 Collector FastAPI；Core/Collector 各自使用 SQLite 与进程内轮询 worker，但不缓存业务结果。
- Core 已装配管理员 Session、店铺、商品草稿/提交、订单、采集导入、翻译/利润、调度、审计、Webhook fail-closed、妙手只读店铺查询等路由；核心迁移已到 v4，Collector 迁移为 v1。
- Collector 已实现 CJ 官方 API、1688 Open Platform、显式 1688 公共页模式、受控出站、图片登记和跨进程导入；官方模式不会静默降级。
- 前端已有总览、妙手店铺、商品、订单、工具、调度、审计页面；当前没有独立的 TikTok OAuth 授权页面、采集任务页面或商品详情/编辑路由。
- TikTok OAuth/刷新/去授权已存在应用层函数和测试，但没有发现 Core HTTP 路由调用，尚未形成用户可操作闭环。
- TikTok Endpoint Registry 覆盖 Local/Global 多项端点，但当前产品 Gateway 主要实现图片上传、创建、搜索、详情和删除；Core 产品 API 暴露草稿、确认、额度、提交、搜索和详情，编辑/价格/库存/上下架/删除尚未形成完整 API+UI 闭环。
- 妙手 Core 运行时只集成 Shop Query；其余 Source Import、公共采集箱、认领、类目推荐、产品编辑、发布均是独立 Skill/脚本资产，不能标记为产品内已集成。
- 交付资料仍明显不足：仓库没有 README、Windows 安装、自定义应用、采集合规、限制风险等旧计划要求的文档；当前 `.env.example` 只列妙手变量，未覆盖 Core/Collector/TikTok/翻译必要配置。
- 已有验证证据来自 2026-08-07 之前记录：后端 152 passed、1 skipped；前端 6 passed，typecheck/build 通过。本轮只改文档，不把这些结果当作重新执行。
- 下一步：按“已交付 / 已实现待真实验收 / 缺口 / 辅助资产”建立保守状态矩阵。

## Checkpoint B：能力状态与缺口矩阵完成

- 已交付（离线可复核）：双服务安全底座、双 SQLite 与迁移、AES-GCM/HMAC/脱敏/no-store、TikTok 签名客户端与端点注册、刊登模式证据和幂等/额度守卫、商品草稿创建链、订单同步与订单行、Collector 受控采集和导入、利润/调度/审计、妙手只读 Shop Query 的 Core 适配与页面。
- 已实现但真实验收受阻：TikTok OAuth/Token/店铺授权领域链、TikTok Local/Global 创建与只读查询、CJ/1688 官方适配、Azure 翻译、妙手只读店铺查询。阻塞原因分别是缺 UI/API 闭环或缺可复现的真实凭据联调证据。
- 明确功能缺口：TikTok OAuth HTTP+UI、商品编辑/价格/库存/上下架/删除 UI/API、采集任务 UI、Webhook 签名契约和状态变更、完整环境模板与 Windows 交付文档。
- 明确回归缺口：路由模块级 `sessionChecked` 仍可能导致登出后再次登录不检查 Session；缺 Session 专项测试；缺 Collector 图片路由直接无签名 401；订单不完整详情仍可能清空已存行；缺翻译成功 HTTP E2E 与 `ToolsView` 交互测试。
- 数据/边界技术债：Core→Collector 地址仍固定为 `127.0.0.1:8010`；`ShopBinding.region` 的数据库层格式约束不足；图片异常中断后的 `.part` 清理需要确认；前端生产包已有 >500 kB 警告。
- 妙手状态：Shop Query 已进入 Core；其余 6 组能力仅为独立 Skills。聊天中出现过 MY 店铺只读查询成功报告，但未在终端或持久验收产物中找到脱敏证据，正式状态定为“需重新执行可复现只读验收”，不能写成已通过。
- Roadmap 顺序确定：P0 基线可信化 → P1 TikTok 授权闭环 → P2 商品生命周期补齐 → P3 来源采集与妙手 Provider 编排 → P4 订单/工具/自动化稳固 → P5 真实联调与 Windows 交付。
- 下一步：按上述状态与阶段顺序重写主计划。

## Checkpoint C：最新版主计划已重建

- 已重新创建 `docs/tiktok_单店管理系统_84523573.plan.md`，旧版“从空仓库开始”和八项全 pending 已移除。
- Frontmatter 拆为 11 个可验证里程碑：5 个 completed、2 个 in_progress、4 个 pending；正文解释 completed 不等于真实平台验收。
- 主计划已加入现行架构、模块职责、三条关键数据流、安全不变量、已交付能力、真实验收状态、妙手 Skills/Runtime 边界、P0-P5 Roadmap 和最终完成定义。
- 下一阶段首个执行点固定为 P0-A：Session 重登语义、Session 专项测试和 Collector 图片路由无签名回归。
- 本次只重构文档，没有调用真实平台，也没有修改源码。
- 下一步：同步进度复核，校验计划 YAML、链接、状态、Git Diff 和敏感信息。

## Checkpoint D：同步与校验完成

- `docs/progress-review-2026-08-06.md` 已新增“最新版计划重构”阶段记录，并将妙手真实联调从“未执行”校正为“历史报告存在但缺少可复核持久证据，需重新执行脱敏只读验收”。
- 计划 frontmatter 校验通过：11 个唯一 todo，状态只使用 `completed/in_progress/pending`；引用的进度与记忆文件均存在。
- `git diff --check` 通过；仅有 Git 关于 LF/CRLF 转换的提示，没有 trailing whitespace 或错误。
- 三份文档 UTF-8/替换字符检查和长凭据模式扫描通过；没有调用真实平台、没有重跑测试、没有修改源码。
- 当前工作区只保留本任务的两个文档修改、外部记忆文件和原有两个无关 BYOK 未跟踪文件；未回退任何其他内容。
- 本任务完成；下一次应从计划 P0-A 开始，而不是回到旧版全量骨架建设。

## Checkpoint P0-A-1：Session 生命周期修复与前端专项回归

- 已确认根因：路由模块级 `sessionChecked` 首次导航后永久为真，Session 状态变化无法通知路由重新检查。
- 前端修复：移除路由模块级标志；Session 状态模块新增内存态 `needsCheck`，由初始检查、登录、登出和 `expire()` 驱动，路由守卫只消费该状态，不建立缓存或持久化。
- `check()` 现在同时尊重 Core 返回的 `authenticated`；即使内存中仍有 CSRF token，`readonly` 阶段的 `canWrite` 仍为 false。
- 新增 `frontend/tests/session.test.ts`，覆盖登出清理 `csrfToken/expiresAt`、401/expire 清理、readonly+token 不可写，以及登出后再次登录导航触发新的 session check。
- 已执行：`Set-Location 'H:\tiktok\frontend'; npm test -- --run tests/session.test.ts` → 1 个文件、4 个测试通过；`Set-Location 'H:\tiktok\frontend'; npm run typecheck` → 通过。
- 当前未执行前端全量构建；待后端边界回归完成后再按安全范围汇总验证。

## Checkpoint P0-A-2：Collector 图片边界与安全窄测

- 新增 `backend/tests/test_collector_http_boundary.py::test_unsigned_image_route_is_rejected_before_lookup`；使用现有 `_collector_database(tmp_path)` 与 ASGI transport，直接请求 `/internal/v1/images/{uuid}`，不带 HMAC 时稳定返回 `401 / INTERNAL_AUTHENTICATION_FAILED`，未写固定路径或图片缓存。
- 已执行：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py -k unsigned_image_route` → `1 passed, 6 deselected, 1 warning`。
- 已执行：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m ruff check tests/test_collector_http_boundary.py` → `All checks passed!`。
- 已执行：`Set-Location 'H:\tiktok\frontend'; npm test` → `5 passed` 个测试文件、`10 passed` 个测试；前端 typecheck 仍通过。
- `git diff --check` 通过；仅有既有 LF/CRLF 转换提示。未创建 Git commit，未运行会清理目录、删除固定磁盘文件或触碰缓存类测试的命令。
- 最终精确复跑：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py::test_unsigned_image_route_is_rejected_before_lookup` → 1 passed、1 个既有 warning。
- 首次误在仓库根目录执行 npm 命令因无 `package.json` 得到 `ENOENT`；随后显式切换到 `frontend` 目录重跑成功，未将该失败计入通过数。

## Checkpoint P0-A-3：阶段收口与计划推进

- 使用仓库内虚拟环境复跑完整 Collector HTTP 边界文件：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -q tests/test_collector_http_boundary.py` → `7 passed, 1 warning`。
- 本次复跑只使用测试自身的 `tmp_path` 与 ASGI transport；没有固定磁盘目录、缓存业务测试、清理命令或真实网络调用。
- 主计划已将 P0-A 标记为完成，`p0-trustworthy-baseline` 聚合状态仍为 `in_progress`；最近下一步推进为 P0-B 订单与工具一致性。
- 提交范围只包含计划、进度、外部记忆和 P0-A 源码/测试；两个既有 BYOK 未跟踪文件继续排除。

## Checkpoint P0-B-1：订单行字段 presence 语义

- 数据流已改为显式语义：`normalize_order` 在上游 payload 中区分 `line_items/items` 缺失与存在，并由 `NormalizedOrder.lines_present` 将该事实带到持久化边界；哈希载荷也包含 presence，不再从空元组猜测字段是否出现。
- Repository 仅按领域命令执行：缺字段时仍更新订单摘要但保留已存订单行与 `item_count`；明确空数组时清空订单行并把数量置零；存在非空数组时完整替换订单行。详情同步时间仍按详情请求更新，不再决定是否删除订单行。
- 新增三项原始 payload → 领域对象 → SQLite 回归，覆盖缺字段保留、明确空数组清空、`items` 正常列表替换；现有正常详情写入回归继续通过。
- 已执行：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py::test_order_update_without_line_field_preserves_stored_lines tests/test_order_features.py::test_order_update_with_explicit_empty_lines_clears_stored_lines tests/test_order_features.py::test_order_update_with_present_lines_replaces_stored_lines` → `3 passed in 0.81s`。
- 已执行：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py` → `9 passed in 1.05s`。
- 两次 pytest 均显式禁用 cacheprovider；只使用现有内存 SQLite 测试工厂，没有固定临时路径、清理命令、缓存类测试或真实网络调用。

## Checkpoint P0-B-2：翻译离线 HTTP E2E

- 新增配置成功 HTTP E2E：真实穿过管理员 Session cookie、CSRF、幂等登记、Tools 路由、`AzureTranslator`、审计与稳定响应；Azure 边界使用 `httpx.MockTransport` 受控响应，明确是离线 E2E，不是 live Azure 验收。
- 成功断言覆盖 Provider 请求契约、200 稳定响应、幂等记录进入 `ACTIVE`、Provider request id 与 `translation.succeeded` 审计；失败断言覆盖 500 上游映射为稳定 502、幂等记录进入 `FAILED`，且密钥、源文本、原始上游正文均不进入 HTTP 或审计。
- `value_api` 测试夹具改为 pytest `tmp_path` + 自定义测试 lifespan 创建 SQLite；移除项目 `data` 下 UUID 文件、手动 WAL/SHM unlink 清理，不引入固定路径或目录清理。
- 首次执行精确节点得到 `2 errors, 1 warning`：应用生产路径解析器拒绝位于项目外的 pytest `tmp_path`；随后改为测试 lifespan 直接以 `tmp_path` 创建受控数据库并复跑。该失败没有调用 Provider 或真实网络，也没有执行清理命令。
- 已执行：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_value_tools.py::test_translation_http_e2e_succeeds_with_controlled_azure_transport tests/test_value_tools.py::test_translation_http_e2e_fails_closed_without_upstream_leakage` → `2 passed, 1 warning in 1.67s`；warning 为既有 Starlette/httpx TestClient 弃用提示。

## Checkpoint P0-B-3：ToolsView 翻译交互

- 新增 `frontend/tests/tools-view.test.ts`，使用现有 Vue Test Utils/Vitest 与 `coreApi` 窄 spy；不建立 Mock Server、不持久化数据、不启动前端服务器。
- 成功交互覆盖能力加载、可写 Session、textarea 多行去空白、表单提交参数、翻译结果和 Provider Request ID 展示；失败交互覆盖稳定错误码/请求 ID 提示，并确认失败后不渲染翻译结果。
- 现有 `ToolsView.vue` 行为在可控组件交互中可复现为正确，无需修改生产实现。
- 已执行：`Set-Location 'H:\tiktok\frontend'; npm test -- --run tests/tools-view.test.ts` → `1 passed` 个测试文件、`2 passed` 个测试，耗时 `2.67s`。

## Checkpoint P0-B-4：窄范围验证与安全检查

- 工具相关复跑：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_value_tools.py -k "not test_azure_translator_uses_verified_v3_contract_without_caching"` → `14 passed, 1 deselected, 1 warning in 1.85s`；唯一 deselected 是名称明确的缓存类测试，按本任务禁令未运行。
- 目标静态检查：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m ruff check --no-cache app/domain/orders.py app/repositories/orders.py tests/test_order_features.py tests/test_value_tools.py tests/test_api_routes.py` → `All checks passed!`。
- 前端首次 `npm run typecheck` 发现新测试的 `ShopSummary` 测试数据与 `ElMessage` 返回类型共 3 个类型错误；修正测试数据与 mock 类型后，`npm test -- --run tests/tools-view.test.ts` → `1 file / 2 tests passed`（`2.40s`），`npm run typecheck` → 通过。
- `git diff --check -- <P0-B 文件>` 通过，仅有既有 LF→CRLF 提示；本任务文件的 UTF-8 替换字符与常见长凭据模式扫描均无命中。工作区范围检查只见 P0-B 文件及两个既有无关 BYOK 未跟踪文件，后两者未读取、修改、暂存或删除。
- 未执行后端全量 pytest、前端全量 Vitest、前端 build，也未执行 `test_azure_translator_uses_verified_v3_contract_without_caching`；原因是严格排除缓存类测试、清理风险与 build，当前只声明窄范围验证结果。

## Checkpoint P0-B-5：文档与计划同步

- `docs/progress-review-2026-08-06.md` 已追加 P0-B 实际状态、全部修改文件、三态数据语义、成功/失败命令、未执行项、warning、live 验收边界和下一步。
- 主计划已将 P0-B 标记为“已完成（离线/窄范围验证）”，关闭订单行误清空、翻译 HTTP E2E 与 ToolsView 交互风险；frontmatter 的 `p0-trustworthy-baseline` 仍保持 `in_progress`。
- 最近下一步已推进到 P0-C：补完整空值环境模板，并重新生成妙手只读查询的可复核脱敏证据或记录可操作阻塞。
- 文档同步后最终 `git diff --check` 通过，仅有既有 LF→CRLF 提示；最终工作区只包含 P0-B 文件与两个既有无关 BYOK 未跟踪文件，任务文件替换字符/常见长凭据扫描仍无命中。

## Checkpoint P0-B-6：父级审阅回归封口

- 父级审阅发现：首版条件仅检查 `lines_present`，使 `detail=False` 的列表 payload 若显式携带空 `items/line_items`，会错误删除此前持久化的完整详情行；若紧随其后的详情又缺少行字段，数据无法恢复。
- 已修正安全矩阵：详情上下文中，缺字段保留，字段存在则允许替换（包括空数组清空）；列表上下文中，仅非空行允许更新，显式空数组保留已有行。Repository 结合明确 `detail` 调用上下文、领域 presence 与规范化行执行，不读取原始平台 DTO。
- 新增 `test_order_list_empty_then_missing_detail_preserves_stored_lines`，按“已有完整详情 → 列表显式空数组 → 详情缺字段”顺序证明订单摘要继续更新，而原有行和 `item_count` 均保留。
- 精确复跑：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py::test_order_update_without_line_field_preserves_stored_lines tests/test_order_features.py::test_order_list_empty_then_missing_detail_preserves_stored_lines tests/test_order_features.py::test_order_update_with_explicit_empty_lines_clears_stored_lines tests/test_order_features.py::test_order_update_with_present_lines_replaces_stored_lines` → `4 passed in 1.41s`。
- 完整订单文件：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m pytest -p no:cacheprovider -q tests/test_order_features.py` → `10 passed in 1.65s`。
- 目标 Ruff：`Set-Location 'H:\tiktok\backend'; & 'H:\tiktok\backend\.venv\Scripts\python.exe' -m ruff check --no-cache app/repositories/orders.py tests/test_order_features.py` → `All checks passed!`。只使用现有内存 SQLite；未运行缓存类测试、固定磁盘清理、全量 pytest、真实网络或真实平台调用。
- 修复后 `git diff --check` 通过，仅有既有 LF→CRLF 提示；本次修复文件替换字符扫描无命中。

## Checkpoint P0-B-7：提交前父级复核

- 父级使用仓库内虚拟环境合并复跑订单完整文件与翻译成功/失败 HTTP 节点：`Set-Location 'H:\tiktok\backend'; .\.venv\Scripts\python.exe -m pytest -p no:cacheprovider -q tests/test_order_features.py tests/test_value_tools.py::test_translation_http_e2e_succeeds_with_controlled_azure_transport tests/test_value_tools.py::test_translation_http_e2e_fails_closed_without_upstream_leakage` → `12 passed, 1 warning in 1.88s`。
- 该命令显式禁用 pytest cacheprovider；订单测试只使用内存 SQLite，翻译测试只使用 pytest `tmp_path` 与 `httpx.MockTransport`，未运行真实网络、缓存类测试或固定路径清理。
- warning 仍为既有 Starlette/httpx TestClient 弃用提示；P0-B 代码与文档进入阶段性 Git 提交审阅。
