# API Reference - Claim Common Collect Box Products

## Endpoint

`POST /open/v1/product/common_collect_box/common_collect_box/claimed`

## Request

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

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `detailSerialNumberPlatformList` | array | Yes | Maximum 100 items |
| `detailId` | integer | Yes | Common collect box detail ID |
| `platform` | string | Yes | Target platform code |
| `serialNumber` | integer | Yes | Use `1` by default |

## Platform Codes

Use these target codes:

- `tiktok`
- `tiktokGlobal`
- `ozon`
- `pddkj`
- `pddkjChoice`
- `shopee`
- `shopeeGlobal`
- `mercadolibre`

## Response

Success response contains:

```json
{
  "data": {
    "platformCollectBoxDetailIdMap": {
      "ozon": {
        "12345": 67890
      }
    }
  }
}
```

Interpretation:

- Outer key: platform code.
- Inner key: common collect box detail ID.
- Inner value: platform collect box detail ID.

Preserve this mapping for downstream platform editing and publishing.
