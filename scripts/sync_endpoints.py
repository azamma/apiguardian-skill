#!/usr/bin/env python3
"""
sync_endpoints.py - Mirror Spring Boot endpoints into AWS API Gateway.

End-to-end orchestrator that:

1. Scans the Spring Boot project (delegates to scan_spring_endpoints.py).
2. Fetches the live API Gateway snapshot for the resolved REST API.
3. Diffs the two and reports missing methods/resources.
4. For each missing endpoint, calls create_path.py to add it, inferring
   authorizer/integration/headers from sibling methods on the same API.

Usage:
    python3 sync_endpoints.py \
        --project ../spring-app --env CI \
        --microservice MyService-Public \
        --types bo,b2c

    # Preview every AWS CLI call before applying anything
    python3 sync_endpoints.py --project . --api-id 5kyuvu07m5 --types ext --dry-run

The script never deploys. After successful sync it prints the exact deployment
command for the user to authorize manually.
"""

import argparse
import json
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

SCRIPT_DIR = Path(__file__).parent.resolve()
SCAN_SCRIPT = SCRIPT_DIR / "scan_spring_endpoints.py"
CREATE_PATH_SCRIPT = SCRIPT_DIR / "create_path.py"


def aws_json(command: str) -> Optional[Dict[str, Any]]:
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"[ERROR] AWS CLI: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout) if result.stdout.strip() else {}


def resolve_api(microservice: str, env: str) -> Optional[Tuple[str, str]]:
    data = aws_json("aws apigateway get-rest-apis --limit 500")
    if not data:
        return None
    target = f"{microservice}-{env}".lower()
    for api in data.get("items", []):
        if api.get("name", "").lower() == target:
            return api["id"], api["name"]
    return None


def fetch_apigw_methods(api_id: str) -> Set[Tuple[str, str]]:
    """Return {(METHOD, path)} for non-OPTIONS methods currently on the API."""
    data = aws_json(
        f"aws apigateway get-resources --rest-api-id {api_id} "
        f"--embed methods --limit 500"
    )
    found: Set[Tuple[str, str]] = set()
    if not data:
        return found
    for resource in data.get("items", []) or []:
        for method_name in (resource.get("resourceMethods") or {}).keys():
            if method_name.upper() == "OPTIONS":
                continue
            found.add((method_name.upper(), resource.get("path", "")))
    return found


def run_scan(project: Path, types: Optional[str], inventory_path: Path) -> Dict[str, Any]:
    args = [
        sys.executable, str(SCAN_SCRIPT),
        "--project", str(project),
        "--output", str(inventory_path),
    ]
    if types:
        args += ["--types", types]
    result = subprocess.run(args, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(
            f"[ERROR] scan_spring_endpoints failed: {result.stderr.strip()}",
            file=sys.stderr,
        )
        sys.exit(result.returncode)
    return json.loads(inventory_path.read_text(encoding="utf-8"))


def call_create_path(
    api_id: str,
    path: str,
    method: str,
    auth: str,
    backend_path: Optional[str],
    dry_run: bool,
) -> bool:
    """Spawn create_path.py for one missing endpoint."""
    cmd = [
        sys.executable, str(CREATE_PATH_SCRIPT),
        "--api-id", api_id,
        "--path", path,
        "--method", method,
        "--auth", auth,
    ]
    if backend_path:
        cmd += ["--backend-path", backend_path]
    if dry_run:
        cmd.append("--dry-run")
    result = subprocess.run(cmd, capture_output=False, text=True, check=False)
    return result.returncode == 0


def group_missing_by_path(
    missing: List[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Aggregate missing endpoints by API Gateway path so we issue one create per path."""
    grouped: Dict[str, Dict[str, Any]] = {}
    for ep in missing:
        path = ep["api_gateway_path"]
        bucket = grouped.setdefault(path, {
            "auth": ep["auth_inferred"],
            "spring_path": ep["spring_path"],
            "methods": [],
        })
        if ep["method"] not in bucket["methods"]:
            bucket["methods"].append(ep["method"])
    return grouped


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Sync missing Spring endpoints into API Gateway."
    )
    parser.add_argument("--project", required=True, help="Spring Boot project path.")
    parser.add_argument("--types", help="Comma-separated endpoint types (default: all exposed).")
    parser.add_argument(
        "--workspace", default="./reports/apigateway-sync/",
        help="Where to keep intermediate inventories and the sync report.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--api-id", help="Target REST API ID.")
    target.add_argument("--microservice", help="Microservice base name (combine with --env).")
    parser.add_argument("--env", default="PROD", help="Environment suffix.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Preview every create_path.py call without applying changes.",
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)

    if args.api_id:
        api_id = args.api_id
        api_name = api_id
    else:
        resolved = resolve_api(args.microservice, args.env)
        if not resolved:
            print(
                f"[ERROR] No API matched {args.microservice}-{args.env}",
                file=sys.stderr,
            )
            return 1
        api_id, api_name = resolved
    print(f"[INFO] Target API: {api_name} ({api_id})")

    inventory_path = workspace / "spring_inventory.json"
    inventory = run_scan(project, args.types, inventory_path)
    spring_endpoints = [
        ep for ep in (inventory.get("endpoints", []) or [])
        if ep.get("exposed_in_api_gateway") and ep.get("api_gateway_path")
    ]

    apigw_keys = fetch_apigw_methods(api_id)
    missing = [
        ep for ep in spring_endpoints
        if (ep["method"].upper(), ep["api_gateway_path"]) not in apigw_keys
    ]

    print(f"[INFO] Spring exposed: {len(spring_endpoints)}")
    print(f"[INFO] API Gateway:    {len(apigw_keys)}")
    print(f"[INFO] Missing:        {len(missing)}")

    grouped = group_missing_by_path(missing)
    if not grouped:
        print("[OK] Nothing to sync.")
        return 0

    for path, info in sorted(grouped.items()):
        methods = ",".join(sorted(info["methods"]))
        print(f"  - {methods:18s} {path}  [{info['auth']}]  ← {info['spring_path']}")

    summary = {
        "api_id": api_id,
        "api_name": api_name,
        "env": args.env,
        "started_at": datetime.now().isoformat(),
        "missing_groups": [
            {"path": p, **info} for p, info in grouped.items()
        ],
        "results": [],
        "dry_run": args.dry_run,
    }

    failures = 0
    for path, info in sorted(grouped.items()):
        methods = ",".join(sorted(info["methods"]))
        # backend path = full Spring path (already includes /<microservice>/...)
        backend_path = info["spring_path"]
        ok = call_create_path(
            api_id=api_id,
            path=path,
            method=methods,
            auth=info["auth"] or "AUTO",
            backend_path=backend_path,
            dry_run=args.dry_run,
        )
        summary["results"].append({
            "path": path,
            "methods": info["methods"],
            "auth": info["auth"],
            "ok": ok,
        })
        if not ok:
            failures += 1
            print(f"[FAIL] {methods} {path}")

    summary_path = workspace / f"sync-{datetime.now().strftime('%Y%m%dT%H%M%S')}.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(f"[OK] Sync summary: {summary_path}")

    if args.dry_run:
        print("[INFO] Dry-run finished. Re-run without --dry-run to apply.")
    else:
        print(
            "[INFO] To make changes live, run:\n"
            f"       aws apigateway create-deployment --rest-api-id {api_id} "
            f"--stage-name <STAGE> --description 'Sync Spring endpoints'"
        )
    return 0 if failures == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
