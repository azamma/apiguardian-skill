#!/usr/bin/env python3
"""
generate_csv.py - Phase 2 of apiguardian-skill.

Reads the docs tree produced by generate_docs.py (specifically the per-API
raw_snapshot.json + raw_authorizers.json files), applies optional whitelists,
and emits a deterministic security-audit CSV.

Columns:
    api,method,path,is_authorized,authorization_type,authorizer_name,
    api_key,whitelist,endpoint_url

Usage:
    python3 generate_csv.py --input-dir ./out/ --output ./out/security_audit.csv
    python3 generate_csv.py --input-dir ./out/ --output ./out/security_audit.csv \
        --whitelist-dir ./whitelists/

OPTIONS methods are excluded (CORS preflight).
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

PROPER_AUTH_TYPES = {"COGNITO_USER_POOLS", "CUSTOM", "AWS_IAM"}
EXCLUDED_METHODS = {"OPTIONS"}
WHITELIST_FILE_GLOB = "whitelist_*.json"
WHITELIST_FILE_PREFIX = "whitelist_"
WHITELIST_FILE_SUFFIX = ".json"
STAGE_VAR_RE = re.compile(r"\$\{stageVariables\.\w+\}")


def load_whitelist(path: Path) -> Dict[str, List[Dict[str, str]]]:
    """Load a whitelist JSON; return the inner 'whitelist' map (api -> entries)."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        print(f"[WARN] Could not parse whitelist {path}: {exc}", file=sys.stderr)
        return {}
    return data.get("whitelist") or data.get("whitelisted_endpoints") or {}


def path_matches(pattern: str, value: str) -> bool:
    """
    Match a whitelist path pattern against an actual API path.

    A literal `*` matches one path segment (no slashes). Fast escape + replace.
    """
    if pattern == value:
        return True
    if "*" not in pattern:
        return False
    regex = "^" + re.escape(pattern).replace(r"\*", r"[^/]+") + "$"
    return re.match(regex, value) is not None


def lookup_whitelist(
    whitelists: Dict[str, Dict[str, List[Dict[str, str]]]],
    api_name: str,
    method: str,
    path: str,
) -> str:
    """
    Return the `+`-joined list of categories matching this method+path.
    `whitelists` is a dict of category -> (api -> entries).
    """
    matched = []
    for category, by_api in whitelists.items():
        entries = by_api.get(api_name) or []
        for entry in entries:
            entry_method = (entry.get("method") or "").upper()
            entry_path = entry.get("path") or ""
            if entry_method and entry_method != method.upper():
                continue
            if not entry_path:
                continue
            if path_matches(entry_path, path):
                matched.append(category)
                break
    return "+".join(matched) if matched else "NO"


def clean_endpoint_url(uri: str) -> str:
    """Strip protocol, host, and stage variable references; keep the path."""
    if not uri:
        return ""
    cleaned = STAGE_VAR_RE.sub("", uri)
    cleaned = re.sub(r"^https?://", "", cleaned)
    if "/" in cleaned:
        cleaned = "/" + cleaned.split("/", 1)[1]
    return cleaned or ""


def load_authorizer_map(authorizers_json: Dict[str, Any]) -> Dict[str, str]:
    """Map authorizer ID -> name."""
    items = authorizers_json.get("items", []) or []
    return {a.get("id"): a.get("name", "") for a in items if a.get("id")}


def iterate_api_dirs(input_dir: Path):
    """Yield each per-API subdirectory that has a raw_snapshot.json."""
    for child in sorted(input_dir.iterdir()):
        if not child.is_dir():
            continue
        snapshot = child / "raw_snapshot.json"
        if snapshot.exists():
            yield child


def rows_for_api(
    api_dir: Path,
    whitelists: Dict[str, Dict[str, List[Dict[str, str]]]],
) -> List[Dict[str, str]]:
    """Build CSV rows for a single API directory."""
    api_name = api_dir.name
    snapshot = json.loads((api_dir / "raw_snapshot.json").read_text(encoding="utf-8"))
    auth_map = {}
    auth_path = api_dir / "raw_authorizers.json"
    if auth_path.exists():
        try:
            auth_map = load_authorizer_map(
                json.loads(auth_path.read_text(encoding="utf-8"))
            )
        except json.JSONDecodeError:
            auth_map = {}

    rows: List[Dict[str, str]] = []
    for resource in snapshot.get("items", []) or []:
        path = resource.get("path", "")
        methods = resource.get("resourceMethods") or {}
        for method_name, data in methods.items():
            if method_name.upper() in EXCLUDED_METHODS:
                continue
            authorization_type = data.get("authorizationType", "NONE")
            authorizer_id = data.get("authorizerId", "")
            authorizer_name = (
                auth_map.get(authorizer_id, "") if authorizer_id else ""
            )
            api_key_required = bool(data.get("apiKeyRequired", False))
            api_key_value = "YES" if api_key_required else "NO"

            integration = data.get("methodIntegration") or {}
            endpoint_url = clean_endpoint_url(integration.get("uri", ""))

            is_authorized = (
                authorization_type in PROPER_AUTH_TYPES or api_key_required
            )
            whitelist_value = lookup_whitelist(
                whitelists, api_name, method_name, path
            )

            rows.append({
                "api": api_name,
                "method": method_name,
                "path": path,
                "is_authorized": "YES" if is_authorized else "NO",
                "authorization_type": authorization_type or "NONE",
                "authorizer_name": authorizer_name or "NONE",
                "api_key": api_key_value,
                "whitelist": whitelist_value,
                "endpoint_url": endpoint_url,
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Build security audit CSV.")
    parser.add_argument(
        "--input-dir", required=True,
        help="Directory containing per-API folders produced by generate_docs.py.",
    )
    parser.add_argument(
        "--output", required=True, help="Path for the output CSV.",
    )
    parser.add_argument(
        "--whitelist-dir",
        help="Optional directory with whitelist_*.json files.",
    )
    args = parser.parse_args()

    input_dir = Path(args.input_dir).expanduser().resolve()
    if not input_dir.is_dir():
        print(f"[ERROR] Input dir not found: {input_dir}", file=sys.stderr)
        return 1

    whitelists: Dict[str, Dict[str, List[Dict[str, str]]]] = {}
    if args.whitelist_dir:
        wd = Path(args.whitelist_dir).expanduser().resolve()
        if not wd.is_dir():
            print(
                f"[WARN] Whitelist dir not found: {wd}", file=sys.stderr
            )
        else:
            files = sorted(wd.glob(WHITELIST_FILE_GLOB))
            if not files:
                print(
                    f"[WARN] No whitelist_*.json files in {wd}",
                    file=sys.stderr,
                )
            for path in files:
                category = path.stem[len(WHITELIST_FILE_PREFIX):]
                if not category:
                    continue
                data = load_whitelist(path)
                if data:
                    whitelists[category] = data

    rows: List[Dict[str, str]] = []
    api_count = 0
    for api_dir in iterate_api_dirs(input_dir):
        api_count += 1
        rows.extend(rows_for_api(api_dir, whitelists))

    if not rows:
        print(
            f"[WARN] No rows produced from {api_count} API dirs in {input_dir}",
            file=sys.stderr,
        )

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(
            fp,
            fieldnames=[
                "api",
                "method",
                "path",
                "is_authorized",
                "authorization_type",
                "authorizer_name",
                "api_key",
                "whitelist",
                "endpoint_url",
            ],
        )
        writer.writeheader()
        writer.writerows(rows)

    unauthorized_unhitelisted = sum(
        1 for r in rows
        if r["is_authorized"] == "NO" and r["whitelist"] == "NO"
    )
    print(
        f"[OK] Wrote {len(rows)} rows from {api_count} APIs to {output}. "
        f"Unauthorized AND not whitelisted: {unauthorized_unhitelisted}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
