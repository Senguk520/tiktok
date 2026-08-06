# API Reference - Miaoshou ERP Shop Query

## Endpoint

`POST /open/v1/product/shop/shop/get_shop_list`

## Request

```json
{
  "platform": "ozon",
  "site": "OZON",
  "pageNo": 1,
  "pageSize": 100
}
```

Required fields from the OpenAPI document:

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `platform` | string | Yes | API platform code |
| `site` | string | Yes | Platform site/mode code |
| `pageNo` | integer | Yes | Minimum 1 |
| `pageSize` | integer | Yes | Maximum 100 |

`site` must be sent as a concrete platform site/mode code. The API specification does not define an empty string as an all-sites query. Iterate the documented site codes when broad discovery is required.

For Ozon, send the literal selector `site=OZON`. Do not translate the Russian marketplace to `RU`: this endpoint can return `result=success` with an empty `shopList` for `platform=ozon, site=RU`, even when the account has authorized Ozon shops.

## Platform Codes

| Platform | Code | Site examples |
| --- | --- | --- |
| TikTok normal | `tiktok` | `ID`, `VN`, `TH`, `MY`, `PH`, `BR`, `MX`, `ES`, `FR`, `GB`, `US`, `DE`, `IT`, `JP` |
| TikTok global | `tiktokGlobal` | `TIKTOKGLOBAL`, `TIKTOKGLOBALUS`, `TIKTOKGLOBALEU` |
| Temu full-service | `pddkj` | `PDDKJ` |
| Temu semi-managed | `pddkjChoice` | `PDDKJCHOICE` |
| Shopee | `shopee` | `ID`, `TW`, `VN`, `TH`, `MY`, `SG`, `PH`, `BR`, `MX`, `CL`, `CO`, `PL`, `ES`, `FR`, `AR` |
| Shopee global | `shopeeGlobal` | `SHOPEEGLOBAL` |
| MercadoLibre | `mercadolibre` | `CBT`, `UP` |
| Ozon | `ozon` | `OZON` only for this endpoint; do not use `RU` |

## Response Fields

`data.shopList[]`:

| Field | Meaning |
| --- | --- |
| `shopId` | Shop ID for platform edit/publish APIs |
| `site` | Site code |
| `siteName` | Human-readable site name |
| `shopNick` | Shop alias/name |
| `platform` | Platform code |
| `parentShopId` | Global parent shop ID when present |
| `isCb` | Whether cross-border |
| `isCnsc` | Whether global shop |
| `status` | Authorization/shop status |
| `gmtExpire` | Expiration time |
| `gmtLastAuth` | Last authorization time |

## Signing

Every call is signed with:

```text
sign = HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)
```

Use compact JSON for the request body and exactly the API path, not the full URL.

## Complete Discovery

For all-shop discovery:

1. Iterate every documented platform/site combination.
2. Request `pageSize=100` and increment `pageNo` until a page contains fewer than 100 shops.
3. Deduplicate by `(platform, site, shopId)`.
4. Preserve and report failed combinations; do not silently treat partial results as complete.
