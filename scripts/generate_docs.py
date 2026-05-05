#!/usr/bin/env python3
"""
generate_docs.py - Phase 1 of apiguardian-skill.

Fetches AWS API Gateway definitions and writes a hierarchical Markdown tree:

    <output-dir>/
      <api-name>/
        INDEX.md
        paths/
          _root_.md
          v2_campaigns.md
          ...
        raw_snapshot.json
        raw_authorizers.json
        raw_stages.json

Usage:
    python3 generate_docs.py --microservice MyService-Public --envs PROD --output-dir ./out/
    python3 generate_docs.py --all --envs PROD --output-dir ./out/

The Markdown is structured so the orchestrating model can read INDEX.md to find
relevant paths and then load only the per-path files it cares about.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ADMIN_CLAIM = "custom:admin_id"
CUSTOMER_CLAIM = "custom:customer_id"
URI_HOST_RE = re.compile(r"^(https?://\$\{stageVariables\.\w+\})(/.*)$")
STAGE_VAR_RE = re.compile(r"\$\{stageVariables\.(\w+)\}")
ENV_SUFFIXES = ("PROD", "DEV", "CI")

SENSITIVE_VAR_RE = re.compile(
    r"(token|secret|password|api[_-]?key|private[_-]?key|credential)",
    re.IGNORECASE,
)


def mask_value(value: str) -> str:
    """Return a masked version of a sensitive value: first 4 + ***."""
    if not value or not isinstance(value, str):
        return value
    if len(value) <= 8:
        return "***REDACTED***"
    return f"{value[:4]}***REDACTED***"


def is_sensitive_var(name: str) -> bool:
    """Check whether a stage variable name looks like a secret."""
    return bool(SENSITIVE_VAR_RE.search(name or ""))


def aws_json(command: str) -> Optional[Dict[str, Any]]:
    """Execute an AWS CLI command and return the parsed JSON output."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            print(f"[ERROR] AWS CLI: {result.stderr.strip()}", file=sys.stderr)
            return None
        return json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        print(f"[ERROR] JSON parse: {exc}", file=sys.stderr)
        return None


def list_apis() -> List[Dict[str, Any]]:
    """List all REST APIs visible to the current AWS credentials."""
    data = aws_json("aws apigateway get-rest-apis --limit 500")
    if not data:
        return []
    return data.get("items", [])


def split_api_name(name: str) -> Tuple[str, Optional[str]]:
    """Split an API name into (base, env) using the trailing -ENV suffix."""
    parts = name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].upper() in ENV_SUFFIXES:
        return parts[0], parts[1].upper()
    return name, None


def filter_apis(
    apis: List[Dict[str, Any]],
    microservice: Optional[str],
    envs: List[str],
    select_all: bool,
) -> List[Dict[str, Any]]:
    """Filter APIs by microservice base name and environment suffix."""
    target_envs = {e.upper() for e in envs}
    selected = []
    for api in apis:
        name = api.get("name", "")
        if not name:
            continue
        base, env = split_api_name(name)
        if env is None or env not in target_envs:
            continue
        if not select_all:
            if microservice and microservice.lower() != base.lower():
                continue
        selected.append(api)
    return selected


def fetch_api_data(api_id: str) -> Dict[str, Any]:
    """Fetch resources, authorizers, and stages for one API in parallel."""
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {
            "resources": pool.submit(
                aws_json,
                f"aws apigateway get-resources --rest-api-id {api_id} "
                f"--embed methods --limit 500",
            ),
            "authorizers": pool.submit(
                aws_json,
                f"aws apigateway get-authorizers --rest-api-id {api_id}",
            ),
            "stages": pool.submit(
                aws_json,
                f"aws apigateway get-stages --rest-api-id {api_id}",
            ),
        }
        return {key: f.result() or {} for key, f in futures.items()}


def infer_auth_pattern(integration_request_params: Dict[str, str]) -> str:
    """Detect COGNITO_ADMIN/COGNITO_CUSTOMER/NO_AUTH from integration claim headers."""
    if not integration_request_params:
        return "NO_AUTH"
    for value in integration_request_params.values():
        if not isinstance(value, str):
            continue
        if ADMIN_CLAIM in value:
            return "COGNITO_ADMIN"
        if CUSTOMER_CLAIM in value:
            return "COGNITO_CUSTOMER"
    return "NO_AUTH"


def safe_filename(path: str) -> str:
    """Convert a resource path into a safe filename (URL-safe, no slashes)."""
    if path == "/":
        return "_root_"
    cleaned = path.strip("/")
    return re.sub(r"[^A-Za-z0-9_-]", "_", cleaned) or "_unnamed_"


def short_arn(arn: str) -> str:
    """Return only the last segment of an ARN (e.g., userpool/us-east-1_X)."""
    if not arn:
        return arn
    if ":" in arn:
        return arn.rsplit(":", 1)[-1]
    return arn


def write_index(
    api: Dict[str, Any],
    data: Dict[str, Any],
    out_dir: Path,
    compact: bool = False,
) -> None:
    """Write INDEX.md summarizing the API and linking to per-path files."""
    api_id = api.get("id", "?")
    api_name = api.get("name", api_id)
    resources = (data.get("resources") or {}).get("items", []) or []
    authorizers = (data.get("authorizers") or {}).get("items", []) or []
    stages = (data.get("stages") or {}).get("item", []) or []

    method_count = sum(
        len([m for m in (r.get("resourceMethods") or {}).keys()
             if m != "OPTIONS"])
        for r in resources
    )

    lines: List[str] = []
    lines.append(f"# {api_name}")
    lines.append("")
    lines.append(f"- **API ID:** `{api_id}`")
    lines.append(f"- **Resources:** {len(resources)}")
    lines.append(f"- **Methods (excl. OPTIONS):** {method_count}")
    lines.append(f"- **Authorizers:** {len(authorizers)}")
    lines.append(f"- **Stages:** {len(stages)}")
    lines.append(f"- **Generated:** {datetime.now().isoformat()}")
    lines.append("")

    lines.append("## Stages")
    if not stages:
        lines.append("_(none)_")
    else:
        for stage in stages:
            lines.append(f"- `{stage.get('stageName', '?')}` "
                         f"(deployment `{stage.get('deploymentId', '?')}`)")
            for var, val in sorted((stage.get("variables") or {}).items()):
                display = mask_value(val) if is_sensitive_var(var) else val
                lines.append(f"  - `{var}` = `{display}`")
    lines.append("")

    lines.append("## Authorizers")
    if not authorizers:
        lines.append("_(none)_")
    else:
        lines.append("| ID | Name | Type | Provider ARNs |")
        lines.append("|----|------|------|---------------|")
        for auth in authorizers:
            raw_arns = auth.get("providerARNs", []) or []
            arns_display = (
                ", ".join(short_arn(a) for a in raw_arns) if compact
                else ", ".join(raw_arns)
            ) or "—"
            lines.append(
                f"| `{auth.get('id', '?')}` | {auth.get('name', '?')} "
                f"| {auth.get('type', '?')} | {arns_display} |"
            )
    lines.append("")

    auth_by_id = {a.get("id"): a for a in authorizers if a.get("id")}

    lines.append("## Paths")
    lines.append("")
    if compact:
        lines.append(
            "_Format: PATH METHOD=Authorizer (file: paths/<safe>.md, "
            "where <safe> = path with `/` and braces replaced by `_`)._"
        )
        lines.append("")
    sorted_resources = sorted(resources, key=lambda r: r.get("path", ""))

    if compact:
        for resource in sorted_resources:
            path = resource.get("path", "?")
            method_map = resource.get("resourceMethods") or {}
            non_options = [m for m in sorted(method_map.keys()) if m != "OPTIONS"]
            if not non_options:
                # Skip intermediate paths with no real methods to save tokens
                continue
            grouped: Dict[str, List[str]] = {}
            for method_name in non_options:
                method_data = method_map.get(method_name) or {}
                authorizer_id = method_data.get("authorizerId", "")
                if authorizer_id:
                    label = (
                        auth_by_id.get(authorizer_id, {}).get("name", "")
                        or authorizer_id
                    )
                elif method_data.get("apiKeyRequired"):
                    label = "API_KEY"
                else:
                    label = "NONE"
                grouped.setdefault(label, []).append(method_name)
            method_groups = " ".join(
                f"{','.join(methods)}={auth}"
                for auth, methods in sorted(grouped.items())
            )
            lines.append(f"- `{path}` {method_groups}")
    else:
        lines.append("| Path | Methods | Authorizers | File |")
        lines.append("|------|---------|-------------|------|")
        for resource in sorted_resources:
            path = resource.get("path", "?")
            method_map = resource.get("resourceMethods") or {}
            methods = sorted(method_map.keys())
            method_label = ", ".join(methods) if methods else "—"

            method_auth_pairs = []
            for method_name in methods:
                if method_name == "OPTIONS":
                    continue
                method_data = method_map.get(method_name) or {}
                authorizer_id = method_data.get("authorizerId", "")
                if authorizer_id:
                    authorizer_name = auth_by_id.get(authorizer_id, {}).get(
                        "name", ""
                    )
                    label = authorizer_name or authorizer_id
                elif method_data.get("apiKeyRequired"):
                    label = "API_KEY"
                else:
                    label = "NONE"
                method_auth_pairs.append(f"{method_name}={label}")
            authorizer_label = (
                ", ".join(method_auth_pairs) if method_auth_pairs else "—"
            )

            file_name = f"paths/{safe_filename(path)}.md"
            lines.append(
                f"| `{path}` | {method_label} | {authorizer_label} | "
                f"[{file_name}]({file_name}) |"
            )
    lines.append("")

    (out_dir / "INDEX.md").write_text("\n".join(lines), encoding="utf-8")


def write_path_md(
    resource: Dict[str, Any],
    auth_by_id: Dict[str, Dict[str, Any]],
    out_dir: Path,
) -> None:
    """Write the per-path Markdown file with all methods and integrations."""
    path = resource.get("path", "?")
    file_name = safe_filename(path)
    methods = resource.get("resourceMethods") or {}

    lines: List[str] = []
    lines.append(f"# {path}")
    lines.append("")
    lines.append(f"- **Resource ID:** `{resource.get('id', '?')}`")
    lines.append(f"- **Method count:** {len(methods)}")
    lines.append("")

    if not methods:
        lines.append("_(no methods on this resource)_")
    else:
        for method_name in sorted(methods.keys()):
            data = methods[method_name]
            authorization_type = data.get("authorizationType", "NONE")
            authorizer_id = data.get("authorizerId", "")
            authorizer_name = (
                auth_by_id.get(authorizer_id, {}).get("name", "")
                if authorizer_id else ""
            )
            api_key_required = bool(data.get("apiKeyRequired", False))

            integration = data.get("methodIntegration") or {}
            integration_type = integration.get("type", "—")
            connection_type = integration.get("connectionType", "—")
            connection_id = integration.get("connectionId", "—")
            timeout_ms = integration.get("timeoutInMillis", "—")
            uri = integration.get("uri", "")
            request_params = integration.get("requestParameters") or {}
            inferred_pattern = infer_auth_pattern(request_params)

            lines.append(f"## {method_name}")
            lines.append("")
            lines.append(f"- **Authorization type:** `{authorization_type}`")
            if authorizer_id:
                lines.append(
                    f"- **Authorizer:** `{authorizer_name}` "
                    f"(id `{authorizer_id}`)"
                )
            else:
                lines.append("- **Authorizer:** _none_")
            lines.append(f"- **API key required:** {api_key_required}")
            lines.append(f"- **Inferred auth pattern:** `{inferred_pattern}`")
            lines.append(f"- **Integration type:** `{integration_type}`")
            lines.append(
                f"- **Connection:** `{connection_type}` `{connection_id}`"
            )
            lines.append(f"- **Timeout (ms):** {timeout_ms}")
            if uri:
                lines.append(f"- **Integration URI:** `{uri}`")
            if request_params:
                lines.append("")
                lines.append("### Integration request parameters")
                lines.append("")
                lines.append("| Parameter | Mapped from |")
                lines.append("|-----------|-------------|")
                for key, value in sorted(request_params.items()):
                    lines.append(f"| `{key}` | `{value}` |")
            lines.append("")

    target = out_dir / "paths" / f"{file_name}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("\n".join(lines), encoding="utf-8")


def mask_stage_data(stages_data: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-copied stages payload with sensitive variable values masked."""
    if not stages_data:
        return stages_data
    cloned = json.loads(json.dumps(stages_data))
    for stage in cloned.get("item", []) or []:
        variables = stage.get("variables") or {}
        for var_name, value in list(variables.items()):
            if is_sensitive_var(var_name):
                variables[var_name] = mask_value(value)
    return cloned


def dump_raw(out_dir: Path, data: Dict[str, Any]) -> None:
    """Persist the raw AWS responses for downstream tools (CSV phase)."""
    (out_dir / "raw_snapshot.json").write_text(
        json.dumps(data.get("resources") or {}, indent=2),
        encoding="utf-8",
    )
    (out_dir / "raw_authorizers.json").write_text(
        json.dumps(data.get("authorizers") or {}, indent=2),
        encoding="utf-8",
    )
    masked_stages = mask_stage_data(data.get("stages") or {})
    (out_dir / "raw_stages.json").write_text(
        json.dumps(masked_stages, indent=2),
        encoding="utf-8",
    )


def process_api(
    api: Dict[str, Any], output_root: Path, compact: bool = False
) -> Dict[str, Any]:
    """Build the docs folder for a single API. Returns a summary dict."""
    api_id = api.get("id", "?")
    api_name = api.get("name", api_id)
    safe_dir_name = re.sub(r"[^A-Za-z0-9_.-]", "_", api_name) or api_id
    api_dir = output_root / safe_dir_name
    api_dir.mkdir(parents=True, exist_ok=True)

    data = fetch_api_data(api_id)
    resources = (data.get("resources") or {}).get("items", []) or []
    authorizers = (data.get("authorizers") or {}).get("items", []) or []
    auth_by_id = {auth["id"]: auth for auth in authorizers if "id" in auth}

    write_index(api, data, api_dir, compact=compact)
    for resource in resources:
        write_path_md(resource, auth_by_id, api_dir)
    dump_raw(api_dir, data)

    return {
        "api_id": api_id,
        "api_name": api_name,
        "path_count": len(resources),
        "out_dir": str(api_dir),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate API Gateway docs tree.")
    parser.add_argument(
        "--microservice", help="Microservice base name (e.g., MyService-Public)."
    )
    parser.add_argument("--all", action="store_true", help="Document all APIs.")
    parser.add_argument(
        "--envs", default="PROD",
        help="Comma-separated environments (default: PROD).",
    )
    parser.add_argument(
        "--output-dir", required=True, help="Output directory."
    )
    parser.add_argument(
        "--compact", action="store_true",
        help=(
            "Generate a token-efficient INDEX.md (path bullets instead of "
            "table, trimmed ARNs). Recommended for APIs with 100+ paths."
        ),
    )
    args = parser.parse_args()

    if not args.all and not args.microservice:
        parser.error("Provide --microservice NAME or --all.")

    envs = [e.strip() for e in args.envs.split(",") if e.strip()]
    output_root = Path(args.output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    print(f"[INFO] Listing APIs from AWS...")
    apis = list_apis()
    if not apis:
        print("[ERROR] No APIs returned. Check AWS credentials.", file=sys.stderr)
        return 1

    selected = filter_apis(apis, args.microservice, envs, args.all)
    if not selected:
        print(
            "[ERROR] No APIs matched filters. "
            f"microservice={args.microservice} envs={envs} all={args.all}",
            file=sys.stderr,
        )
        return 1

    print(f"[INFO] Will document {len(selected)} APIs:")
    for api in selected:
        print(f"   - {api.get('name', '?')} ({api.get('id', '?')})")

    summaries = []
    for api in selected:
        print(f"[INFO] Processing {api.get('name', '?')}...")
        summary = process_api(api, output_root, compact=args.compact)
        summaries.append(summary)
        print(
            f"[OK]   {summary['api_name']} -> {summary['out_dir']} "
            f"({summary['path_count']} paths)"
        )

    summary_path = output_root / "summary.json"
    summary_path.write_text(
        json.dumps(
            {
                "generated_at": datetime.now().isoformat(),
                "envs": envs,
                "microservice": args.microservice,
                "all": args.all,
                "apis": summaries,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[INFO] Summary written to {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
