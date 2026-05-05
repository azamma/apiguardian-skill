#!/usr/bin/env python3
"""
create_path.py - Create a new resource + method on an existing API Gateway,
inferring authorizer / integration / headers from sibling methods on the same
API.

Designed to pair with generate_docs.py: once the model has read INDEX.md and
understands what authorizers and integration patterns the API uses, it can
spawn this script to create matching new endpoints without re-asking the user
for every header.

Usage:
    python3 create_path.py \
        --microservice MyService-Public --env PROD \
        --path /v2/promotions/{id}/redeem --method POST,DELETE \
        --auth COGNITO_CUSTOMER

    python3 create_path.py \
        --api-id 5kyuvu07m5 \
        --path /b2c/customer/notifications --method GET \
        --auth COGNITO_CUSTOMER --dry-run

Auth values:
    COGNITO_ADMIN        — picks first authorizer whose name matches /admin/i
    COGNITO_CUSTOMER     — picks first authorizer whose name matches /customer/i
    COGNITO_USER_POOLS   — generic Cognito; picks any Cognito authorizer
    API_KEY              — apiKeyRequired=true, no Cognito
    NO_AUTH              — open endpoint
    AUTO                 — pick auth_type from any sibling using same path prefix

Backend path defaults: same shape as the resource path, prefixed by the first
segment used by other endpoints in the API (e.g. `/<service>`). Pass
`--backend-path /custom/...` to override.
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ENV_SUFFIXES = ("PROD", "DEV", "CI")
DEFAULT_TIMEOUT_MS = 29000
DEFAULT_INTEGRATION_TYPE = "HTTP_PROXY"
DEFAULT_CONNECTION_TYPE = "VPC_LINK"
PATH_PARAM_RE = re.compile(r"\{(\w+)\}")
URI_HOST_RE = re.compile(r"^(https?://\$\{stageVariables\.\w+\})(/.*)$")


def aws_run(command: str, dry_run: bool, description: str) -> Optional[Dict[str, Any]]:
    """Execute a shell AWS CLI command (or print it under --dry-run)."""
    if dry_run:
        print(f"[DRY-RUN] {description}\n         $ {command}")
        return {}
    print(f"[EXEC]    {description}")
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(
            f"[ERROR]   {description}: {result.stderr.strip()}",
            file=sys.stderr,
        )
        return None
    if not result.stdout.strip():
        return {}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}


def find_api_by_microservice(microservice: str, env: str) -> Optional[Tuple[str, str]]:
    """Resolve (api_id, api_name) for `microservice` in `env`."""
    target = f"{microservice}-{env}".lower()
    target_alt = f"{microservice.lower()}-{env.lower()}"
    raw = subprocess.run(
        "aws apigateway get-rest-apis --limit 500",
        shell=True, capture_output=True, text=True, check=False,
    )
    if raw.returncode != 0:
        print(f"[ERROR] Could not list APIs: {raw.stderr.strip()}", file=sys.stderr)
        return None
    items = json.loads(raw.stdout or "{}").get("items", [])
    for api in items:
        name = api.get("name", "")
        if name.lower() == target or name.lower() == target_alt:
            return api["id"], name
    return None


def fetch_snapshot(api_id: str) -> Dict[str, Any]:
    """Fetch the full resources + methods snapshot for an API."""
    raw = subprocess.run(
        f"aws apigateway get-resources --rest-api-id {api_id} "
        f"--embed methods --limit 500",
        shell=True, capture_output=True, text=True, check=False,
    )
    if raw.returncode != 0:
        print(f"[ERROR] get-resources: {raw.stderr.strip()}", file=sys.stderr)
        return {}
    return json.loads(raw.stdout or "{}")


def fetch_authorizers(api_id: str) -> List[Dict[str, Any]]:
    raw = subprocess.run(
        f"aws apigateway get-authorizers --rest-api-id {api_id}",
        shell=True, capture_output=True, text=True, check=False,
    )
    if raw.returncode != 0:
        return []
    return json.loads(raw.stdout or "{}").get("items", []) or []


def pick_authorizer(
    auth_choice: str, authorizers: List[Dict[str, Any]]
) -> Optional[Dict[str, Any]]:
    """Match `auth_choice` (COGNITO_ADMIN, COGNITO_CUSTOMER, COGNITO_USER_POOLS) to an authorizer."""
    if auth_choice == "COGNITO_ADMIN":
        for a in authorizers:
            if "admin" in (a.get("name") or "").lower():
                return a
    if auth_choice == "COGNITO_CUSTOMER":
        for a in authorizers:
            if "customer" in (a.get("name") or "").lower():
                return a
    if auth_choice == "COGNITO_USER_POOLS":
        for a in authorizers:
            if a.get("type") == "COGNITO_USER_POOLS":
                return a
    return None


def find_template_method(
    snapshot: Dict[str, Any],
    auth_choice: str,
    authorizer_id: Optional[str],
    api_key: bool,
) -> Optional[Dict[str, Any]]:
    """Pick an existing method to clone integration from."""
    for resource in snapshot.get("items", []) or []:
        for method_name, data in (resource.get("resourceMethods") or {}).items():
            if method_name == "OPTIONS":
                continue
            integration = data.get("methodIntegration") or {}
            if not integration:
                continue
            if api_key:
                if data.get("apiKeyRequired"):
                    return data
                continue
            if auth_choice == "NO_AUTH":
                if data.get("authorizationType") == "NONE":
                    return data
                continue
            if authorizer_id and data.get("authorizerId") == authorizer_id:
                return data
    # Fallback: any method with an integration so we can copy host / VPC link
    for resource in snapshot.get("items", []) or []:
        for method_name, data in (resource.get("resourceMethods") or {}).items():
            if method_name == "OPTIONS":
                continue
            if data.get("methodIntegration"):
                return data
    return None


def parse_uri(uri: str) -> Tuple[Optional[str], Optional[str]]:
    """Split integration URI into (host_with_stagevar, backend_path)."""
    if not uri:
        return None, None
    match = URI_HOST_RE.match(uri)
    if not match:
        return None, uri
    return match.group(1), match.group(2)


def detect_first_segment(snapshot: Dict[str, Any]) -> Optional[str]:
    """Find the first backend-path segment used across the API (e.g. '<service>')."""
    for resource in snapshot.get("items", []) or []:
        for data in (resource.get("resourceMethods") or {}).values():
            integration = data.get("methodIntegration") or {}
            uri = integration.get("uri") or ""
            _, backend = parse_uri(uri)
            if backend and len(backend) > 1:
                segments = backend.strip("/").split("/")
                if segments:
                    return segments[0]
    return None


def find_resource_for_path(
    snapshot: Dict[str, Any], path: str
) -> Optional[str]:
    for resource in snapshot.get("items", []) or []:
        if resource.get("path") == path:
            return resource.get("id")
    return None


def root_resource_id(snapshot: Dict[str, Any]) -> Optional[str]:
    for resource in snapshot.get("items", []) or []:
        if resource.get("path") == "/":
            return resource.get("id")
    return None


def ensure_resources(
    api_id: str,
    snapshot: Dict[str, Any],
    target_path: str,
    dry_run: bool,
) -> Optional[str]:
    """Create any missing intermediate resources up to target_path. Returns leaf resource_id."""
    if target_path == "/":
        return root_resource_id(snapshot)

    parts = [p for p in target_path.split("/") if p]
    parent_id = root_resource_id(snapshot)
    if not parent_id and not dry_run:
        print("[ERROR] No root resource found.", file=sys.stderr)
        return None

    accumulated = ""
    for segment in parts:
        accumulated += "/" + segment
        existing_id = find_resource_for_path(snapshot, accumulated)
        if existing_id:
            parent_id = existing_id
            continue
        result = aws_run(
            f"aws apigateway create-resource --rest-api-id {api_id} "
            f"--parent-id {parent_id} --path-part \"{segment}\"",
            dry_run,
            f"create resource {segment}",
        )
        if result is None:
            return None
        new_id = result.get("id") if isinstance(result, dict) else None
        if not new_id and not dry_run:
            print(f"[ERROR] Failed to create resource {segment}", file=sys.stderr)
            return None
        # Update snapshot in memory so subsequent lookups work
        snapshot.setdefault("items", []).append({
            "id": new_id or f"DRY_{segment}",
            "path": accumulated,
            "resourceMethods": {},
        })
        parent_id = new_id or f"DRY_{segment}"
    return parent_id


def build_request_parameters(
    template: Dict[str, Any], target_path: str
) -> Dict[str, str]:
    """Clone request parameters from the template; rebuild path-param mappings for new path."""
    template_params = (
        (template.get("methodIntegration") or {}).get("requestParameters") or {}
    )
    result: Dict[str, str] = {}
    for key, value in template_params.items():
        if key.startswith("integration.request.path."):
            # Skip — we'll rebuild from the new path
            continue
        result[key] = value
    for param in PATH_PARAM_RE.findall(target_path):
        result[f"integration.request.path.{param}"] = (
            f"method.request.path.{param}"
        )
    return result


def build_method_request_parameters(target_path: str) -> Dict[str, bool]:
    return {
        f"method.request.path.{p}": True
        for p in PATH_PARAM_RE.findall(target_path)
    }


def create_method(
    api_id: str,
    resource_id: str,
    method: str,
    target_path: str,
    template: Dict[str, Any],
    authorizer_id: Optional[str],
    auth_choice: str,
    api_key: bool,
    backend_path: str,
    dry_run: bool,
) -> bool:
    """Run put-method + put-integration + put-*-response for one HTTP method."""
    integration = template.get("methodIntegration") or {}
    template_uri = integration.get("uri") or ""
    template_host, _ = parse_uri(template_uri)
    if not template_host:
        print("[ERROR] Could not parse template integration URI.", file=sys.stderr)
        return False

    if api_key:
        authorization_type = "NONE"
    elif auth_choice == "NO_AUTH":
        authorization_type = "NONE"
    else:
        authorization_type = "COGNITO_USER_POOLS"

    method_params = build_method_request_parameters(target_path)
    request_params = build_request_parameters(template, target_path)
    new_uri = f"{template_host}{backend_path}"

    parts = [
        f"aws apigateway put-method --rest-api-id {api_id}",
        f"--resource-id {resource_id}",
        f"--http-method {method}",
        f"--authorization-type {authorization_type}",
    ]
    if authorizer_id and authorization_type == "COGNITO_USER_POOLS":
        parts.append(f"--authorizer-id {authorizer_id}")
    parts.append("--api-key-required" if api_key else "--no-api-key-required")
    if method_params:
        params_json = json.dumps(method_params).replace('"', '\\"')
        parts.append(f'--request-parameters "{params_json}"')
    if aws_run(" ".join(parts), dry_run, f"put-method {method} {target_path}") is None:
        return False

    timeout_ms = integration.get("timeoutInMillis") or DEFAULT_TIMEOUT_MS
    integration_type = integration.get("type") or DEFAULT_INTEGRATION_TYPE
    connection_type = integration.get("connectionType") or DEFAULT_CONNECTION_TYPE
    connection_id = integration.get("connectionId") or ""

    int_parts = [
        f"aws apigateway put-integration --rest-api-id {api_id}",
        f"--resource-id {resource_id}",
        f"--http-method {method}",
        f"--type {integration_type}",
        f"--integration-http-method {method}",
        f'--uri "{new_uri}"',
        f"--connection-type {connection_type}",
    ]
    if connection_id:
        int_parts.append(f'--connection-id "{connection_id}"')
    if request_params:
        rp_json = json.dumps(request_params).replace('"', '\\"')
        int_parts.append(f'--request-parameters "{rp_json}"')
    int_parts.append(f"--timeout-in-millis {timeout_ms}")
    int_parts.append("--passthrough-behavior WHEN_NO_MATCH")
    if aws_run(" ".join(int_parts), dry_run, f"put-integration {method}") is None:
        return False

    aws_run(
        f"aws apigateway put-method-response --rest-api-id {api_id} "
        f"--resource-id {resource_id} --http-method {method} "
        f"--status-code 200 --response-models "
        f"\"{json.dumps({'application/json': 'Empty'}).replace(chr(34), chr(92)+chr(34))}\"",
        dry_run,
        f"put-method-response 200 {method}",
    )
    aws_run(
        f"aws apigateway put-integration-response --rest-api-id {api_id} "
        f"--resource-id {resource_id} --http-method {method} "
        f"--status-code 200 --response-templates "
        f"\"{json.dumps({'application/json': ''}).replace(chr(34), chr(92)+chr(34))}\"",
        dry_run,
        f"put-integration-response 200 {method}",
    )
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a new API Gateway resource+method.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--api-id", help="Target REST API ID.")
    group.add_argument(
        "--microservice",
        help="Microservice base name (used with --env).",
    )
    parser.add_argument(
        "--env", default="PROD",
        help="Environment suffix when using --microservice (default: PROD).",
    )
    parser.add_argument("--path", required=True, help="API Gateway resource path.")
    parser.add_argument(
        "--method", required=True,
        help="HTTP method or comma-separated list (e.g. POST or GET,POST).",
    )
    parser.add_argument(
        "--auth", default="AUTO",
        help=(
            "Auth choice: COGNITO_ADMIN | COGNITO_CUSTOMER | COGNITO_USER_POOLS "
            "| API_KEY | NO_AUTH | AUTO. AUTO copies auth from siblings when "
            "possible."
        ),
    )
    parser.add_argument(
        "--backend-path",
        help=(
            "Override backend integration path. Default: prepend the API's "
            "first backend segment (e.g. /<service>) to --path."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print AWS commands without executing.",
    )
    args = parser.parse_args()

    if args.api_id:
        api_id = args.api_id
        api_name = api_id
    else:
        resolved = find_api_by_microservice(args.microservice, args.env)
        if not resolved:
            print(
                f"[ERROR] Could not find API for {args.microservice}-{args.env}",
                file=sys.stderr,
            )
            return 1
        api_id, api_name = resolved

    print(f"[INFO] Target API: {api_name} ({api_id})")
    snapshot = fetch_snapshot(api_id)
    authorizers = fetch_authorizers(api_id)

    auth_choice = args.auth.upper()
    api_key = auth_choice == "API_KEY"
    authorizer_id: Optional[str] = None
    if auth_choice in {"COGNITO_ADMIN", "COGNITO_CUSTOMER", "COGNITO_USER_POOLS"}:
        authorizer = pick_authorizer(auth_choice, authorizers)
        if not authorizer:
            print(
                f"[ERROR] No authorizer matches {auth_choice} in this API.",
                file=sys.stderr,
            )
            return 1
        authorizer_id = authorizer["id"]
        print(
            f"[INFO] Using authorizer: {authorizer.get('name')} "
            f"({authorizer_id})"
        )

    template = find_template_method(snapshot, auth_choice, authorizer_id, api_key)
    if not template:
        print(
            "[ERROR] Could not find a template method to clone integration from.",
            file=sys.stderr,
        )
        return 1
    if auth_choice == "AUTO":
        if template.get("apiKeyRequired"):
            api_key = True
        elif template.get("authorizationType") == "COGNITO_USER_POOLS":
            authorizer_id = template.get("authorizerId")

    if args.backend_path:
        backend_path = args.backend_path
    else:
        first_segment = detect_first_segment(snapshot) or ""
        prefix = f"/{first_segment}" if first_segment else ""
        backend_path = f"{prefix}{args.path}"
    print(f"[INFO] Backend path: {backend_path}")

    resource_id = ensure_resources(api_id, snapshot, args.path, args.dry_run)
    if not resource_id:
        return 1
    print(f"[INFO] Resource ID: {resource_id}")

    methods = [m.strip().upper() for m in args.method.split(",") if m.strip()]
    overall_ok = True
    for method in methods:
        ok = create_method(
            api_id=api_id,
            resource_id=resource_id,
            method=method,
            target_path=args.path,
            template=template,
            authorizer_id=authorizer_id,
            auth_choice=auth_choice,
            api_key=api_key,
            backend_path=backend_path,
            dry_run=args.dry_run,
        )
        if ok:
            print(f"[OK]   {method} {args.path}")
        else:
            overall_ok = False
            print(f"[FAIL] {method} {args.path}")

    if args.dry_run:
        print("[INFO] Dry-run complete. No changes applied.")
    return 0 if overall_ok else 1


if __name__ == "__main__":
    sys.exit(main())
