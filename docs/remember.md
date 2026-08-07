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
