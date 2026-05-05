# apiguardian-skill

```
  █████╗  ██████╗  ██╗  ██████╗  ██╗   ██╗  █████╗  ██████╗  ██████╗  ██╗  █████╗  ███╗   ██╗
 ██╔══██╗ ██╔══██╗ ██║ ██╔════╝  ██║   ██║ ██╔══██╗ ██╔══██╗ ██╔══██╗ ██║ ██╔══██╗ ████╗  ██║
 ███████║ ██████╔╝ ██║ ██║  ███╗ ██║   ██║ ███████║ ██████╔╝ ██║  ██║ ██║ ███████║ ██╔██╗ ██║
 ██╔══██║ ██╔═══╝  ██║ ██║   ██║ ██║   ██║ ██╔══██║ ██╔══██╗ ██║  ██║ ██║ ██╔══██║ ██║╚██╗██║
 ██║  ██║ ██║      ██║ ╚██████╔╝ ╚██████╔╝ ██║  ██║ ██║  ██║ ██████╔╝ ██║ ██║  ██║ ██║ ╚████║
 ╚═╝  ╚═╝ ╚═╝      ╚═╝  ╚═════╝   ╚═════╝  ╚═╝  ╚═╝ ╚═╝  ╚═╝ ╚═════╝  ╚═╝ ╚═╝  ╚═╝ ╚═╝  ╚═══╝
```

Documentation, security auditing, and lifecycle management for AWS API Gateway,
designed to be driven by an LLM (Claude Code or similar) without it issuing
ad-hoc AWS API Gateway commands. Every action is wrapped in a deterministic
Python script: easy to review, easy to reproduce, easy to use as a regular CLI
tool too.

The skill ships seven self-contained scripts that do everything via the AWS
CLI v2 — no SDK dependencies, no third-party packages.

## What it does

| Capability | Script | What it produces |
|------------|--------|------------------|
| Documentation | `generate_docs.py` | Hierarchical Markdown tree of an API: `INDEX.md` + one file per resource path. |
| Security CSV | `generate_csv.py` | Deterministic security report (`is_authorized`, authorizer name, integration URL, optional whitelist categories). |
| Provisioning | `create_path.py` | Adds a new resource + method, copying authorizer / VPC link / headers / integration URI from a sibling endpoint on the same API. |
| Spring Boot sync | `sync_endpoints.py` | Diffs a Spring Boot project against an API and creates the missing endpoints. |
| Deprecation report | `report_deprecated.py` | Lists endpoints in API Gateway that no longer have a Spring controller backing them. |
| Cleanup | `cleanup_endpoints.py` | Reads a deprecation report and deletes the listed methods (and orphan resources, if requested). |
| Helper | `scan_spring_endpoints.py` | Pure scanner: parses Spring Boot `@*Mapping` annotations into a JSON inventory. |

## Why it exists

LLMs handle "audit this API" or "create a matching endpoint" badly when they
do it via a long sequence of raw AWS CLI calls — output is verbose, mistakes
are silent, and the same logic gets reinvented per conversation.

Splitting the work into deterministic scripts gives:

- **Reproducibility** — the same inputs always yield the same outputs.
- **Readable diffs** — Markdown trees and CSVs are easy to commit and review.
- **Token efficiency** — the LLM reads only the per-path file it needs (use
  `--compact` for huge APIs, ~65 % fewer tokens than the default tree).
- **Safer mutations** — `--dry-run` everywhere; nothing deploys automatically.

## Requirements

- Python 3.8+
- AWS CLI v2, configured (`aws sts get-caller-identity` must succeed)
- IAM permissions (Read-only is enough for documentation/audit; provisioning
  and cleanup need write permissions — see `SKILL.md`)

There are no third-party Python dependencies. Standard library only.

## Installation

### As a Claude Code skill

```bash
mkdir -p ~/.claude/skills
cp -r apiguardian-skill ~/.claude/skills/
```

Restart Claude Code (or open a new session). Trigger phrases live in
`SKILL.md`'s frontmatter — anything from "audit this API" to "clean up
deprecated endpoints" should fire it.

### As standalone CLI scripts

```bash
git clone <this-repo> apiguardian-skill
cd apiguardian-skill/scripts
python3 generate_docs.py --help
```

Symlink whatever scripts you want onto your `$PATH`:

```bash
ln -s "$PWD/generate_docs.py" /usr/local/bin/apg-generate-docs
```

## Quick start

### 1. Document every PROD API in your account

```bash
python3 scripts/generate_docs.py --all --envs PROD \
    --output-dir ./reports/audit/ --compact
```

For each API named like `MyService-Public-PROD`, you get:

```
reports/audit/MyService-Public-PROD/
  INDEX.md                  # API metadata, stages, authorizers, path index
  paths/                    # one Markdown file per resource path
  raw_snapshot.json         # raw AWS get-resources output
  raw_authorizers.json
  raw_stages.json           # sensitive stage variables auto-masked
```

### 2. Generate a security CSV

```bash
python3 scripts/generate_csv.py \
    --input-dir ./reports/audit/ \
    --output ./reports/audit/security.csv \
    --whitelist-dir ./whitelists/   # optional
```

CSV columns:

```
api,method,path,is_authorized,authorization_type,authorizer_name,api_key,whitelist,endpoint_url
```

### 3. Add a new endpoint that matches the API's existing pattern

```bash
# Preview every AWS CLI call first
python3 scripts/create_path.py \
    --api-id abc123def4 \
    --path /v2/promotions/{id}/redeem \
    --method POST,DELETE \
    --auth COGNITO_CUSTOMER \
    --dry-run

# Apply
python3 scripts/create_path.py --api-id abc123def4 \
    --path /v2/promotions/{id}/redeem --method POST,DELETE \
    --auth COGNITO_CUSTOMER
```

The script picks an existing method on the same API, infers the integration
URI, VPC link, request headers, and authorizer, and creates the new resource
+ method to match.

### 4. Sync a Spring Boot project into API Gateway

```bash
python3 scripts/sync_endpoints.py \
    --project ./path/to/spring-app --env CI \
    --microservice MyService-Public \
    --types b2c,bo \
    --workspace ./reports/sync/ \
    --dry-run

# Apply
python3 scripts/sync_endpoints.py --project ./path/to/spring-app \
    --microservice MyService-Public --env CI --types b2c,bo
```

### 5. Detect deprecated endpoints (in API Gateway but missing in code)

```bash
python3 scripts/report_deprecated.py \
    --project ./path/to/spring-app --env CI \
    --microservice MyService-Public --types bo \
    --output-dir ./reports/deprecated/
```

Then hand the resulting `.md` to:

```bash
python3 scripts/cleanup_endpoints.py \
    --report ./reports/deprecated/ci-bo-2026....md \
    --remove-orphan-resources
```

(Or pass `--auto` for non-interactive cleanup, `--dry-run` to preview.)

## Whitelists

`generate_csv.py` accepts an optional `--whitelist-dir` containing files named
`whitelist_<LABEL>.json`. Any file matching that pattern is auto-discovered —
the `<LABEL>` becomes the value emitted in the CSV's `whitelist` column.

Three suggested categories ship with the skill (you can add more):

- `whitelist_PUBLIC_BY_DESIGN.json` — public by design (health checks,
  unauthenticated webhooks).
- `whitelist_AUTH_IN_BACKEND.json` — backend service handles auth itself.
- `whitelist_IP_RESTRICTED.json` — IP allowlist or network-restricted
  endpoints.

Format:

```json
{
  "whitelist": {
    "MyService-Public-PROD": [
      {"method": "POST", "path": "/oauth/token", "comment": "Public auth"},
      {"method": "POST", "path": "/webhooks/foo/*", "comment": "HMAC-signed"}
    ]
  }
}
```

`*` matches a single path segment (no slashes). Multiple matches are joined
with `+` in the CSV (e.g. `PUBLIC_BY_DESIGN+IP_RESTRICTED`).

See `references/whitelist_format.md` for full semantics.

## Endpoint type policy

`scan_spring_endpoints.py` reads a configurable map of *endpoint types* — each
type defines a path prefix, an auth choice (`COGNITO_ADMIN`, `COGNITO_CUSTOMER`,
`NO_AUTH`, `API_KEY`), and whether to expose it in API Gateway at all.

A small example policy ships in the script as `DEFAULT_TYPE_MAP`. Adapt it to
your own conventions in either of two ways:

1. Edit `DEFAULT_TYPE_MAP` in `scan_spring_endpoints.py` directly.
2. Pass `--config-file path/to/policy.json` with the same shape as
   `DEFAULT_TYPE_MAP` (see the docstring at the top of the script).

A type with `expose=false` is recognized while scanning but skipped from API
Gateway sync — useful for internal-only endpoints (cron, internal-service
calls, etc.).

## Sensitive data handling

Stage variable values whose names match `token`, `secret`, `password`,
`api_key`, `private_key`, or `credential` (case-insensitive) are auto-masked
when writing INDEX.md and `raw_stages.json`. Override the regex in
`generate_docs.py:SENSITIVE_VAR_RE` if your conventions differ.

The MD reports never contain raw integration URIs unless they were already
stage-variable-templated; nothing is exfiltrated by default.

## Repository layout

```
apiguardian-skill/
├── SKILL.md                # Skill manifest + workflow (read this first)
├── README.md               # this file
├── scripts/
│   ├── generate_docs.py
│   ├── generate_csv.py
│   ├── create_path.py
│   ├── sync_endpoints.py
│   ├── report_deprecated.py
│   ├── cleanup_endpoints.py
│   └── scan_spring_endpoints.py
├── references/
│   ├── csv_columns.md
│   └── whitelist_format.md
└── evals/
    └── evals.json          # tests for the skill (used with skill-creator)
```

## Limitations

- Spring Boot scanner is regex-based, not a full Java parser. It handles
  standard `@RestController` + `@*Mapping` patterns; non-standard
  meta-annotations may slip through. Add a `--types` filter if your scan picks
  up too much.
- `create_path.py` infers integration settings from a sibling endpoint. If
  the API has no integrations, pass `--auth NO_AUTH` and the script uses
  fallback defaults.
- Nothing in this skill performs `aws apigateway create-deployment`. After
  any mutation that should be live, deploy explicitly:
  `aws apigateway create-deployment --rest-api-id <id> --stage-name <stage>`.
- AWS API Gateway HTTP APIs (v2) are out of scope; only REST APIs are
  supported.

## Testing

The `evals/` directory holds prompt-level tests intended to be driven by the
`skill-creator` framework. Run them via:

```bash
# From the skill-creator workspace
python3 -m scripts.aggregate_benchmark <path-to-runs> --skill-name apiguardian-skill
```

You can also call the scripts directly against a sandbox AWS account to
verify behaviour outside the LLM loop.

## Contributing

PRs welcome. When adding a new capability, prefer creating a new script under
`scripts/` (one CLI per concern) rather than expanding an existing one. The
LLM is much better at picking the right tool when each script does one thing.

If you change the CSV columns or the whitelist format, update both
`references/*.md` and `SKILL.md` in the same change so the skill description
stays accurate.

## License

MIT (see `LICENSE`).
