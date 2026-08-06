---
name: miaoshou-erp-common-collectbox-manage
description: "查询、新增、编辑、删除妙手 ERP 公共采集箱商品。Use when the user wants CRUD operations on Miaoshou ERP common collect box products, including 商品查询, 公共采集箱, 采集箱商品, 新增商品到采集箱, 编辑采集箱, 修改标题/价格/库存/重量/图片/SKU, 批量改价, 批量改库存, 删除采集箱商品, or preparing common collect box products before claiming to platform collect boxes such as TikTok, Ozon, Shopee, Shopee Global, Temu, or MercadoLibre."
---

# Miaoshou ERP Common Collect Box CRUD

Use this skill to manage products that already live in the Miaoshou ERP common collect box. It supports read-only queries plus create, edit, and delete operations through the Miaoshou ERP Open Platform.

## First-Use Readiness Check

Before running any API operation, verify the local runtime and credentials:

```bash
cd {base_dir}
python -m pip install -r requirements.txt
python scripts/collectbox_crud.py doctor
```

If `python` is not available, ask the user to install Python or use an available Python runtime path, then rerun the same commands with that executable.

Configure credentials in one of these ways:

- Copy `resources/config.json.example` to `resources/config.json` and fill `app_key` and `app_secret`.
- Or set `MIAOSHOU_APP_KEY` and `MIAOSHOU_APP_SECRET` in the environment.
- Optionally set `MIAOSHOU_BASE_URL`; otherwise the script uses `https://openapi-erp.91miaoshou.com`.

After local checks pass, optionally run a read-only connectivity check:

```bash
python scripts/collectbox_crud.py doctor --check-api
```

Do not ask users to paste `AppSecret` into chat. Use local config, environment variables, or a secure host connector.

## Scope

Use this skill for common collect box records only.

Supported operations:

- Query product list or product detail.
- Create a common collect box product.
- Edit SPU fields such as title, price, stock, weight, dimensions, images, and notes.
- Edit SKU fields such as SKU price, stock, item number, weight, and property values.
- Delete products or remove selected product assets when supported by the API.

Do not use this skill to collect supplier/source URLs, edit platform-specific collect boxes after claim, or publish products. Route source URL collection to `miaoshou-erp-source-import` when available. For Shopee after claim, route platform-specific work to Shopee category, size chart, collect box edit, global product manage, or publish Skills.

## Safety Classification

| Operation | Safety level | Requirement |
| --- | --- | --- |
| Query | Read-only | Run after ID/scope is clear |
| Create | Write | Preview and explicit confirmation required |
| Edit | Write | Query current values, validate risks, preview, explicit confirmation required |
| Delete | Destructive | Exact IDs/count and explicit confirmation required |

## Core Safety Rules

- Never execute create, edit, or delete from a vague instruction.
- Query current product detail before every edit or delete.
- Show current value, target value, affected fields, and validation risks before writing.
- For batch operations, show item count and affected product IDs.
- For delete operations, require confirmation that includes the exact product IDs.
- Do not print signed headers, `AppSecret`, or credential-bearing requests.
- Stop on API errors and report partial success/failure clearly.

## Standard Workflow

1. Classify the request as query, create, edit, delete, or batch operation.
2. Collect required IDs, target fields, and target values.
3. Run the readiness check if this is the first use or if the environment changed.
4. Query current product detail for every affected item before writes.
5. Build a change plan with product ID, title, current values, target values, affected fields, and risks.
6. Ask for explicit confirmation before create/edit/delete.
7. Execute the script or API call.
8. Report results per product, separating success, skipped, and failed items.

## Business Rules

### Query Related Fields Before Save

Do not query only the field the user wants to change. Some API validations depend on related fields.

| User wants to edit | Also inspect |
| --- | --- |
| SPU `price` | SPU `stock`, SKU stock |
| SPU `stock` | SPU price, SKU stock |
| SKU price | SKU stock, original stock |
| SKU stock | SKU price, original stock |
| SKU fields | Complete SKU object, not only changed field |

Known validation rule: stock values above `99999` may block save operations. If current SPU/SKU/original stock exceeds this limit, present the risk and ask whether to correct, skip, or cancel.

### Preserve Complete SKU Records

When editing `skuMap`, do not submit only the changed field. Preserve all required SKU fields returned by the detail API.

Correct shape:

```json
{
  "price": 71.28,
  "stock": 99999,
  "oriPrice": 80.0,
  "oriStock": 99999,
  "itemNum": "SKU-RED-S",
  "weight": 0.3
}
```

Incorrect shape:

```json
{
  "price": 71.28
}
```

### Present Risks Once

After querying product detail, summarize all detected risks in one preview:

- Current values.
- Proposed values.
- Related field risks.
- Fields that will be preserved or corrected.
- Confirmation options.

## API Authorization

Before calling the Miaoshou ERP Open Platform API, confirm the customer has an approved Open Platform app.

1. Customer creates and submits an app in Miaoshou ERP Open Platform.
2. Customer provides `AppKey` and `AppSecret` through local config or a secure connector.
3. If account-level IP whitelist is enabled, confirm the current machine or host is allowlisted.
4. Every API request must be `POST` with `Content-Type: application/json` and signed headers: `x-app-key`, `x-timestamp`, and `x-sign`.

Signing contract:

```text
base_url = https://openapi-erp.91miaoshou.com
sign = HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)
```

Important details:

- `path` is only the API path, for example `/open/v1/order/create`.
- `timestamp` is a seconds-level Unix timestamp. Requests expire after 300 seconds of clock drift.
- `bodyJson` must be the exact JSON string sent in the POST body.
- `x-sign` is lowercase hex HmacSHA256 output.

Handle these auth errors explicitly: `signMissing`, `signExpired`, `signInvalid`, `appNotFound`, `appNoPermission`, and `ipNotInWhitelist`.

## CLI

Use `scripts/collectbox_crud.py` for deterministic operations:

```bash
python scripts/collectbox_crud.py doctor
python scripts/collectbox_crud.py doctor --check-api
python scripts/collectbox_crud.py list --status noClaimed --page 1 --size 20
python scripts/collectbox_crud.py detail --id 12345
python scripts/collectbox_crud.py add --data '{"title":"Test Product","price":19.99,"stock":100}'
python scripts/collectbox_crud.py edit --id 12345 --data '{"price":69.8}'
python scripts/collectbox_crud.py delete --ids 12345,12346
```

If bundled scripts are unavailable, call the documented API endpoints through the host HTTP client or connector.

## Confirmation Templates

Edit confirmation:

```text
请确认修改计划：
- 商品ID：12345
- 标题：示例商品
- 修改字段：price
- 当前值：64.80
- 目标值：69.80
- 关联检查：SPU库存=99999，SKU库存未超限
- 不修改字段：标题、图片、类目、SKU属性

确认后才会写入。请回复“确认修改”或“取消”。
```

Delete confirmation:

```text
请确认删除计划：
- 删除商品数：3
- 商品ID：12345, 12346, 12347
- 操作结果：从公共采集箱删除，可能不可撤销

请回复“确认删除 12345,12346,12347”后再执行。
```

## API Reference

Detailed endpoint behavior, field definitions, and payload structures are in `references/api_reference.md`. Load it before non-trivial create, edit, SKU, or delete operations.

## Failure Handling

- Validation error: identify the blocking field and current value.
- Partial batch failure: report successful, failed, and skipped products separately.
- Stock limit error: suggest correcting over-limit stock or skipping the affected SKU/product.
- Signature/auth errors: check local config or connector; do not expose secrets.
- Unknown API response: stop and show the upstream message; do not continue with additional writes.

## Related Skills

| Next step | Skill |
| --- | --- |
| Collect source URLs into common collect box | `miaoshou-erp-source-import` |
| Claim edited products to platform | `miaoshou-erp-product-claim` |
| Edit TikTok platform collect box after claim | `miaoshou-erp-tiktok-product-edit` |
| Publish TikTok products | `miaoshou-erp-tiktok-product-publish` |
| Recommend Ozon category after claim | `miaoshou-erp-ozon-category-recommend` |
| Edit Ozon platform collect box after claim | `miaoshou-erp-ozon-product-edit` |
| Publish Ozon products | `miaoshou-erp-ozon-product-publish` |
| Recommend Temu full-service categories after claim | `miaoshou-erp-temu-full-category-recommend` |
| Manage Temu full-service size charts after claim | `miaoshou-erp-temu-full-size-chart-manage` |
| Plan Temu full-service vehicle fitment after claim | `miaoshou-erp-temu-full-vehicle-fitment-manage` |
| Manage Temu full-service models after claim | `miaoshou-erp-temu-full-model-manage` |
| Edit Temu full-service platform collect box after claim | `miaoshou-erp-temu-full-product-edit` |
| Publish Temu full-service products | `miaoshou-erp-temu-full-product-publish` |
| Recommend Shopee category after claim | `miaoshou-erp-shopee-category-recommend` |
| Manage Shopee size chart after claim | `miaoshou-erp-shopee-size-chart-manage` |
| Edit Shopee platform collect box after claim | `miaoshou-erp-shopee-collectbox-product-edit` |
| Manage Shopee Global online products | `miaoshou-erp-shopee-global-product-manage` |
| Publish Shopee products | `miaoshou-erp-shopee-product-publish` |
| Recommend MercadoLibre categories after claim | `miaoshou-erp-mercadolibre-category-recommend` |
| Plan MercadoLibre size charts after claim | `miaoshou-erp-mercadolibre-size-chart-manage` |
| Edit MercadoLibre collect box after claim | `miaoshou-erp-mercadolibre-product-edit` |
| Publish MercadoLibre products | `miaoshou-erp-mercadolibre-product-publish` |
