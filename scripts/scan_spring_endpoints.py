#!/usr/bin/env python3
"""
scan_spring_endpoints.py - Scan a Spring Boot project for HTTP endpoints.

Walks every .java file under the project's `src/main/java`, extracts
`@RequestMapping`, `@GetMapping`, `@PostMapping`, etc. annotations, and emits a
JSON inventory other scripts (sync, deprecated report) consume.

Output JSON shape:
    {
      "project": "/path/to/repo",
      "scanned_at": "2026-...",
      "endpoint_type_map": { "b2c": {"prefix": "/b2c/", "auth": "..."}, ... },
      "endpoints": [
        {
          "spring_path": "/my-service/bo/foo",
          "api_gateway_path": "/bo/foo",
          "method": "GET",
          "endpoint_type": "bo",
          "auth_inferred": "COGNITO_ADMIN",
          "controller_file": "src/main/java/.../FooController.java",
          "exposed_in_api_gateway": true
        }, ...
      ]
    }

Default endpoint type policy (a common convention) is hardcoded but can be
overridden with --config-file pointing at JSON of the same shape as
endpoint_type_map.

Usage:
    python3 scan_spring_endpoints.py --project ../spring-app --output endpoints.json
    python3 scan_spring_endpoints.py --project . --output - > endpoints.json
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

DEFAULT_TYPE_MAP: Dict[str, Dict[str, Any]] = {
    "b2c": {
        "spring_prefix": "/b2c/",
        "api_gateway_prefix": "/b2c/",
        "auth": "COGNITO_CUSTOMER",
        "expose": True,
        "description": "Persona-type users",
    },
    "b2b": {
        "spring_prefix": "/b2b/",
        "api_gateway_prefix": "/b2b/",
        "auth": "COGNITO_CUSTOMER",
        "expose": True,
        "description": "Empresa-type users",
    },
    "bo": {
        "spring_prefix": "/bo/",
        "api_gateway_prefix": "/bo/",
        "auth": "COGNITO_ADMIN",
        "expose": True,
        "description": "Backoffice admin",
    },
    "ext": {
        "spring_prefix": "/ext/",
        "api_gateway_prefix": "/ext/",
        "auth": "NO_AUTH",
        "expose": True,
        "description": "Public endpoints (login, register)",
    },
    "notification": {
        "spring_prefix": "/notification",
        "api_gateway_prefix": "/notification",
        "auth": "API_KEY",
        "expose": True,
        "description": "Provider webhooks",
    },
    "iuse": {
        "spring_prefix": "/iuse/",
        "api_gateway_prefix": None,
        "auth": None,
        "expose": False,
        "description": "Internal microservice-to-microservice",
    },
    "sfc": {
        "spring_prefix": "/sfc/",
        "api_gateway_prefix": None,
        "auth": None,
        "expose": False,
        "description": "Salesforce internal",
    },
    "cron": {
        "spring_prefix": "/cron",
        "api_gateway_prefix": None,
        "auth": None,
        "expose": False,
        "description": "Scheduled Lambdas / EventBridge",
    },
}


CLASS_REQ_MAPPING_RE = re.compile(
    r"@RequestMapping\s*\(\s*(?:value\s*=\s*)?\"([^\"]+)\"",
    re.MULTILINE,
)
METHOD_MAPPING_RE = re.compile(
    r"@(Get|Post|Put|Delete|Patch)Mapping"
    r"\s*(?:\(\s*(?:value\s*=\s*)?\"([^\"]*)\"\s*(?:,[^)]*)?\))?",
)
REQUEST_MAPPING_METHOD_RE = re.compile(
    r"@RequestMapping\s*\([^)]*method\s*=\s*RequestMethod\.(\w+)"
)
REQUEST_MAPPING_PATH_INSIDE_RE = re.compile(
    r"@RequestMapping\s*\([^)]*"
    r"(?:value|path)\s*=\s*\"([^\"]+)\"",
)
REST_CONTROLLER_RE = re.compile(r"@RestController\b")
CONTROLLER_RE = re.compile(r"@Controller\b")


def normalize_path(path: str) -> str:
    """Ensure leading slash, no trailing slash (except for the root)."""
    if not path:
        return ""
    if not path.startswith("/"):
        path = "/" + path
    while path.endswith("/") and len(path) > 1:
        path = path[:-1]
    return path


def join_path(base: str, sub: str) -> str:
    """Concatenate Spring base path and method path, handling slashes."""
    base = normalize_path(base)
    sub = normalize_path(sub)
    if not sub or sub == "/":
        return base or "/"
    if not base or base == "/":
        return sub
    return f"{base}{sub}"


def detect_type(spring_path: str, type_map: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """
    Find which endpoint type a Spring path belongs to.

    Spring paths look like `/<microservice>/<type>/...` (e.g.
    `/my-service/bo/foo` or `/my-service/v2/campaigns`). The first segment is the
    microservice itself; the type prefix lives in the rest of the path.

    To stay microservice-agnostic, we compare the prefix candidates against
    every position in the path (anchored at a `/` boundary).
    """
    longest_match: Optional[Tuple[str, int]] = None
    parts = [p for p in spring_path.split("/") if p]
    # Build the candidate "without microservice" path: skip first segment.
    candidate = "/" + "/".join(parts[1:]) if len(parts) > 1 else spring_path
    for type_name, cfg in type_map.items():
        prefix = cfg.get("spring_prefix") or ""
        if not prefix:
            continue
        normalized_prefix = prefix if prefix.startswith("/") else f"/{prefix}"
        if (
            candidate == normalized_prefix.rstrip("/")
            or candidate.startswith(normalized_prefix)
            or spring_path == normalized_prefix.rstrip("/")
            or spring_path.startswith(normalized_prefix)
        ):
            if longest_match is None or len(normalized_prefix) > longest_match[1]:
                longest_match = (type_name, len(normalized_prefix))
    return longest_match[0] if longest_match else None


def detect_microservice_segment(spring_path: str) -> Optional[str]:
    """First Spring segment, e.g. 'stocks' from '/my-service/bo/foo'."""
    if not spring_path:
        return None
    parts = [p for p in spring_path.split("/") if p]
    return parts[0] if parts else None


def to_apigateway_path(
    spring_path: str,
    type_map: Dict[str, Dict[str, Any]],
) -> Optional[str]:
    """Strip the microservice segment, leaving the API Gateway-facing path."""
    parts = [p for p in spring_path.split("/") if p]
    if len(parts) < 2:
        return None
    # Drop the first segment (microservice name)
    return "/" + "/".join(parts[1:])


def parse_controller(java_text: str) -> List[Dict[str, Any]]:
    """Extract endpoints from a controller's source. Best-effort regex parsing."""
    if not (REST_CONTROLLER_RE.search(java_text) or CONTROLLER_RE.search(java_text)):
        return []

    class_match = CLASS_REQ_MAPPING_RE.search(java_text)
    base_path = class_match.group(1) if class_match else ""

    endpoints: List[Dict[str, Any]] = []
    for match in METHOD_MAPPING_RE.finditer(java_text):
        verb = match.group(1).upper()
        sub_path = match.group(2) or ""
        endpoints.append({
            "method": verb,
            "spring_path": join_path(base_path, sub_path),
        })

    for match in REQUEST_MAPPING_METHOD_RE.finditer(java_text):
        method = match.group(1).upper()
        path_match = REQUEST_MAPPING_PATH_INSIDE_RE.search(
            java_text, pos=match.start()
        )
        sub_path = path_match.group(1) if path_match else ""
        endpoints.append({
            "method": method,
            "spring_path": join_path(base_path, sub_path),
        })
    return endpoints


def find_controllers(project_dir: Path) -> List[Path]:
    """All .java files under src/main/java (or the whole tree if missing)."""
    src = project_dir / "src" / "main" / "java"
    root = src if src.is_dir() else project_dir
    return [p for p in root.rglob("*.java") if p.is_file()]


def load_type_map(config_path: Optional[Path]) -> Dict[str, Dict[str, Any]]:
    """Use defaults unless a JSON file overrides them."""
    if not config_path:
        return DEFAULT_TYPE_MAP
    if not config_path.exists():
        print(
            f"[ERROR] Config file not found: {config_path}", file=sys.stderr
        )
        sys.exit(1)
    return json.loads(config_path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Scan Spring Boot project for HTTP endpoints."
    )
    parser.add_argument(
        "--project", required=True,
        help="Path to the Spring Boot project root.",
    )
    parser.add_argument(
        "--output", required=True,
        help="Output JSON path. Use '-' for stdout.",
    )
    parser.add_argument(
        "--config-file",
        help="Optional JSON file overriding the endpoint type policy.",
    )
    parser.add_argument(
        "--types",
        help=(
            "Comma-separated list of endpoint types to keep. Default: all. "
            "Example: 'b2c,bo' restricts the inventory."
        ),
    )
    args = parser.parse_args()

    project = Path(args.project).expanduser().resolve()
    if not project.is_dir():
        print(f"[ERROR] Project dir not found: {project}", file=sys.stderr)
        return 1

    type_map = load_type_map(
        Path(args.config_file).expanduser().resolve() if args.config_file else None
    )
    type_filter = (
        {t.strip() for t in args.types.split(",") if t.strip()}
        if args.types else None
    )

    endpoints: List[Dict[str, Any]] = []
    seen = set()  # (method, spring_path)
    for java in find_controllers(project):
        try:
            text = java.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            text = java.read_text(encoding="latin-1")
        for ep in parse_controller(text):
            spring_path = ep["spring_path"]
            method = ep["method"]
            key = (method, spring_path)
            if key in seen:
                continue
            seen.add(key)
            endpoint_type = detect_type(spring_path, type_map)
            type_cfg = type_map.get(endpoint_type or "", {})
            apigw_path = (
                to_apigateway_path(spring_path, type_map)
                if type_cfg.get("expose") else None
            )
            if type_filter and (endpoint_type not in type_filter):
                continue
            endpoints.append({
                "method": method,
                "spring_path": spring_path,
                "endpoint_type": endpoint_type,
                "exposed_in_api_gateway": bool(type_cfg.get("expose")),
                "api_gateway_path": apigw_path,
                "auth_inferred": type_cfg.get("auth"),
                "controller_file": str(java.relative_to(project)),
            })

    endpoints.sort(key=lambda e: (e.get("endpoint_type") or "", e["spring_path"], e["method"]))

    inventory = {
        "project": str(project),
        "scanned_at": datetime.now().isoformat(),
        "microservice_segment": detect_microservice_segment(
            endpoints[0]["spring_path"]
        ) if endpoints else None,
        "endpoint_type_map": type_map,
        "type_filter": sorted(type_filter) if type_filter else None,
        "total_endpoints": len(endpoints),
        "endpoints": endpoints,
    }

    payload = json.dumps(inventory, indent=2)
    if args.output == "-":
        sys.stdout.write(payload)
    else:
        out = Path(args.output).expanduser().resolve()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(payload, encoding="utf-8")
        print(f"[OK] Wrote {len(endpoints)} endpoints to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
