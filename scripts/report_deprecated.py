#!/usr/bin/env python3
"""
report_deprecated.py - Detect API Gateway endpoints absent from the Spring code.

Compares the Spring inventory produced by scan_spring_endpoints.py against the
live API Gateway snapshot, then writes a Markdown report of endpoints that
exist in API Gateway but no longer have a controller method backing them.

The report is the durable record consumed by cleanup_endpoints.py.

Usage:
    python3 report_deprecated.py \
        --project ../spring-app --env CI \
        --microservice MyService-Public \
        --types bo,b2c \
        --output-dir ./reports/deprecated/

Behavior:
- Spawns scan_spring_endpoints.py internally so callers don't have to manage
  intermediate files (the inventory is also saved to <output-dir>/spring_inventory.json
  for transparency).
- Resolves the API Gateway target either via --api-id or --microservice + --env.
- Excludes OPTIONS methods (CORS preflight, not real endpoints).
- The report path is `<output-dir>/<env>-<types>-<timestamp>.md`. Returns the
  path on stdout for piping.
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
ENV_SUFFIXES = ("PROD", "DEV", "CI")


def aws_json(command: str) -> Optional[Dict[str, Any]]:
    """Run an AWS CLI command and parse JSON output."""
    result = subprocess.run(
        command, shell=True, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        print(f"[ERROR] AWS CLI: {result.stderr.strip()}", file=sys.stderr)
        return None
    return json.loads(result.stdout) if result.stdout.strip() else {}


def resolve_api_id(microservice: str, env: str) -> Optional[Tuple[str, str]]:
    """Match (api_id, api_name) for `microservice` in `env`."""
    data = aws_json("aws apigateway get-rest-apis --limit 500")
    if not data:
        return None
    target_a = f"{microservice}-{env}".lower()
    target_b = f"{microservice}{env}".lower()
    for api in data.get("items", []):
        name = api.get("name", "")
        if name.lower() == target_a or name.lower().replace("-", "") == target_b.replace("-", ""):
            return api["id"], name
    return None


def fetch_api_gateway_endpoints(
    api_id: str,
) -> List[Dict[str, Any]]:
    """Build a flat list of {method, path, resource_id} from API Gateway."""
    data = aws_json(
        f"aws apigateway get-resources --rest-api-id {api_id} "
        f"--embed methods --limit 500"
    )
    out: List[Dict[str, Any]] = []
    if not data:
        return out
    for resource in data.get("items", []) or []:
        for method_name in (resource.get("resourceMethods") or {}).keys():
            if method_name.upper() == "OPTIONS":
                continue
            out.append({
                "method": method_name.upper(),
                "path": resource.get("path", ""),
                "resource_id": resource.get("id", ""),
            })
    return out


def run_scan(project: Path, types: Optional[str], inventory_path: Path) -> Dict[str, Any]:
    """Invoke scan_spring_endpoints.py and load the resulting inventory."""
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


def build_report_md(
    inventory: Dict[str, Any],
    api_name: str,
    api_id: str,
    env: str,
    types: Optional[str],
    deprecated: List[Dict[str, Any]],
    spring_count: int,
    apigw_count: int,
) -> str:
    """Render the Markdown report consumed by cleanup_endpoints.py."""
    lines: List[str] = []
    lines.append("# Reporte de Endpoints Deprecados")
    lines.append("")
    lines.append("## Información del Análisis")
    lines.append("")
    lines.append("| Campo | Valor |")
    lines.append("|-------|-------|")
    lines.append(f"| Proyecto | `{inventory.get('project', '?')}` |")
    lines.append(f"| Microservice segment | `{inventory.get('microservice_segment') or '?'}` |")
    lines.append(f"| Ambiente | `{env}` |")
    lines.append(f"| Tipos | `{types or 'all'}` |")
    lines.append(f"| API Gateway | `{api_name}` (`{api_id}`) |")
    lines.append(f"| Fecha | `{datetime.now().isoformat()}` |")
    lines.append(f"| Endpoints en código Spring | {spring_count} |")
    lines.append(f"| Endpoints en API Gateway | {apigw_count} |")
    lines.append(f"| Endpoints deprecados | **{len(deprecated)}** |")
    lines.append("")

    if not deprecated:
        lines.append("✅ No hay endpoints deprecados detectados.")
        return "\n".join(lines)

    lines.append("## Endpoints Deprecados")
    lines.append("")
    lines.append("| # | Método | Path API Gateway | Resource ID |")
    lines.append("|---|--------|------------------|-------------|")
    for idx, ep in enumerate(deprecated, 1):
        lines.append(
            f"| {idx} | {ep['method']} | `{ep['path']}` | `{ep['resource_id']}` |"
        )
    lines.append("")

    lines.append("## Acción sugerida")
    lines.append("")
    lines.append(
        "Ejecutá `cleanup_endpoints.py --report <este archivo>` para borrarlos. "
        "El script confirma cada eliminación, borra recursos huérfanos y deja "
        "registro en `<report_dir>/cleanup-<timestamp>.md`."
    )
    lines.append("")

    lines.append("## Datos crudos")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(deprecated, indent=2))
    lines.append("```")
    lines.append("")
    return "\n".join(lines)


def safe_slug(value: str) -> str:
    """Filename-safe slug for the report name."""
    return re.sub(r"[^A-Za-z0-9_-]", "_", value or "all")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deprecated-endpoints Markdown report."
    )
    parser.add_argument("--project", required=True, help="Spring Boot project path.")
    parser.add_argument(
        "--types", help="Comma-separated endpoint types (default: all)."
    )
    parser.add_argument(
        "--output-dir", required=True,
        help="Directory where the .md report is written.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--api-id", help="Target REST API ID.")
    target.add_argument(
        "--microservice", help="Microservice base name; combine with --env.",
    )
    parser.add_argument("--env", default="PROD", help="Environment suffix (default PROD).")

    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.api_id:
        api_id = args.api_id
        api_name = api_id
    else:
        resolved = resolve_api_id(args.microservice, args.env)
        if not resolved:
            print(
                f"[ERROR] Could not find API for {args.microservice}-{args.env}",
                file=sys.stderr,
            )
            return 1
        api_id, api_name = resolved
    print(f"[INFO] Target API: {api_name} ({api_id})")

    inventory_path = output_dir / "spring_inventory.json"
    inventory = run_scan(project, args.types, inventory_path)
    spring_endpoints = inventory.get("endpoints", []) or []

    spring_keys: Set[Tuple[str, str]] = set()
    for ep in spring_endpoints:
        if not ep.get("exposed_in_api_gateway"):
            continue
        if not ep.get("api_gateway_path"):
            continue
        spring_keys.add((ep["method"].upper(), ep["api_gateway_path"]))

    apigw_endpoints = fetch_api_gateway_endpoints(api_id)
    types_filter: Set[str] = set()
    if args.types:
        types_filter = {
            inventory["endpoint_type_map"][t]["api_gateway_prefix"].rstrip("/")
            for t in (args.types.split(",") if args.types else [])
            if t in inventory["endpoint_type_map"]
            and inventory["endpoint_type_map"][t].get("expose")
            and inventory["endpoint_type_map"][t].get("api_gateway_prefix")
        }

    if types_filter:
        apigw_endpoints = [
            ep for ep in apigw_endpoints
            if any(ep["path"].startswith(prefix) or ep["path"] == prefix for prefix in types_filter)
        ]

    deprecated = [
        ep for ep in apigw_endpoints
        if (ep["method"].upper(), ep["path"]) not in spring_keys
    ]
    deprecated.sort(key=lambda e: (e["path"], e["method"]))

    timestamp = datetime.now().strftime("%Y%m%dT%H%M%S")
    types_slug = safe_slug(args.types or "all")
    report_path = output_dir / f"{args.env.lower()}-{types_slug}-{timestamp}.md"
    report_path.write_text(
        build_report_md(
            inventory=inventory,
            api_name=api_name,
            api_id=api_id,
            env=args.env,
            types=args.types,
            deprecated=deprecated,
            spring_count=len(spring_keys),
            apigw_count=len(apigw_endpoints),
        ),
        encoding="utf-8",
    )

    print(f"[OK] Spring endpoints: {len(spring_keys)}")
    print(f"[OK] API Gateway endpoints: {len(apigw_endpoints)}")
    print(f"[OK] Deprecated: {len(deprecated)}")
    print(f"[OK] Report: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
