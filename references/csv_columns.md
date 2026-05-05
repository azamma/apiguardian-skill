# CSV column derivation

Column-by-column rules used by `generate_csv.py`.

| Column | Source | Derivation |
|--------|--------|------------|
| `api` | `<input-dir>/<api-name>/` directory name | The folder produced by Phase 1 — typically the full AWS API name like `MyService-Public-PROD`. |
| `method` | `raw_snapshot.json` resource method key | Excludes `OPTIONS` (CORS preflight). |
| `path` | `raw_snapshot.json` resource `path` | Verbatim from AWS API Gateway. |
| `authorization_type` | resource method `authorizationType` | Possible values: `NONE`, `COGNITO_USER_POOLS`, `CUSTOM`, `AWS_IAM`. |
| `authorizer_name` | `raw_authorizers.json` lookup by `authorizerId` | Empty when no authorizer is attached → emitted as `NONE`. |
| `api_key` | `apiKeyRequired` boolean | `YES`/`NO`. |
| `is_authorized` | derived | `YES` when `authorization_type ∈ {COGNITO_USER_POOLS, CUSTOM, AWS_IAM}` OR `api_key == YES`. Else `NO`. |
| `whitelist` | whitelist directory | `NO` or one of the configured category labels (e.g. `PUBLIC_BY_DESIGN`, `AUTH_IN_BACKEND`, `IP_RESTRICTED`), or `+`-joined when an endpoint matches multiple whitelist files. See `whitelist_format.md`. |
| `endpoint_url` | `methodIntegration.uri` | Strip `https?://`, strip `${stageVariables.X}`, leave the path portion (e.g., `/my-service/v2/campaigns`). |

## Notes

- `OPTIONS` is intentionally excluded. It exists for CORS preflight only and is auto-generated for every authorized resource — including it would inflate counts and create false `NO`s on every row.
- `authorizer_name = NONE` is meaningful: it means there is no authorizer at all, not that a name lookup failed. If lookup fails on a present `authorizerId`, the row still gets `is_authorized = YES` (because the type check matches) but `authorizer_name` will be empty — surface this case in review.
- `endpoint_url` is intentionally lossy: callers typically only care about which backend path the request reaches, not which stage variable resolves to which host. Keep raw URI in the per-path Markdown if you need the original.
- The CSV is sorted by API directory order, then by the order resources are returned by AWS. Re-sort downstream if you need a stable comparison view.
