---
name: miaoshou-erp-shop-query
description: Query authorized shop lists from Miaoshou ERP JCOP Open Platform across TikTok, TikTok Global, Ozon, Temu full-service, Temu semi-managed, Shopee, Shopee Global, MercadoLibre, and other platforms. Use when the user mentions "店铺列表", "授权店铺", "查询店铺", "我的店铺", "shop list", "shop ID", "认领到哪个店铺", "发布到哪个店铺", or needs shopId/site/platform authorization status before claim, platform collect box edit, or publish workflows.
---

# Miaoshou ERP Shop Query

Query authorized shops and return the identifiers needed by downstream ERP workflows.

## Typical User Requests

- "帮我查一下 TikTok US 已授权店铺"
- "Ozon 有哪些可用店铺？"
- "查 Temu 半托管店铺 ID"
- "我有哪些 Shopee Global 店铺可以认领商品？"
- "查 Shopee MY 店铺，准备发布"
- "查美客多 / MercadoLibre 可用店铺"
- "发布前先帮我看一下可用店铺"
- "这个站点应该用哪个 shopId？"

## Scope

This skill is read-only. Use it to query shop IDs, shop names, platform codes, site codes, authorization status, expiration time, and global/CB/CNSC flags.

Do not use this skill to:

- Collect source URLs.
- Edit common or platform collect box products.
- Claim products.
- Publish products.
- Change shop authorization data.

Use this skill before claim, platform collect box edit, or publish workflows when shop selection or shop validity is unclear.

## Safety Rules

- Do not perform write operations from this skill.
- Do not ask the user to paste `app_secret`, cookies, tokens, passwords, or signed headers into chat.
- Load credentials only from local `resources/config.json`, environment variables, or a secure host connector.
- Do not print secrets or full signed request headers.
- If a shop is expired, disabled, unauthorized, or site-mismatched, present it as unavailable for write workflows.
- If platform or site is ambiguous, ask a concise clarification unless the user explicitly asks for all shops.

## Platform Codes

Use the API platform code exactly:

| Platform | Code | Common sites |
| --- | --- | --- |
| TikTok Shop | `tiktok` | `ID`, `VN`, `TH`, `MY`, `PH`, `BR`, `MX`, `ES`, `FR`, `GB`, `US`, `DE`, `IT`, `JP` |
| TikTok Global | `tiktokGlobal` | `TIKTOKGLOBAL`, `TIKTOKGLOBALUS`, `TIKTOKGLOBALEU` |
| Ozon | `ozon` | `OZON` (exact query selector; do not use `RU`) |
| Temu full-service | `pddkj` | `PDDKJ` |
| Temu semi-managed | `pddkjChoice` | `PDDKJCHOICE` |
| Shopee | `shopee` | `ID`, `TW`, `VN`, `TH`, `MY`, `SG`, `PH`, `BR`, `MX`, `CL`, `CO`, `PL`, `ES`, `FR`, `AR` |
| Shopee Global | `shopeeGlobal` | `SHOPEEGLOBAL` |
| MercadoLibre | `mercadolibre` | `CBT`, `UP` |

Legacy platform codes such as `lazada`, `amazon`, and `shein` may exist in older installed skills or historical workflows, but they are not in the current shop API reference used for this source skill. Ask for confirmation before using legacy platform codes.

Preserve mixed-case codes such as `tiktokGlobal`, `pddkjChoice`, and `shopeeGlobal`.

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

1. Parse requested platform and site.
2. Treat `site` as required for every API request. Never use an empty string as an all-sites sentinel.
3. If the user asks for all shops, query every documented platform/site combination, paginate each combination, and summarize failures as well as results.
4. If platform or site is missing for a specific query, ask a concise clarification, except for platforms with one fixed selector such as Ozon (`site=OZON`).
5. Query `/open/v1/product/shop/shop/get_shop_list`.
6. Present available shops first and unavailable shops separately.
7. Explain which identifier is needed next:
   - Platform edit and publish workflows usually use `shopId`.
   - Claim workflows use platform code and common collect box detail IDs; do not imply the user must provide `serialNumber`.
8. If the result set is large, group by platform/site and show page/size context.
9. Preserve normalized `CB`, `CNSC`, and `shopType` fields in raw output so Shopee publish workflows can split local shops from global child shops.
10. Report every failed platform/site/page query. Never present a partial broad-discovery result as complete.

## Response Standard

For each shop, include:

| Field | Why it matters |
| --- | --- |
| `shopId` | Primary shop identifier for platform edit/publish workflows |
| `shopNick` or shop name | Human-readable confirmation |
| `platform` | Prevents cross-platform mistakes |
| `site` / `siteName` | Prevents wrong-country or wrong-mode operations |
| `status` | Indicates whether the shop can be used |
| `gmtExpire` | Helps identify authorization risks |
| `gmtLastAuth` | Helps troubleshoot stale authorization |
| `parentShopId` | Useful for global or parent-child shop structures |
| `isCb` / `isCnsc` | Helps distinguish cross-border/global shop modes |
| `CB` / `CNSC` / `shopType` | Normalized downstream routing fields; `CB=Y` and `CNSC=Y` means Shopee global child flow |

If a connector returns extra fields such as `serialNumber`, report them only when a downstream API explicitly needs them. Do not ask ordinary users for `serialNumber` for public collect box claim.

## Scenario Handling

| Scenario | Expected behavior |
| --- | --- |
| User asks "有哪些店铺" without platform | Ask whether to query all common platforms or a specific platform |
| User asks for TikTok US shops | Query `platform=tiktok`, `site=US` |
| User asks for TikTok Global shops | Query `platform=tiktokGlobal` and matching global site |
| User asks for Temu shops | Ask whether full-service `pddkj` or semi-managed `pddkjChoice` |
| User asks for Ozon shops | Query `platform=ozon`, `site=OZON`. Treat `OZON` as the exact API selector even when the user says Russia/RU; never send `site=RU` to this endpoint |
| User asks for Shopee local shops | Query `platform=shopee` and the requested site, such as `MY`, `TW`, or `ID` |
| User asks for Shopee Global shops | Query `platform=shopeeGlobal`, `site=SHOPEEGLOBAL`; inspect `parentShopId`, `isCb`, and `isCnsc` for downstream global workflows |
| User is preparing to publish | Return `shopId` and remind that publishing requires separate confirmation in the publish skill |
| User is preparing to claim | Return platform/site/shop context; route actual claim to `miaoshou-erp-product-claim` |
| Shop is expired or disabled | Do not suggest it as a target; report the issue clearly |
| No shops found | Report queried platform/site and suggest checking ERP authorization |

## CLI

```bash
python {base_dir}/scripts/shop_list.py list --platform tiktok --site US --page 1 --size 100
python {base_dir}/scripts/shop_list.py list --platform ozon --site OZON
python {base_dir}/scripts/shop_list.py list --platform pddkjChoice --site PDDKJCHOICE
python {base_dir}/scripts/shop_list.py list --platform shopee --site MX --page 1 --size 50
python {base_dir}/scripts/shop_list.py list --platform shopeeGlobal --site SHOPEEGLOBAL --page 1 --size 50
python {base_dir}/scripts/shop_list.py list-platform-all --platform tiktok
python {base_dir}/scripts/shop_list.py list-all
```

If scripts are unavailable, call the endpoint through the host HTTP client or connector. Load `references/api_reference.md` when exact request/response fields are needed.

## Failure Handling

- `signMissing`: check signed headers.
- `signExpired`: check system time or timestamp seconds.
- `signInvalid`: verify signing path/body/config; do not expose secrets.
- `appNotFound`: verify app key and app approval state.
- `appNoPermission`: ask user to enable shop-list endpoint permission.
- `ipNotInWhitelist`: caller IP is not allowed by the account whitelist.
- Empty response: report possible service, network, or whitelist issue.
- Empty shop list: report queried platform/site and suggest checking shop authorization in ERP.
- Partial `list-all` result: return successful shops plus the failed platform/site/page combinations.
- Repeated pagination page: stop that site scan and report `repeatedPage` rather than looping indefinitely.

## Related Skills

| Step | Skill |
| --- | --- |
| Collect source URLs into common collect box | `miaoshou-erp-source-import` |
| Edit common collect box | `miaoshou-erp-common-collectbox-manage` |
| Claim to platform collect box | `miaoshou-erp-product-claim` |
| Edit TikTok collect box | `miaoshou-erp-tiktok-product-edit` |
| Publish TikTok products | `miaoshou-erp-tiktok-product-publish` |
| Edit Ozon collect box | `miaoshou-erp-ozon-product-edit` |
| Publish Ozon products | `miaoshou-erp-ozon-product-publish` |
| Recommend Temu full-service categories | `miaoshou-erp-temu-full-category-recommend` |
| Manage Temu full-service size charts | `miaoshou-erp-temu-full-size-chart-manage` |
| Plan Temu full-service vehicle fitment | `miaoshou-erp-temu-full-vehicle-fitment-manage` |
| Manage Temu full-service shop models | `miaoshou-erp-temu-full-model-manage` |
| Edit Temu full-service collect box | `miaoshou-erp-temu-full-product-edit` |
| Publish Temu full-service products | `miaoshou-erp-temu-full-product-publish` |
| Recommend Shopee category | `miaoshou-erp-shopee-category-recommend` |
| Manage Shopee size chart | `miaoshou-erp-shopee-size-chart-manage` |
| Edit Shopee collect box | `miaoshou-erp-shopee-collectbox-product-edit` |
| Manage Shopee Global online products | `miaoshou-erp-shopee-global-product-manage` |
| Publish Shopee products | `miaoshou-erp-shopee-product-publish` |
| Recommend MercadoLibre categories | `miaoshou-erp-mercadolibre-category-recommend` |
| Plan MercadoLibre size charts | `miaoshou-erp-mercadolibre-size-chart-manage` |
| Edit MercadoLibre collect box | `miaoshou-erp-mercadolibre-product-edit` |
| Publish MercadoLibre products | `miaoshou-erp-mercadolibre-product-publish` |
| Other platform follow-up | Use the matching platform-specific edit/category/publish skill when available |

## Configuration

Use `resources/config.json.example` as the local configuration template. Do not distribute real `resources/config.json`.

## Script Usage Notes

- Script name: `shop_list.py`.
- Use the `list` subcommand; do not pass `--platform` directly to the script root.
- Use `list-platform-all --platform tiktok` when the platform is known but the site is not; it scans only that platform.
- Use `list-all` only when the user asks for broad cross-platform discovery.
- The `list` command requires `--site`; an empty site is not an all-sites query.
- `list-all` scans every documented site, follows pagination, and returns `{shops, failures}` in raw output.
- Use `--page` and `--size` for large result sets.
- Preserve mixed-case platform codes in final API calls.
