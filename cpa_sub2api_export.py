"""Export local CPA auth files as raw CPA zip or merged sub2api JSON."""

from __future__ import annotations

import base64
import io
import json
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REQUIRED_ACCESS_TOKEN_REFERRER = "grok-build"


def _now_stamp() -> str:
    return datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H-%M-%S")


def _iso_now() -> str:
    return datetime.now(tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _strip_empty(value: Any) -> Any:
    if isinstance(value, list):
        out = [_strip_empty(item) for item in value]
        return [item for item in out if item is not None]
    if isinstance(value, dict):
        out = {
            key: _strip_empty(item)
            for key, item in value.items()
            if item is not None and item != ""
        }
        return out or None
    if value is None or value == "":
        return None
    return value


def _email_key(email: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (email or "").strip().lower()).strip("_")


def _safe_arcname(path: Path) -> str:
    name = path.name.replace("\\", "-").replace("/", "-")
    return name or "account.json"


def _jwt_payload(token: str) -> dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        raise ValueError("access_token is not a JWT")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))


def _validate_access_token_referrer(data: dict[str, Any]) -> None:
    payload = _jwt_payload(str(data.get("access_token") or ""))
    referrer = str(payload.get("referrer") or "").strip()
    if referrer != REQUIRED_ACCESS_TOKEN_REFERRER:
        raise ValueError(
            f"missing access_token referrer={REQUIRED_ACCESS_TOKEN_REFERRER}; regenerate CPA"
        )


def iter_cpa_files(cpa_dir: Path) -> list[Path]:
    if not cpa_dir.exists():
        return []
    return sorted(
        path
        for path in cpa_dir.glob("*.json")
        if path.is_file() and not path.name.startswith(".")
    )


def load_cpa_records(cpa_dir: Path) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    records: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for path in iter_cpa_files(cpa_dir):
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if not isinstance(data, dict):
                raise ValueError("json root is not object")
            if not data.get("access_token"):
                raise ValueError("missing access_token")
            _validate_access_token_referrer(data)
            records.append({"path": path, "data": data})
        except Exception as exc:
            skipped.append({"file": path.name, "reason": str(exc)})
    return records, skipped


def cpa_record_to_sub2api_account(record: dict[str, Any], source_name: str = "") -> dict[str, Any]:
    source_type = str(record.get("type") or "xai").strip().lower()
    platform = "openai" if source_type == "xai" else source_type
    email = str(record.get("email") or "").strip()
    name = email or source_name or str(record.get("sub") or "").strip() or "xai-account"

    credentials = _strip_empty({
        "access_token": record.get("access_token"),
        "refresh_token": record.get("refresh_token"),
        "id_token": record.get("id_token"),
        "token_type": record.get("token_type"),
        "expires_in": record.get("expires_in"),
        "expires_at": record.get("expired") or record.get("expires_at") or record.get("expires"),
        "auth_kind": record.get("auth_kind"),
        "email": email,
        "sub": record.get("sub"),
        "base_url": record.get("base_url"),
        "token_endpoint": record.get("token_endpoint"),
        "redirect_uri": record.get("redirect_uri"),
        "headers": record.get("headers"),
        "disabled": record.get("disabled") if isinstance(record.get("disabled"), bool) else None,
    }) or {}

    extra = _strip_empty({
        "email": email,
        "email_key": _email_key(email),
        "sub": record.get("sub"),
        "last_refresh": record.get("last_refresh"),
        "source_file": source_name,
    }) or {}

    return _strip_empty({
        "name": name,
        "platform": platform,
        "type": "oauth",
        "concurrency": 10,
        "priority": 1,
        "credentials": credentials,
        "extra": extra,
    }) or {}


def build_sub2api_document(cpa_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    records, skipped = load_cpa_records(cpa_dir)
    accounts = [
        cpa_record_to_sub2api_account(item["data"], item["path"].name)
        for item in records
    ]
    document = {
        "exported_at": _iso_now(),
        "proxies": [],
        "accounts": accounts,
    }
    summary = {
        "format": "sub2api",
        "count": len(accounts),
        "skipped": skipped,
        "filename": f"sub2api-{_now_stamp()}.json",
    }
    return document, summary


def build_cpa_zip(cpa_dir: Path) -> tuple[bytes, dict[str, Any]]:
    records, skipped = load_cpa_records(cpa_dir)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in records:
            path = item["path"]
            zf.write(path, _safe_arcname(path))
        manifest = {
            "exported_at": _iso_now(),
            "source_dir": str(cpa_dir),
            "count": len(records),
            "skipped": skipped,
        }
        zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    summary = {
        "format": "cpa",
        "count": len(records),
        "skipped": skipped,
        "filename": f"cpa-auths-{_now_stamp()}.zip",
    }
    return buf.getvalue(), summary


def export_summary(cpa_dir: Path) -> dict[str, Any]:
    records, skipped = load_cpa_records(cpa_dir)
    return {
        "dir": str(cpa_dir),
        "total_json": len(iter_cpa_files(cpa_dir)),
        "valid_cpa": len(records),
        "skipped": skipped,
    }
