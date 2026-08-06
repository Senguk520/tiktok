---
name: miaoshou-erp-tiktok-category-recommend
description: Independently recommend TikTok Shop leaf categories and required attribute values for Miaoshou ERP TikTok collect box products using product title, description, detail ID, site, shop IDs, category tree, and category metadata. Use when the user mentions "类目匹配", "AI类目推荐", "类目属性", "推荐属性值", "TikTok类目", "TK类目", category confidence, missing category attributes, or needs category guidance before editing, write-back, or publishing.
---

# Miaoshou ERP TikTok Category Match

Recommend TikTok Shop categories and attribute values. This skill must close the category recommendation task by itself: collect or request the required product context, query category data when credentials are available, fall back to user-provided product/category evidence when API access is missing, and return a structured recommendation that can be reviewed or handed to another workflow.

## Typical User Requests

- "帮这个 TikTok 商品匹配类目"
- "商品 12345 应该放哪个 TikTok 类目？"
- "给我推荐类目和必填属性"
- "这个标题适合哪个 TK 类目？"
- "帮我看这个商品还缺哪些类目属性"
- "推荐结果确认后帮我写回采集箱"

## Scope

Use this skill to:

- Collect missing product, site, shop, and authorization inputs.
- Query TikTok category tree by site.
- Select likely leaf category candidates.
- Query category metadata.
- Recommend required and optional attributes.
- Explain missing information before editing or publishing.
- Produce a standalone final recommendation even when write-back or publish skills are not installed.

Do not save product data directly from this skill. If the user wants to apply the recommendation and a write-back skill or connector is available, hand off the confirmed category and attribute payload. If that skill or connector is unavailable, clearly tell the user what capability is missing and still provide the exact category/attribute result to copy into the next step.

## Safety Rules

- Treat category and attribute outputs as recommendations until user confirms.
- Do not invent regulated values, product certifications, manufacturer data, warehouse, or EU responsible person information.
- Required attributes and optional attributes must be separated.
- If site or shop IDs are missing, ask for them or use shop-list guidance.
- If Miaoshou Open Platform credentials are missing, ask the user to provide or configure them instead of declaring the skill unusable.
- Prefer leaf categories. If uncertain, present multiple candidates with reasons.
- Do not print secrets or signed headers.

## Inputs

The product context may come from either source:

| Input mode | Required data |
| --- | --- |
| Existing TikTok collect box product | `detailId`, mode, site, shop IDs |
| User-provided content | title, description, site, shop IDs |

If only a title is available, ask for product description or key attributes when needed.

## API Authorization

Before calling any Miaoshou ERP Open Platform API, make sure the customer has authorized the skill with an approved Open Platform app. Missing credentials are a recoverable input gap, not a reason to abandon the task.

1. Ask the customer to log in to Miaoshou ERP, open 「开放平台」, create an app, submit it for review, and use it only after approval.
2. Obtain the app credentials: `AppKey` and `AppSecret`. Prefer local configuration, environment variables, or a secure host connector. If no secure channel is available and the user chooses to provide credentials in chat, ask only for the minimum needed values, do not echo `AppSecret`, and do not include it in final answers, logs, or examples.
3. Configure credentials in one of these ways:
   - Copy `resources/config.json.example` to `resources/config.json` and fill `app_key` and `app_secret`.
   - Or set environment variables `MIAOSHOU_APP_KEY` and `MIAOSHOU_APP_SECRET`. Optional: set `MIAOSHOU_BASE_URL`, otherwise use `https://openapi-erp.91miaoshou.com`.
4. If the customer enabled the account-level IP whitelist, confirm the machine or host running this skill is in that whitelist. The whitelist is shared by all apps under the same Miaoshou account.
5. Every API request must be `POST` with `Content-Type: application/json` and signed request headers: `x-app-key`, `x-timestamp`, and `x-sign`.

Signing contract from the Open Platform quick-start:

```text
base_url = https://openapi-erp.91miaoshou.com
sign = HmacSHA256(appSecret, appSecret + path + timestamp + appKey + bodyJson + appSecret)
```

Important details:

- `path` is only the API path, for example `/open/v1/order/create`. Do not include the domain or query string in the signature content.
- `timestamp` is a seconds-level Unix timestamp. Requests expire after 300 seconds of clock drift.
- `bodyJson` must be the exact JSON string sent in the POST body; use an empty string only when there is no body.
- `x-sign` is lowercase hex HmacSHA256 output.
- Never print `AppSecret`, signed headers, or full credential-bearing requests in the final answer or logs.

If authorization fails, handle these quick-start codes explicitly: `signMissing` means missing headers, `signExpired` means local clock or seconds timestamp problem, `signInvalid` means signature/body/path/secret mismatch, `appNotFound` means the app key is wrong, disabled, or not approved, `appNoPermission` means the app lacks endpoint permission, and `ipNotInWhitelist` means the caller IP is not allowed.

## Missing Capability Handling

When a required capability is unavailable, continue with the best closed-loop response:

| Missing capability | Required response |
| --- | --- |
| Open Platform credentials | Ask the user for `AppKey`, `AppSecret`, base URL if non-default, and whether credentials should be supplied by config, environment variables, connector, or chat. Do not ask for unrelated secrets. |
| Network/API access | Explain that live category tree and metadata cannot be queried, then ask the user to provide category tree/metadata exports or enough product context for a conservative recommendation. |
| Bundled script cannot run | Use the host HTTP client/connector with the API contract in this skill, or ask the user to run the CLI locally. The script uses only Python standard library modules. |
| Related write-back/shop/publish skill | Tell the user which optional skill is missing, then output the confirmed recommendation payload so the category task remains complete. |
| LLM endpoint | Continue without the configured LLM. Use product evidence and retrieved metadata to rank candidates; mark lower confidence instead of failing. |

## Standard Workflow

1. Gather product title, description, images/keywords if available, site, and shop IDs.
2. Check whether credentials/API access are available. If not, ask for the missing credentials or request category/metadata input from the user.
3. If `detailId` is provided and API access is available, query collect box detail first. If not, ask the user for title, description, images/keywords, and known attributes.
4. Query category tree for the site when possible.
5. Use product information to shortlist top leaf category candidates.
6. Query metadata for each candidate category when possible.
7. Recommend required attribute values and useful optional values.
8. Mark uncertain values, missing user-provided data, and API/metadata gaps.
9. Present a final recommendation that can be reviewed before editing.

## Recommendation Output

For each candidate category, include:

- Rank.
- Category path.
- `cid`.
- Why it matches.
- Confidence level.
- Required attributes and suggested values.
- Optional attributes worth filling.
- Missing or uncertain values.
- Whether it is ready to apply.

## Scenario Handling

| Scenario | Expected behavior |
| --- | --- |
| User provides `detailId` | Fetch product detail, then recommend category and attributes |
| User provides only title/description | Ask for site and shop IDs before querying metadata |
| Credentials are missing | Ask for credentials/config method, or ask the user to provide product/category metadata for offline recommendation |
| Category metadata cannot be fetched | Recommend from available category evidence, clearly mark metadata-dependent attributes as unknown |
| Product fits multiple categories | Return top candidates with trade-offs; do not force one |
| Required compliance data is missing | Ask user to provide or query corresponding list; do not invent |
| User asks to save recommendation | Confirm exact category/attributes. If edit skill/connector exists, hand off; otherwise tell the user it is unavailable and provide the payload |

## CLI

If bundled scripts are available:

```bash
python {base_dir}/scripts/tiktok_category_match.py match --detail-id 12345 --mode site --site US --shop-ids 1001
python {base_dir}/scripts/tiktok_category_match.py match --title "women dress" --description "summer floral dress" --site US --shop-ids 1001
python {base_dir}/scripts/tiktok_category_match.py tree --site US
python {base_dir}/scripts/tiktok_category_match.py attributes --site US --cid 12345 --shop-ids 1001
```

If scripts are unavailable, call category tree and metadata endpoints through the host HTTP client or connector.

## Business Notes

- Category selection should prefer the most specific valid leaf category.
- Attribute suggestions should be conservative when product evidence is weak.
- Do not create fake brand, certification, material, origin, or compliance values.
- If the metadata says a field is required, surface it clearly even if no value can be recommended.

## Related Skills

These skills are optional follow-on capabilities, not prerequisites for category recommendation. If one is missing, say so and keep the recommendation result usable.

| Next step | Skill |
| --- | --- |
| Apply confirmed category/attributes | `miaoshou-erp-tiktok-product-edit` |
| Find target shop IDs | `miaoshou-erp-shop-query` |
| Publish after data is complete | `miaoshou-erp-tiktok-product-publish` |

## Configuration

Use `resources/config.json.example` as the template for local configuration. Do not distribute real `resources/config.json`.

## Script Usage

LLM common mistakes:

| Wrong | Correct | Note |
| --- | --- | --- |
| Omit `--shop-ids` | Add `--shop-ids <shopIds>` | `--shop-ids` is required even when using `--detail-id` to fetch product information. |
| Use script name `miaoshou_tiktok_category_recommend.py` | Use `tiktok_category_match.py` | The script name is `tiktok_category_match.py`. |

Correct examples:

```bash
python {base_dir}/scripts/tiktok_category_match.py match --detail-id 3134788019 --site MX --shop-ids 13302632 --mode site
python {base_dir}/scripts/tiktok_category_match.py match --title "产品标题" --site US --shop-ids 1001,1002
```
