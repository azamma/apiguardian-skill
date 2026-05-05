# Whitelist format

Each whitelist file is a JSON document with this shape:

```json
{
  "whitelist": {
    "<full-api-name>": [
      {"method": "POST", "path": "/oauth/token", "comment": "Public auth endpoint"},
      {"method": "GET",  "path": "/oauth/validate", "comment": "Signature-checked"}
    ]
  }
}
```

## Discovery

`generate_csv.py` scans `--whitelist-dir` for any file matching `whitelist_*.json`. The label between `whitelist_` and `.json` becomes the category that lands in the CSV.

The skill ships three suggested categories:

- `whitelist_PUBLIC_BY_DESIGN.json` — public by design (health checks, public webhooks).
- `whitelist_AUTH_IN_BACKEND.json` — the backend service handles auth itself.
- `whitelist_IP_RESTRICTED.json` — IP allowlist or network-restricted endpoints.

You are free to drop any number of additional `whitelist_<LABEL>.json` files — the script picks them up automatically and uses `<LABEL>` as the CSV value.

## Matching rules

- **Key**: must be the exact API name as it appears in AWS (and as Phase 1 named the folder). For example, `MyService-Public-PROD`.
- **Method**: case-insensitive exact match. If `method` is missing or empty, the entry is skipped — both fields are required.
- **Path**: literal match, except `*` matches a single path segment (no slashes).
  - `/oauth/token` matches only `/oauth/token`.
  - `/users/*/profile` matches `/users/123/profile`. Does NOT match `/users/123/profile/settings` (extra segment) or `/users//profile` (empty segment).
  - `/webhooks/*/callback` matches `/webhooks/foo/callback` and `/webhooks/bar/callback`. Does NOT match `/webhooks/foo/bar/callback`.
- **Comment**: descriptive only. Not used for matching but visible to humans reading the JSON.

## Multiple matches

If the same `(api, method, path)` matches entries in more than one whitelist file, all category labels appear in the CSV joined by `+`, e.g. `PUBLIC_BY_DESIGN+IP_RESTRICTED`. Within a single file, only the first matching entry per category is recorded.

## Tips

- When migrating from a path-only whitelist, expand each entry to `{method, path}` pairs. There is no "all methods" sentinel — list each method explicitly.
- Wildcards intentionally only match a single segment to avoid accidentally whitelisting deeper subtrees. If you genuinely need a subtree, list each segment depth or revisit whether the endpoint should really be whitelisted.
- The whitelist column doesn't change `is_authorized`. It explains *why* an unauthorized endpoint may still be acceptable; the security signal stays explicit.
