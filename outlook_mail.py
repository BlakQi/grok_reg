from __future__ import annotations

import base64
import imaplib
import json
import os
import random
import re
import time
from datetime import datetime, timezone
from email import message_from_bytes, policy
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests

OUTLOOK_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
OUTLOOK_GRAPH_MESSAGES_URL = "https://graph.microsoft.com/v1.0/me/messages"
OUTLOOK_GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
OUTLOOK_IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
OUTLOOK_DEFAULT_IMAP_HOST = "outlook.office365.com"
OUTLOOK_IN_USE_STALE_SECONDS = 3600
OUTLOOK_UNAVAILABLE_STATES = {"reserved", "used", "login_required", "token_invalid", "failed"}
OUTLOOK_FATAL_STATES = {"login_required", "token_invalid"}


class OutlookTokenError(RuntimeError):
    pass


class OutlookTokenRateLimitError(OutlookTokenError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(value: Any) -> str:
    return str(value or "").replace("\ufeff", "").replace("\xa0", " ").strip()


def _load_state(state_file: Path) -> dict[str, dict[str, str]]:
    if not state_file.exists():
        return {}
    try:
        data = json.loads(state_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return {}
    result: dict[str, dict[str, str]] = {}
    if isinstance(data, list):
        for item in data:
            key = _clean(item).lower()
            if key:
                result[key] = {"state": "used", "reason": "", "updated_at": ""}
    elif isinstance(data, dict):
        for key, value in data.items():
            email = _clean(key).lower()
            if not email:
                continue
            if isinstance(value, dict):
                result[email] = {
                    "state": _clean(value.get("state") or "used") or "used",
                    "reason": _clean(value.get("reason")),
                    "updated_at": _clean(value.get("updated_at")),
                }
            else:
                result[email] = {"state": _clean(value or "used") or "used", "reason": "", "updated_at": ""}
    return result


def _save_state(state_file: Path, state: dict[str, dict[str, str]]) -> None:
    state_file.parent.mkdir(parents=True, exist_ok=True)
    ordered = {key: state[key] for key in sorted(state)}
    state_file.write_text(json.dumps(ordered, ensure_ascii=False, indent=2), encoding="utf-8")


def _entry_available(entry: dict[str, str] | None) -> bool:
    if not isinstance(entry, dict):
        return True
    state = _clean(entry.get("state"))
    if state in OUTLOOK_UNAVAILABLE_STATES:
        return False
    if state == "in_use":
        try:
            updated = datetime.fromisoformat(_clean(entry.get("updated_at")))
            if updated.tzinfo is None:
                updated = updated.replace(tzinfo=timezone.utc)
            return (datetime.now(timezone.utc) - updated).total_seconds() >= OUTLOOK_IN_USE_STALE_SECONDS
        except Exception:
            return True
    return True


def parse_credentials(text: str) -> tuple[list[dict[str, str]], dict[str, Any]]:
    credentials: list[dict[str, str]] = []
    seen: set[str] = set()
    report: dict[str, Any] = {"valid": 0, "duplicates": 0, "invalid": 0, "issues": []}
    for line_no, raw_line in enumerate(str(text or "").splitlines(), 1):
        line = _clean(raw_line)
        if not line:
            continue
        parts = [_clean(part) for part in line.split("----", 3)]
        if len(parts) != 4:
            report["invalid"] += 1
            if len(report["issues"]) < 10:
                report["issues"].append({"line": line_no, "reason": "expected email----password----client_id----refresh_token"})
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or not client_id or not refresh_token:
            report["invalid"] += 1
            if len(report["issues"]) < 10:
                report["issues"].append({"line": line_no, "email": email, "reason": "missing email/client_id/refresh_token"})
            continue
        key = email.lower()
        if key in seen:
            report["duplicates"] += 1
            continue
        seen.add(key)
        credentials.append({"email": email, "password": password, "client_id": client_id, "refresh_token": refresh_token})
    report["valid"] = len(credentials)
    return credentials, report


def load_credentials(pool_file: Path) -> list[dict[str, str]]:
    if not pool_file.exists():
        return []
    return parse_credentials(pool_file.read_text(encoding="utf-8-sig"))[0]


def save_credentials(pool_file: Path, credentials: list[dict[str, str]]) -> None:
    pool_file.parent.mkdir(parents=True, exist_ok=True)
    rows = [
        f"{item['email']}----{item.get('password', '')}----{item['client_id']}----{item['refresh_token']}"
        for item in credentials
    ]
    pool_file.write_text(("\n".join(rows) + ("\n" if rows else "")), encoding="utf-8")


def import_credentials(pool_file: Path, raw_text: str) -> dict[str, Any]:
    incoming, report = parse_credentials(raw_text)
    current = load_credentials(pool_file)
    merged = {item["email"].lower(): dict(item) for item in current}
    imported = 0
    updated = 0
    for item in incoming:
        key = item["email"].lower()
        if key in merged:
            updated += 1
        else:
            imported += 1
        merged[key] = item
    save_credentials(pool_file, list(merged.values()))
    return {
        "imported": imported,
        "updated": updated,
        "valid": report["valid"],
        "duplicates": report["duplicates"],
        "invalid": report["invalid"],
        "issues": report["issues"],
        "total": len(merged),
    }


def alias_supported(email: str) -> bool:
    _, sep, domain = _clean(email).lower().partition("@")
    return bool(sep) and (
        domain in {"outlook.com", "hotmail.com", "live.com", "msn.com"}
        or domain.startswith("outlook.")
        or domain.startswith("hotmail.")
    )


def alias_address(email: str, tag: str) -> str:
    local, sep, domain = _clean(email).partition("@")
    if not sep:
        return email
    return f"{local.split('+', 1)[0]}+{tag}@{domain}"


def expand_aliases(
    credentials: list[dict[str, str]],
    *,
    alias_count: int = 0,
    include_original: bool = True,
    prefix: str = "grok",
) -> list[dict[str, str]]:
    alias_count = max(0, min(int(alias_count or 0), 200))
    clean_prefix = re.sub(r"[^A-Za-z0-9._-]+", "", str(prefix or "").strip()) or "grok"
    expanded: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in credentials:
        original = _clean(item.get("login_email") or item.get("alias_of") or item.get("email"))
        if include_original:
            key = _clean(item.get("email")).lower()
            if key and key not in seen:
                expanded.append(dict(item))
                seen.add(key)
        if alias_count <= 0 or not alias_supported(original):
            continue
        for index in range(1, alias_count + 1):
            alias = alias_address(original, f"{clean_prefix}{index}")
            key = alias.lower()
            if key in seen:
                continue
            expanded.append({**item, "email": alias, "login_email": original, "alias_of": original})
            seen.add(key)
    return expanded


def pool_stats(pool_file: Path, state_file: Path, cfg: dict[str, Any] | None = None) -> dict[str, int]:
    cfg = cfg or {}
    base = load_credentials(pool_file)
    expanded = expand_aliases(
        base,
        alias_count=int(cfg.get("outlook_alias_count") or 0),
        include_original=bool(cfg.get("outlook_alias_include_original", True)),
        prefix=str(cfg.get("outlook_alias_prefix") or "grok"),
    )
    state = _load_state(state_file)
    counts = {"base": len(base), "expanded": len(expanded), "unused": 0, "reserved": 0, "in_use": 0, "used": 0, "failed": 0, "login_required": 0, "token_invalid": 0}
    for item in expanded:
        key = _clean(item.get("email")).lower()
        entry = state.get(key, {})
        current = _clean(entry.get("state"))
        parent = _clean(item.get("login_email") or item.get("alias_of")).lower()
        parent_state = _clean(state.get(parent, {}).get("state")) if parent and parent != key else ""
        if current not in counts and parent_state in OUTLOOK_FATAL_STATES:
            current = parent_state
        if current in counts:
            counts[current] += 1
        else:
            counts["unused"] += 1
    counts["available"] = counts["unused"]
    counts["invalid"] = counts["login_required"] + counts["token_invalid"]
    return counts


def _credential_available(state: dict[str, dict[str, str]], credential: dict[str, str]) -> bool:
    key = _clean(credential.get("email")).lower()
    if not _entry_available(state.get(key)):
        return False
    parent = _clean(credential.get("login_email") or credential.get("alias_of")).lower()
    if parent and parent != key and _clean(state.get(parent, {}).get("state")) in OUTLOOK_FATAL_STATES:
        return False
    return True


def acquire_mailbox(pool_file: Path, state_file: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    cfg = cfg or {}
    base = load_credentials(pool_file)
    pool = expand_aliases(
        base,
        alias_count=int(cfg.get("outlook_alias_count") or 0),
        include_original=bool(cfg.get("outlook_alias_include_original", True)),
        prefix=str(cfg.get("outlook_alias_prefix") or "grok"),
    )
    if not pool:
        raise RuntimeError("outlook_pool_empty")
    state = _load_state(state_file)
    random.shuffle(pool)
    credential = next((item for item in pool if _credential_available(state, item)), None)
    if credential is None:
        raise RuntimeError("outlook_pool_no_available_mailbox")
    address = _clean(credential["email"])
    state[address.lower()] = {"state": "reserved", "reason": "acquired", "updated_at": _now_iso()}
    _save_state(state_file, state)
    return {
        "provider": "outlook",
        "address": address,
        "login_email": _clean(credential.get("login_email") or credential.get("alias_of") or credential["email"]),
        "alias_of": _clean(credential.get("alias_of")),
        "password": _clean(credential.get("password")),
        "client_id": _clean(credential.get("client_id")),
        "refresh_token": _clean(credential.get("refresh_token")),
        "mode": _clean(cfg.get("outlook_mode") or "auto").lower() or "auto",
        "imap_host": _clean(cfg.get("outlook_imap_host") or OUTLOOK_DEFAULT_IMAP_HOST) or OUTLOOK_DEFAULT_IMAP_HOST,
        "message_limit": max(1, int(cfg.get("outlook_message_limit") or 10)),
    }


def mark_result(state_file: Path, address: str, success: bool, reason: str = "", login_email: str = "") -> None:
    target = _clean(address).lower()
    if not target:
        return
    state = _load_state(state_file)
    if success:
        state[target] = {"state": "used", "reason": "", "updated_at": _now_iso()}
    else:
        lowered = str(reason or "").lower()
        if "aadsts90055" in lowered or "429" in lowered or "rate limit" in lowered:
            next_state = "failed"
        elif "invalid_grant" in lowered or "access_token" in lowered or "refresh" in lowered:
            next_state = "token_invalid"
        elif "login_required" in lowered:
            next_state = "login_required"
        else:
            next_state = "failed"
        state[target] = {"state": next_state, "reason": str(reason or "")[:300], "updated_at": _now_iso()}
        parent = _clean(login_email).lower()
        if parent and parent != target and next_state in OUTLOOK_FATAL_STATES:
            state[parent] = {"state": next_state, "reason": str(reason or "")[:300], "updated_at": _now_iso()}
    _save_state(state_file, state)


def mark_reserved(state_file: Path, address: str, reason: str = "acquired") -> None:
    target = _clean(address).lower()
    if not target:
        return
    state = _load_state(state_file)
    current = _clean(state.get(target, {}).get("state"))
    if current in OUTLOOK_UNAVAILABLE_STATES:
        return
    state[target] = {"state": "reserved", "reason": str(reason or "")[:300], "updated_at": _now_iso()}
    _save_state(state_file, state)


def release_mailbox(state_file: Path, address: str) -> None:
    target = _clean(address).lower()
    if not target:
        return
    state = _load_state(state_file)
    if _clean(state.get(target, {}).get("state")) == "in_use":
        state.pop(target, None)
        _save_state(state_file, state)


def reset_state(state_file: Path, scope: str = "all") -> int:
    state = _load_state(state_file)
    if not state:
        return 0
    scope = _clean(scope).lower() or "all"
    if scope in {"failed", "retryable"}:
        targets = {"failed", "in_use"}
    elif scope in {"invalid", "reauth"}:
        targets = {"login_required", "token_invalid"}
    elif scope in {"busy", "in_use"}:
        targets = {"in_use"}
    else:
        count = len(state)
        _save_state(state_file, {})
        return count
    removed = [key for key, value in state.items() if _clean(value.get("state")) in targets]
    for key in removed:
        state.pop(key, None)
    _save_state(state_file, state)
    return len(removed)


def encode_mailbox_token(mailbox: dict[str, Any]) -> str:
    raw = json.dumps(mailbox, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "outlook:" + base64.urlsafe_b64encode(raw).decode("ascii")


def decode_mailbox_token(token: str) -> dict[str, Any]:
    text = str(token or "")
    if not text.startswith("outlook:"):
        raise RuntimeError("invalid_outlook_token")
    raw = base64.urlsafe_b64decode(text.split(":", 1)[1].encode("ascii"))
    data = json.loads(raw.decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("invalid_outlook_token_payload")
    return data


def _request_proxies(proxy: str = "") -> dict[str, str] | None:
    proxy = _clean(proxy or os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _exchange_refresh_token(client_id: str, refresh_token: str, scope: str, user_agent: str, proxy: str = "") -> str:
    last_status = 0
    last_detail = ""
    for attempt in range(3):
        resp = requests.post(
            OUTLOOK_TOKEN_URL,
            data={"client_id": client_id, "grant_type": "refresh_token", "refresh_token": refresh_token, "scope": scope},
            headers={"Content-Type": "application/x-www-form-urlencoded", "User-Agent": user_agent or "Mozilla/5.0"},
            timeout=20,
            proxies=_request_proxies(proxy),
        )
        try:
            data = resp.json()
        except Exception:
            data = {}
        if resp.status_code == 200:
            access_token = _clean(data.get("access_token"))
            if access_token:
                return access_token
            raise OutlookTokenError("missing_access_token")
        detail = _clean(data.get("error_description") or data.get("error") or resp.text[:300])
        last_status = int(resp.status_code)
        last_detail = detail
        if last_status == 429 or "aadsts90055" in detail.lower():
            time.sleep(min(30.0, 1.5 * (attempt + 1) + random.uniform(0.5, 1.5)))
            continue
        raise OutlookTokenError(f"token_refresh_failed_http_{last_status}: {detail}")
    raise OutlookTokenRateLimitError(f"token_refresh_rate_limited_http_{last_status}: {last_detail}")


def _graph_messages(mailbox: dict[str, Any], access_token: str, user_agent: str, proxy: str = "") -> list[dict[str, Any]]:
    resp = requests.get(
        OUTLOOK_GRAPH_MESSAGES_URL,
        headers={"Authorization": f"Bearer {access_token}", "Accept": "application/json", "User-Agent": user_agent or "Mozilla/5.0"},
        params={"$top": int(mailbox.get("message_limit") or 10), "$orderby": "receivedDateTime desc", "$select": "subject,receivedDateTime,from,toRecipients,ccRecipients,body,bodyPreview"},
        timeout=20,
        proxies=_request_proxies(proxy),
    )
    try:
        data = resp.json()
    except Exception:
        data = {}
    if resp.status_code != 200:
        detail = data.get("error", {}).get("message") if isinstance(data.get("error"), dict) else resp.text[:300]
        raise RuntimeError(f"graph_failed_http_{resp.status_code}: {detail}")
    items = data.get("value") if isinstance(data, dict) else []
    result: list[dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        body = item.get("body") if isinstance(item.get("body"), dict) else {}
        recipients: list[str] = []
        for key in ("toRecipients", "ccRecipients"):
            for addr in item.get(key) or []:
                email_addr = addr.get("emailAddress") if isinstance(addr, dict) else {}
                if isinstance(email_addr, dict):
                    value = _clean(email_addr.get("address") or email_addr.get("name"))
                    if value:
                        recipients.append(value)
        result.append({
            "id": _clean(item.get("id")),
            "subject": _clean(item.get("subject")),
            "to": recipients,
            "text": _clean(item.get("bodyPreview")),
            "html": _clean(body.get("content")) if _clean(body.get("contentType")).lower() == "html" else "",
            "received_at": _clean(item.get("receivedDateTime")),
        })
    return result


def _decode_header(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text)))
    except Exception:
        return text


def _imap_messages(mailbox: dict[str, Any], access_token: str) -> list[dict[str, Any]]:
    auth = f"user={mailbox.get('login_email') or mailbox['address']}\x01auth=Bearer {access_token}\x01\x01"
    imap = imaplib.IMAP4_SSL(_clean(mailbox.get("imap_host")) or OUTLOOK_DEFAULT_IMAP_HOST)
    try:
        imap.authenticate("XOAUTH2", lambda _: auth.encode("utf-8"))
        status, _ = imap.select("INBOX", readonly=True)
        if status != "OK":
            raise RuntimeError("imap_select_failed")
        status, data = imap.uid("search", None, "ALL")
        if status != "OK" or not data or not data[0]:
            return []
        limit = int(mailbox.get("message_limit") or 10)
        messages: list[dict[str, Any]] = []
        for uid in reversed(data[0].split()[-limit:]):
            status, fetched = imap.uid("fetch", uid, "(RFC822)")
            if status != "OK":
                continue
            raw = next((part[1] for part in fetched if isinstance(part, tuple) and isinstance(part[1], bytes)), b"")
            if not raw:
                continue
            message = message_from_bytes(raw, policy=policy.default)
            plain: list[str] = []
            html: list[str] = []
            for part in message.walk() if message.is_multipart() else [message]:
                if part.get_content_maintype() == "multipart":
                    continue
                try:
                    payload = part.get_content()
                except Exception:
                    continue
                if part.get_content_type() == "text/html":
                    html.append(str(payload))
                else:
                    plain.append(str(payload))
            received_at = ""
            try:
                received = parsedate_to_datetime(str(message.get("Date") or ""))
                received_at = received.isoformat()
            except Exception:
                pass
            messages.append({
                "id": _decode_header(message.get("Message-ID")),
                "subject": _decode_header(message.get("Subject")),
                "to": [_decode_header(message.get("To")), _decode_header(message.get("Delivered-To")), _decode_header(message.get("X-Original-To"))],
                "text": "\n".join(plain),
                "html": "\n".join(html),
                "received_at": received_at,
            })
        return messages
    finally:
        try:
            imap.logout()
        except Exception:
            pass


def _message_matches_email(message: dict[str, Any], target: str) -> bool:
    target = _clean(target).lower()
    if not target:
        return True
    values: list[str] = []
    for key in ("to", "delivered_to", "x_original_to"):
        value = message.get(key)
        if isinstance(value, list):
            values.extend(_clean(v).lower() for v in value)
        else:
            values.append(_clean(value).lower())
    haystack = "\n".join(values + [_clean(message.get("text")).lower(), _clean(message.get("html")).lower()])
    return target in haystack


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", " ", value or "")


def wait_for_code(
    mailbox: dict[str, Any],
    *,
    extract_code: Callable[[str, str], str | None],
    timeout: float = 180,
    poll_interval: float = 3,
    user_agent: str = "Mozilla/5.0",
    proxy: str = "",
    log_callback: Callable[[str], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> str:
    client_id = _clean(mailbox.get("client_id"))
    refresh_token = _clean(mailbox.get("refresh_token"))
    if not client_id or not refresh_token:
        raise RuntimeError("outlook_missing_client_id_or_refresh_token")
    mode = _clean(mailbox.get("mode") or "auto").lower()
    if mode not in {"auto", "graph", "imap"}:
        mode = "auto"
    token_cache: dict[str, tuple[str, float]] = {}

    def access_token(scope: str) -> str:
        cached = token_cache.get(scope)
        if cached and time.monotonic() < cached[1]:
            return cached[0]
        token = _exchange_refresh_token(client_id, refresh_token, scope, user_agent, proxy)
        token_cache[scope] = (token, time.monotonic() + 600)
        return token

    deadline = time.monotonic() + float(timeout or 180)
    seen: set[str] = set()
    last_error = ""
    disabled_modes: set[str] = set()
    while time.monotonic() < deadline:
        if cancel_callback and cancel_callback():
            raise RuntimeError("cancelled")
        messages: list[dict[str, Any]] = []
        errors: list[str] = []
        if mode in {"auto", "graph"} and "graph" not in disabled_modes:
            try:
                messages = _graph_messages(mailbox, access_token(OUTLOOK_GRAPH_SCOPE), user_agent, proxy)
            except OutlookTokenRateLimitError:
                raise
            except OutlookTokenError as exc:
                errors.append(f"graph: {exc}")
                if mode == "graph":
                    raise
                disabled_modes.add("graph")
            except Exception as exc:
                errors.append(f"graph: {exc}")
                if mode == "graph":
                    raise
        if not messages and mode in {"auto", "imap"} and "imap" not in disabled_modes:
            try:
                messages = _imap_messages(mailbox, access_token(OUTLOOK_IMAP_SCOPE))
            except OutlookTokenRateLimitError:
                raise
            except OutlookTokenError as exc:
                errors.append(f"imap: {exc}")
                if mode == "imap":
                    raise
                disabled_modes.add("imap")
            except Exception as exc:
                errors.append(f"imap: {exc}")
                if mode == "imap":
                    raise
        if mode == "auto" and {"graph", "imap"}.issubset(disabled_modes):
            raise OutlookTokenError("; ".join(errors) or last_error or "outlook_token_invalid")
        if errors:
            last_error = "; ".join(errors)
            if log_callback:
                log_callback(f"[Debug] Outlook mail fetch: {last_error}")
        if log_callback:
            log_callback(f"[Debug] Outlook messages: {len(messages)}")
        for message in messages:
            ref = _clean(message.get("id")) or _clean(message.get("subject"))
            if ref and ref in seen:
                continue
            if ref:
                seen.add(ref)
            if not _message_matches_email(message, _clean(mailbox.get("address"))):
                continue
            subject = _clean(message.get("subject"))
            combined = "\n".join([_clean(message.get("text")), _strip_html(_clean(message.get("html")))])
            code = extract_code(combined, subject)
            if code:
                return code
        time.sleep(max(0.2, float(poll_interval or 3)))
    raise RuntimeError(f"outlook_code_timeout: {last_error}")
