---
name: apiguardian-skill
description: AWS API Gateway documentation, security auditing, path provisioning, Spring Boot synchronization, and deprecated-endpoint cleanup. Generates per-path Markdown documentation, a CSV security report, creates new resources matching the existing API's patterns, syncs missing Spring endpoints to API Gateway, and detects + deletes deprecated endpoints (those in API Gateway but not in code). Use this skill whenever the user wants to audit, document, scan, inventory, sync, clean up, or add endpoints to AWS API Gateway — for a specific REST API or all APIs in an account. Also trigger when the user asks to provision a new path, create an endpoint, sync endpoints, find orphan/deprecated endpoints, clean up the API surface, or check authorizers, integrations, whitelists, or compliance.
---

# APIGuardian Skill

Documentation, security auditing, and lifecycle management for AWS API Gateway, designed so an LLM can operate on a small slice of an API at a time instead of loading one giant dump.

The skill ships seven Python scripts that wrap the AWS CLI. Run them — don't issue raw AWS API Gateway commands — so behavior stays deterministic, reviewable, and reusable across organizations.

## What it can do

| Phase | Script | Purpose |
|-------|--------|---------|
| 1. Docs | `generate_docs.py` | Pull an API definition and write a hierarchical Markdown tree (`INDEX.md` + one file per resource path). |
| 2. CSV  | `generate_csv.py`  | Read the docs tree (and optional whitelists) and produce a deterministic security audit CSV. |
| 3. Create | `create_path.py` | Add a new resource + method to an existing API, inferring authorizer / integration / headers from a sibling method. |
| 4. Sync | `sync_endpoints.py` | Diff a Spring Boot project against the live API and create the missing endpoints. |
| 5. Deprecated | `report_deprecated.py` | List endpoints in API Gateway that no longer exist in the Spring code. |
| 6. Cleanup | `cleanup_endpoints.py` | Read the deprecated report and delete the listed endpoints (with confirmations and an audit trail). |
| Helper | `scan_spring_endpoints.py` | Pure scanner used by the sync/deprecated scripts to extract `@*Mapping` annotations from a Spring Boot project. |

## When to use this skill

Use whenever the user asks to:

- Audit, scan, document, or inventory AWS API Gateway endpoints.
- Generate a security audit CSV (authorized vs. unauthorized vs. whitelisted).
- Provision a new endpoint that should match the API's existing authorizer / integration pattern.
- Sync a Spring Boot project's endpoints into API Gateway.
- Find or clean up deprecated / orphan endpoints (present in API Gateway, missing in code).

If the user only wants to *deploy* an existing API definition (e.g. `aws apigateway create-deployment`), that is outside this skill — point them at the relevant AWS CLI command but do not run it without explicit authorization.

## Required inputs

Before running, confirm with the user:

- **Target API**: a REST API ID (e.g. `abc123def4`) or a name pattern (e.g. `MyService-Public-PROD`).
- **Environment** (when using a name pattern): default `PROD`. Accept `CI`, `DEV`, `PROD`, or any other suffix the org uses.
- **Output directory**: default `./reports/<timestamp>/`. Override if the user specifies.
- **Whitelist directory** (optional): default none. If supplied, `generate_csv.py` looks for category JSON files in it (see "Whitelists" below).

If something is ambiguous, ask once, then proceed.

## Workflow

### Step 1 — Generate docs

Run `scripts/generate_docs.py` to fetch API definitions from AWS and write the Markdown tree.

```bash
python3 scripts/generate_docs.py \
    --microservice "MyService-Public" \
    --envs PROD \
    --output-dir ./reports/audit/
```

Or for every API in the account:

```bash
python3 scripts/generate_docs.py --all --envs PROD --output-dir ./reports/audit/
```

For APIs with 100+ paths, add `--compact` to produce a token-efficient INDEX.md (path bullets instead of a full table, trimmed ARNs, intermediate paths skipped):

```bash
python3 scripts/generate_docs.py --microservice MyService-Public --envs PROD \
    --output-dir ./reports/audit/ --compact
```

Output structure:

```
<output-dir>/
  <api-name>/                    # e.g. MyService-Public-PROD
    INDEX.md                     # API metadata, stages, authorizers, list of paths
    paths/
      _root_.md                  # `/` (health, etc.)
      v2_campaigns.md            # `/v2/campaigns` with all its methods
      v2_campaigns__id_.md       # `/v2/campaigns/{id}` ...
      ...
    raw_snapshot.json            # AWS get-resources --embed methods raw output
    raw_authorizers.json         # AWS get-authorizers raw output
    raw_stages.json              # AWS get-stages raw output (sensitive vars masked)
```

`INDEX.md` lists every path with a relative link to its file so the model can pick what to read.

`paths/<file>.md` for each resource contains:

- Resource ID
- All HTTP methods on that path (GET, POST, OPTIONS, …)
- For each method:
  - Authorization type + authorizer name + authorizer ID
  - API key required
  - Inferred auth pattern (e.g. `COGNITO_ADMIN`, `COGNITO_CUSTOMER`, or `NO_AUTH`) based on integration request claim headers
  - Integration type, connection (VPC link), URI
  - Integration request parameters (header / path mappings)

### Step 2 — Generate the security CSV

Run `scripts/generate_csv.py` over the docs tree:

```bash
python3 scripts/generate_csv.py \
    --input-dir ./reports/audit/ \
    --output ./reports/audit/security_audit_report.csv \
    [--whitelist-dir /path/to/whitelists/]
```

Columns:

```
api,method,path,is_authorized,authorization_type,authorizer_name,api_key,whitelist,endpoint_url
```

- `is_authorized = YES` when `authorization_type ∈ {COGNITO_USER_POOLS, CUSTOM, AWS_IAM}` OR `api_key = YES`. Otherwise `NO`.
- `whitelist` values: `NO`, one of the configured category labels, or `+`-joined when an endpoint matches multiple categories.
- `endpoint_url` is the integration URI with host and `${stageVariables.X}` stripped, leaving only the backend path.
- OPTIONS methods are excluded (CORS preflight).

See `references/csv_columns.md` for the full derivation rules and `references/whitelist_format.md` for the whitelist matching semantics.

### Step 3 — (Optional) Create new resources / methods

When the user asks to add or provision an endpoint on an existing API, use `scripts/create_path.py`. It picks a sibling method on the same API as a template and copies the authorizer, integration URI, headers, VPC link, and request parameters, so the new endpoint matches the existing pattern automatically.

```bash
python3 scripts/create_path.py \
    --microservice MyService-Public --env PROD \
    --path /v2/promotions/{id}/redeem \
    --method POST,DELETE \
    --auth COGNITO_CUSTOMER

# Preview before applying
python3 scripts/create_path.py \
    --api-id abc123def4 \
    --path /v2/notifications --method GET \
    --auth COGNITO_CUSTOMER --dry-run
```

Auth choices:

- `COGNITO_ADMIN` / `COGNITO_CUSTOMER` — pick the authorizer whose name matches `/admin/i` or `/customer/i`.
- `COGNITO_USER_POOLS` — generic Cognito; first matching pool authorizer.
- `API_KEY` — sets `apiKeyRequired=true`, no Cognito.
- `NO_AUTH` — open endpoint.
- `AUTO` — pick auth from the closest sibling method.

Behavior:

- Creates intermediate resources (`/v2`, `/v2/promotions`, …) when missing.
- Builds the backend integration URI by combining the API's host stage variable with the API's first backend segment (auto-detected from sibling integrations) and the new path. Override with `--backend-path /custom/...`.
- Path parameters in the new path (e.g. `{id}`) get the standard `integration.request.path.X = method.request.path.X` mapping automatically.
- Always offer `--dry-run` first so the user reviews every AWS CLI call.

After the script runs, advise the user that a stage deployment is needed for the change to be live (`aws apigateway create-deployment --rest-api-id <id> --stage-name <stage>`). Don't deploy automatically — that's an irreversible side effect for the user to authorize.

### Step 4 — Sync Spring Boot endpoints into API Gateway

When the user asks to mirror a Spring Boot project's endpoints into an API, use `scripts/sync_endpoints.py`. It scans the project, diffs against the live API, and runs `create_path.py` for each missing endpoint — no loose AWS CLI calls from the model.

```bash
python3 scripts/sync_endpoints.py \
    --project ./path/to/spring-app --env CI \
    --microservice MyService-Public \
    --types b2c,bo \
    --workspace ./reports/sync/

# Always preview first
python3 scripts/sync_endpoints.py --project ./spring-app --api-id abc123def4 \
    --types ext --dry-run
```

The endpoint type policy (which path prefixes are exposed publicly, which authorizer applies, etc.) lives in the `DEFAULT_TYPE_MAP` of `scan_spring_endpoints.py`. A small example map is shipped as a starting point; adapt it to the conventions used in the user's project, either by editing `DEFAULT_TYPE_MAP` directly or by passing `--config-file path/to/policy.json` with the same shape. Only types whose `expose=true` are pushed to API Gateway; the rest are scanned but skipped.

`sync_endpoints.py` writes `<workspace>/spring_inventory.json` plus a `sync-<timestamp>.json` summary. After sync it prints the deployment command — never runs it.

### Step 5 — Detect deprecated endpoints

When the user asks "which endpoints are deprecated/orphan?" or wants to clean up an API, start with `scripts/report_deprecated.py`.

```bash
python3 scripts/report_deprecated.py \
    --project ./path/to/spring-app --env CI \
    --microservice MyService-Public --types bo \
    --output-dir ./reports/deprecated/
```

The script:

- Calls `scan_spring_endpoints.py` on the project.
- Fetches the live API Gateway snapshot.
- Compares method by method (excluding OPTIONS).
- Writes `<output-dir>/<env>-<types>-<timestamp>.md` with metadata, the deprecated table, and an embedded JSON block consumed by `cleanup_endpoints.py`.

If the user filters with `--types`, the comparison is also restricted to the matching API Gateway prefixes so the report stays focused.

### Step 6 — Cleanup deprecated endpoints

After reviewing the deprecated report with the user, use `scripts/cleanup_endpoints.py`. It reads the report (no manual JSON parsing required) and issues the AWS CLI deletes itself.

```bash
# Interactive — prompts before deleting and before removing orphan resources
python3 scripts/cleanup_endpoints.py --report ./reports/deprecated/<file>.md \
    --remove-orphan-resources

# CI-friendly: skip prompts (still respects --dry-run)
python3 scripts/cleanup_endpoints.py --report <file>.md --auto --remove-orphan-resources

# Preview every AWS call without applying
python3 scripts/cleanup_endpoints.py --report <file>.md --dry-run
```

Behavior:

- Top-level confirmation unless `--auto` or `--dry-run`.
- For each entry, deletes the method.
- With `--remove-orphan-resources`, when no real methods remain on a resource, deletes the OPTIONS method (if present) and the resource itself — asking once per resource unless `--auto`.
- Writes a cleanup report `<report_dir>/cleanup-<timestamp>.md` listing successes and failures.
- Never deploys; prints the deployment command at the end.

If the user asks to clean up but no report exists, run `report_deprecated.py` first, then `cleanup_endpoints.py`.

### Step 7 — Use the docs

After running these phases:

- For broad questions ("which endpoints lack auth?"), read the CSV — compact and complete.
- For deep questions ("how is `POST /v2/foo` integrated?"), read just `paths/v2_foo.md`. Do not pull every file.
- Use `INDEX.md` first to locate the right files before opening anything.

When investigating a specific finding from the CSV, open the corresponding path's MD file for full context (integration URI, request parameters, etc.).

## Whitelists

Whitelists tell the auditor that an endpoint without API Gateway authorization is still acceptable because of an alternative protection mechanism. Three default categories ship with the skill (matched by filename in `--whitelist-dir`):

- `whitelist_PUBLIC_BY_DESIGN.json` — public by design (health checks, public webhooks).
- `whitelist_AUTH_IN_BACKEND.json` — backend service handles auth itself.
- `whitelist_IP_RESTRICTED.json` — IP-allowlist or VPC-restricted endpoints.

The category label that lands in the CSV is the middle segment of the filename (e.g. `PUBLIC_BY_DESIGN`). Add or rename categories simply by dropping new `whitelist_<LABEL>.json` files into the directory; `generate_csv.py` discovers any file matching the `whitelist_*.json` pattern.

Format:

```json
{
  "whitelist": {
    "MyService-Auth-Public-PROD": [
      {"method": "POST", "path": "/oauth/token", "comment": "Public auth endpoint"},
      {"method": "GET",  "path": "/oauth/validate", "comment": "Validates with signature check"}
    ],
    "MyService-Webhooks-Public-PROD": [
      {"method": "POST", "path": "/webhooks/foo/*", "comment": "HMAC-signed webhook"}
    ]
  }
}
```

Matching rules (see `references/whitelist_format.md`):

- Keys are full API names as they appear in AWS (e.g. `MyService-Public-PROD`).
- `method` + `path` must match. Method is exact; path supports a single `*` per segment as a wildcard.
- `*` matches one path segment (e.g. `/users/*/profile` matches `/users/123/profile` but not `/users/123/profile/settings`).
- Comments are descriptive only, not used for matching.

If `--whitelist-dir` is omitted, all rows get `whitelist=NO`.

## Permissions

Scripts use AWS CLI v2. Required IAM:

```
apigateway:GetRestApis
apigateway:GetResources
apigateway:GetMethod
apigateway:GetAuthorizer
apigateway:GetStages
apigateway:CreateResource          # Step 3 / 4 only
apigateway:PutMethod               # Step 3 / 4 only
apigateway:PutIntegration          # Step 3 / 4 only
apigateway:PutMethodResponse       # Step 3 / 4 only
apigateway:PutIntegrationResponse  # Step 3 / 4 only
apigateway:DeleteMethod            # Step 6 only
apigateway:DeleteResource          # Step 6 only
```

Expired SSO sessions surface as AWS CLI errors — relay them and ask the user to refresh credentials.

## When something goes wrong

- **Empty docs tree** → AWS credentials missing or `--microservice` filter matches nothing. Confirm names with `aws apigateway get-rest-apis --query 'items[].name'`.
- **CSV missing rows** → check `_root_.md` and confirm OPTIONS exclusion isn't accidentally dropping real methods.
- **Whitelist column always `NO`** → wrong `--whitelist-dir`, or the API key in the JSON doesn't match the actual API name as registered in AWS.
- **`create_path.py` cannot find a template** → the API has no integrations to copy; pass `--auth NO_AUTH` and the script falls back to a basic configuration, or wire the integration manually with `aws apigateway put-integration`.

## Reference files

- `references/csv_columns.md` — full spec of CSV columns and how each is derived.
- `references/whitelist_format.md` — whitelist matching rules + wildcard semantics.

## Repo

The scripts in this skill are self-contained. To use the skill outside Claude Code, copy the `scripts/` directory anywhere with Python 3 and AWS CLI v2 configured.
