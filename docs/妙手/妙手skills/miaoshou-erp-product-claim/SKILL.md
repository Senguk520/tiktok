---
name: miaoshou-erp-product-claim
description: Claim products from Miaoshou ERP common collect box to a platform-specific collect box, including TikTok, TikTok Global, Ozon, Temu full-service, Temu semi-managed, Shopee, Shopee Global, and MercadoLibre. Use when the user mentions "认领", "认领到平台", "公共采集箱认领", "claim to", "move to platform", "transfer to platform", or wants to move common collect box products into a target platform collect box before platform-specific editing or publishing.
---

# Miaoshou ERP Product Claim

Claim products from the common collect box into a platform collect box. This is a write operation that creates or maps platform-specific collect box records.

## Typical User Requests

- "把商品 12345 认领到 TikTok"
- "把 12345,12346 从公共采集箱转到 Ozon 采集箱"
- "这几个公共采集箱商品认领到 Temu 半托管"
- "采集完这些货源后认领到 Shopee"
- "认领到 Shopee Global，后面走全球商品发布"
- "认领到美客多 / MercadoLibre，后面补类目属性再发布"
- "认领完成后给我返回平台采集箱 ID"
- "这些商品先认领到平台，后面再编辑类目属性"

## Scope

Use this skill only for cross-box transfer:

- From common collect box detail IDs to platform collect box detail IDs.
- Before platform-specific editing and publishing.
- After common collect box data has been checked or edited.

Do not use this skill to:

- Collect source URLs into the common collect box.
- Edit title, price, stock, images, SKU, category, or attributes.
- Publish products to shops.
- Decide a target platform when the user has not specified it.

## Safety Classification

Claiming is a write operation. It changes ERP workflow state and may create platform-specific collect box records. Always preview and require explicit confirmation before calling the claim API.

Do not combine claim and publish in one confirmation. They are separate write operations.

## Required Inputs

| Input | Required | Notes |
| --- | --- | --- |
| `detailIds` | Yes | Common collect box detail IDs |
| `platform` | Yes | Target API platform code |

`serialNumber` is an internal API compatibility field. Do not ask normal business users for it. Always use `serialNumber=1` unless a connector, developer instruction, or current API documentation explicitly supplies another value.

## Platform Codes

Current claim API documentation supports:

| Platform | Code | Notes |
| --- | --- | --- |
| TikTok Shop | `tiktok` | Normal TikTok shop |
| TikTok Global | `tiktokGlobal` | Preserve mixed-case code |
| Ozon | `ozon` | Ozon platform collect box |
| Temu full-service | `pddkj` | Full-managed Temu |
| Temu semi-managed | `pddkjChoice` | Preserve mixed-case code |
| Shopee | `shopee` | Normal Shopee |
| Shopee Global | `shopeeGlobal` | Preserve mixed-case code |
| MercadoLibre | `mercadolibre` | Meikeduo/MercadoLibre |

Legacy platform codes such as `lazada`, `amazon`, and `shein` may exist in older installed skills or historical workflows, but they are not in the current claim API reference used for this source skill. Ask for confirmation before using legacy platform codes.

Normalize user aliases in scripts, but preserve API code casing in final request payloads.

## API Authorization

Before calling any Miaoshou ERP Open Platform API, make sure the user has authorized an approved Open Platform app.

1. Load credentials from local `resources/config.json`, environment variables `MIAOSHOU_APP_KEY` and `MIAOSHOU_APP_SECRET`, or a secure host connector.
2. Optional: set `MIAOSHOU_BASE_URL`; default to `https://openapi-erp.91miaoshou.com`.
3. If the account enabled IP whitelist, confirm the caller host is allowed.
4. Send signed `POST` requests with `Content-Type: application/json`, `x-app-key`, `x-timestamp`, and `x-sign`.

Signing contract:

```text
sign = HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)
```

Use the exact API path, not the full URL. Use second-level Unix timestamp. Use the exact compact JSON string sent in the request body. Never print `AppSecret`, signed headers, or credential-bearing request data.

Common auth errors:

| Code | Meaning | Handling |
| --- | --- | --- |
| `signMissing` | Missing signed headers | Check request construction |
| `signExpired` | Timestamp expired or clock drift | Check system time and regenerate |
| `signInvalid` | Signature/path/body/secret mismatch | Check signing inputs without exposing secrets |
| `appNotFound` | App key invalid or app not approved | Verify app authorization |
| `appNoPermission` | App lacks endpoint permission | Ask user to enable endpoint permission |
| `ipNotInWhitelist` | Caller IP blocked | Add caller IP to whitelist |

## Standard Workflow

1. Parse common collect box `detailIds` and target `platform`.
2. If product IDs are missing, ask for them. If platform is missing, ask for the target platform.
3. Do not ask for `serialNumber`; use internal default `1`.
4. Query product detail for each common collect box ID when possible.
5. Show a product preview with ID, title, price/SKU count/image count when available.
6. Show a claim plan with product count, common IDs, target platform, transfer behavior, and explicit non-actions.
7. Execute the claim API only after explicit confirmation.
8. Report success, failure, and partial success separately.
9. Preserve the mapping from common collect box ID to platform collect box ID for downstream platform edit/publish workflows.
10. Route the user to the matching platform-specific skill only for successful items.

## Confirmation Template

```text
请确认认领计划：
- 商品数量：N
- 公共采集箱商品ID：12345, 12346
- 目标平台：tiktok / tiktokGlobal / ozon / pddkj / pddkjChoice / shopee / shopeeGlobal / mercadolibre
- 将迁移：标题、图片、SKU、价格、库存、基础属性
- 不会执行：平台采集箱编辑、类目属性补全、发布

确认后才会调用认领接口。请回复“确认认领”或“取消”。
```

## Scenario Handling

| User request | Expected behavior |
| --- | --- |
| "认领商品12345到TikTok" | Query/preview common detail, confirm, then claim with `platform=tiktok` and `serialNumber=1` |
| "把商品12345认领到Ozon" | Confirm `platform=ozon`, claim, then return Ozon collect box detail ID mapping |
| "认领到Temu半托管" | Use `platform=pddkjChoice`; preserve mixed-case API code |
| "认领到Temu" | Ask whether full-service `pddkj` or semi-managed `pddkjChoice` |
| "认领到Shopee" | Use `platform=shopee`; then route to Shopee collect box editing |
| "认领到Shopee Global" | Use `platform=shopeeGlobal` when the business/API contract supports global claim; preserve mixed-case API code |
| "认领到美客多" / "认领到MercadoLibre" | Use `platform=mercadolibre`; then route to MercadoLibre collect box editing |
| "把这批商品认领" | Ask for target platform |
| "把商品A和B认领" | Resolve or ask for actual common collect box detail IDs |
| "采集完再认领" | Use `miaoshou-erp-source-import` first, then claim successful detail IDs |
| Product already claimed | Report existing state and suggest checking platform collect box |

## API Summary

- Endpoint: `POST /open/v1/product/common_collect_box/common_collect_box/claimed`
- Request field: `detailSerialNumberPlatformList`
- Item fields: `detailId`, `platform`, `serialNumber`
- Default `serialNumber`: `1`
- Response field: `platformCollectBoxDetailIdMap`

Example request:

```json
{
  "detailSerialNumberPlatformList": [
    {
      "detailId": 12345,
      "platform": "ozon",
      "serialNumber": 1
    }
  ]
}
```

Example response mapping:

```json
{
  "platformCollectBoxDetailIdMap": {
    "ozon": {
      "12345": 67890
    }
  }
}
```

Load `references/api_reference.md` before implementing alternate callers or debugging endpoint behavior.

## CLI

```bash
python {base_dir}/scripts/claim_to_platform.py platforms
python {base_dir}/scripts/claim_to_platform.py detail --detail-id 12345
python {base_dir}/scripts/claim_to_platform.py claim --detail-ids 12345,12346 --platform ozon
python {base_dir}/scripts/claim_to_platform.py claim --detail-ids 12345 --platform pddkjChoice
python {base_dir}/scripts/claim_to_platform.py claim --detail-ids 12345 --platform shopee
python {base_dir}/scripts/claim_to_platform.py claim --detail-ids 12345 --platform shopeeGlobal
python {base_dir}/scripts/claim_to_platform.py batch-claim --file claim_list.json
```

## Failure Handling

| Error | Meaning | Safe response |
| --- | --- | --- |
| `productNotFound` | Product ID is invalid or unavailable | Ask user to confirm common collect box detail ID |
| `alreadyClaimed` | Product has already been claimed to the platform | Report existing state and suggest checking platform collect box |
| `invalidSerialNumber` | Internal serial number rejected by API | Report a technical configuration/API contract issue; do not ask normal users to guess this value |
| `platformInvalid` or platform-related error | Unsupported or wrong platform code | Re-check current platform table; for Temu ask full-service vs semi-managed |
| Signature/auth errors | App/signing/permission/whitelist issue | Check local config, permissions, and whitelist without exposing secrets |

For partial failures, show successful mappings and failed IDs separately. Do not retry failed writes automatically without user instruction.

## Related Skills

| Step | Skill |
| --- | --- |
| Query shops | `miaoshou-erp-shop-query` |
| Collect source URLs into common collect box | `miaoshou-erp-source-import` |
| Edit common collect box first | `miaoshou-erp-common-collectbox-manage` |
| Recommend TikTok category after TikTok claim | `miaoshou-erp-tiktok-category-recommend` |
| Edit TikTok collect box after TikTok claim | `miaoshou-erp-tiktok-product-edit` |
| Publish TikTok product after edit | `miaoshou-erp-tiktok-product-publish` |
| Recommend Ozon category after Ozon claim | `miaoshou-erp-ozon-category-recommend` |
| Edit Ozon collect box after Ozon claim | `miaoshou-erp-ozon-product-edit` |
| Publish Ozon product after edit | `miaoshou-erp-ozon-product-publish` |
| Recommend Temu full-service category after claim | `miaoshou-erp-temu-full-category-recommend` |
| Manage Temu full-service size chart after claim | `miaoshou-erp-temu-full-size-chart-manage` |
| Plan Temu full-service vehicle fitment after claim | `miaoshou-erp-temu-full-vehicle-fitment-manage` |
| Manage Temu full-service models after claim | `miaoshou-erp-temu-full-model-manage` |
| Edit Temu full-service collect box after claim | `miaoshou-erp-temu-full-product-edit` |
| Publish Temu full-service products after edit | `miaoshou-erp-temu-full-product-publish` |
| Recommend Shopee category after Shopee claim | `miaoshou-erp-shopee-category-recommend` |
| Manage Shopee size chart after Shopee claim | `miaoshou-erp-shopee-size-chart-manage` |
| Edit Shopee collect box after Shopee claim | `miaoshou-erp-shopee-collectbox-product-edit` |
| Manage Shopee Global online products | `miaoshou-erp-shopee-global-product-manage` |
| Publish Shopee products after edit | `miaoshou-erp-shopee-product-publish` |
| Recommend MercadoLibre category after MercadoLibre claim | `miaoshou-erp-mercadolibre-category-recommend` |
| Plan MercadoLibre size chart after MercadoLibre claim | `miaoshou-erp-mercadolibre-size-chart-manage` |
| Edit MercadoLibre collect box after MercadoLibre claim | `miaoshou-erp-mercadolibre-product-edit` |
| Publish MercadoLibre product after edit | `miaoshou-erp-mercadolibre-product-publish` |
| Other platform follow-up | Use the matching platform-specific edit/category/publish skill when available |

## Configuration

Use `resources/config.json.example` as the local configuration template. Do not distribute real `resources/config.json`.

## Script Usage Notes

- Script name: `claim_to_platform.py`
- Use `--platform`, not `--targetPlatform`.
- Use `--detail-ids` with common collect box detail IDs, not shop IDs.
- Use kebab-case CLI flags such as `--detail-ids`.
- Omit `--serial-number` in normal use; the script defaults it to `1`.
