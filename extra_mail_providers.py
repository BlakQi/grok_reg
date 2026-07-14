from __future__ import annotations

import base64
import json
import os
import random
import re
import secrets
import string
import time
from datetime import datetime, timezone
from email import message_from_string, policy
from pathlib import Path
from typing import Any, Callable

from curl_cffi import requests

BASE_DIR = Path(__file__).resolve().parent
DDG_ALIASES_FILE = BASE_DIR / "ddg_aliases.json"
SUPPORTED_PROVIDERS = {
    "tempmail_lol",
    "gptmail",
    "donemail",
    "done_mail",
    "moemail",
    "inbucket",
    "ddg_mail",
}


def is_supported(provider: str) -> bool:
    return str(provider or "").strip().lower() in SUPPORTED_PROVIDERS


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _csv(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean(v) for v in value if _clean(v)]
    return [x.strip() for x in re.split(r"[,，\s]+", str(value or "")) if x.strip()]


def _bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


def _int(value: Any, default: int = 0, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        result = int(value)
    except Exception:
        result = default
    if minimum is not None:
        result = max(minimum, result)
    if maximum is not None:
        result = min(maximum, result)
    return result


def _username(length: int = 10) -> str:
    chars = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(chars) for _ in range(length))


def _subdomain() -> str:
    return "".join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(random.randint(4, 10)))


def _pick(values: list[str]) -> str:
    if not values:
        return ""
    return values[random.randrange(len(values))]


def _proxy(cfg: dict[str, Any]) -> dict[str, str]:
    from proxy_runtime import proxy_candidates, resolve_config_proxy

    if proxy_candidates(cfg):
        value = resolve_config_proxy(cfg)
    else:
        value = ""
    return {"http": value, "https": value} if value else {}


def _request(cfg: dict[str, Any], method: str, url: str, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.setdefault("User-Agent", _clean(cfg.get("user_agent")) or "Mozilla/5.0")
    kwargs.setdefault("timeout", 20)
    kwargs.setdefault("proxies", _proxy(cfg))
    return requests.request(method.upper(), url, headers=headers, **kwargs)


def _json_response(resp, provider: str, action: str, expected: tuple[int, ...] = (200,)):
    if resp.status_code not in expected:
        raise RuntimeError(f"{provider}_{action}_http_{resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except Exception:
        raise RuntimeError(f"{provider}_{action}_non_json: {resp.text[:300]}")


def _provider_token(mailbox: dict[str, Any]) -> str:
    raw = json.dumps(mailbox, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return "extra:" + base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_token(token: str) -> dict[str, Any]:
    text = str(token or "")
    if not text.startswith("extra:"):
        raise RuntimeError("invalid_extra_mail_token")
    data = json.loads(base64.urlsafe_b64decode(text.split(":", 1)[1].encode("ascii")).decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("invalid_extra_mail_token_payload")
    return data


def _parse_received(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        return 0.0
    try:
        dt = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", " ", text or "")


def _extract_content(item: dict[str, Any]) -> tuple[str, str]:
    text = _clean(item.get("text_content") or item.get("text") or item.get("content") or item.get("body") or item.get("bodyPreview"))
    html = item.get("html_content") or item.get("html") or item.get("html_body") or item.get("body_html") or ""
    if isinstance(html, list):
        html = "\n".join(str(x) for x in html)
    if text or html:
        return str(text), str(html)
    raw = _clean(item.get("raw"))
    if not raw:
        return "", ""
    try:
        msg = message_from_string(raw, policy=policy.default)
        plain: list[str] = []
        html_parts: list[str] = []
        for part in msg.walk() if msg.is_multipart() else [msg]:
            if part.get_content_maintype() == "multipart":
                continue
            payload = part.get_content()
            if part.get_content_type() == "text/html":
                html_parts.append(str(payload))
            else:
                plain.append(str(payload))
        return "\n".join(plain), "\n".join(html_parts)
    except Exception:
        return raw, ""


def _field_values(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        result: list[str] = []
        for key in ("address", "email", "name", "value"):
            if key in value:
                result.extend(_field_values(value[key]))
        return result
    if isinstance(value, list):
        result: list[str] = []
        for item in value:
            result.extend(_field_values(item))
        return result
    return []


def _matches_address(item: dict[str, Any], address: str) -> bool:
    target = address.lower()
    values: list[str] = []
    for key in ("to", "toEmail", "mailTo", "receiver", "receivers", "address", "email", "delivered_to", "x_original_to"):
        if key in item:
            values.extend(_field_values(item[key]))
    if not values:
        return True
    return any(target in value.lower() for value in values if value)


def _normalize_message(provider: str, mailbox: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    text, html = _extract_content(item)
    sender = item.get("from") or item.get("sender") or item.get("from_address") or ""
    if isinstance(sender, dict):
        sender = sender.get("address") or sender.get("email") or sender.get("name") or ""
    return {
        "provider": provider,
        "mailbox": mailbox.get("address", ""),
        "message_id": _clean(item.get("id") or item.get("_id") or item.get("messageId") or item.get("message_id") or item.get("token")),
        "subject": _clean(item.get("subject")),
        "sender": _clean(sender),
        "text_content": text,
        "html_content": html,
        "received": _parse_received(item.get("receivedAt") or item.get("received_at") or item.get("createdAt") or item.get("created_at") or item.get("date") or item.get("timestamp")),
        "raw": item,
    }


def _list_payload(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in ("data", "emails", "messages", "mails", "items", "results", "hydra:member"):
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
            if isinstance(value, dict):
                nested = _list_payload(value)
                if nested:
                    return nested
    return []


def _latest(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not items:
        return None
    return max(items, key=lambda x: (_parse_received(x.get("receivedAt") or x.get("received_at") or x.get("createdAt") or x.get("created_at") or x.get("date") or x.get("timestamp")), _clean(x.get("id") or x.get("_id"))))


def create_mailbox(cfg: dict[str, Any]) -> dict[str, Any]:
    provider = _clean(cfg.get("email_provider")).lower()
    if provider == "tempmail_lol":
        domains = _csv(cfg.get("tempmail_lol_domains") or cfg.get("defaultDomains"))
        payload: dict[str, Any] = {}
        domain = _pick(domains)
        if domain:
            if domain.startswith("*."):
                payload["domain"] = f"{_subdomain()}.{domain[2:]}"
                payload["prefix"] = _username()
            else:
                payload["domain"] = domain
        key = _clean(cfg.get("tempmail_lol_api_key"))
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = _json_response(_request(cfg, "POST", "https://api.tempmail.lol/v2/inbox/create", headers=headers, json=payload), provider, "create", (200, 201))
        return {"provider": provider, "address": _clean(data.get("address")), "token": _clean(data.get("token"))}

    if provider == "gptmail":
        api_base = (_clean(cfg.get("gptmail_api_base")) or "https://mail.chatgpt.org.uk").rstrip("/")
        domain = _clean(cfg.get("gptmail_default_domain") or cfg.get("defaultDomains")).split(",", 1)[0].strip()
        if _bool(cfg.get("gptmail_local_compose"), False):
            if not domain:
                raise RuntimeError("gptmail_default_domain_required")
            return {"provider": provider, "address": f"{_username()}@{domain}", "api_base": api_base}
        key = _clean(cfg.get("gptmail_api_key"))
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if key:
            headers["X-API-Key"] = key
        payload = {k: v for k, v in {"prefix": _username(), "domain": domain}.items() if v}
        data = _json_response(_request(cfg, "POST", f"{api_base}/api/generate-email", headers=headers, json=payload), provider, "create")
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            data = data["data"]
        return {"provider": provider, "address": _clean(data.get("email")), "api_base": api_base, "api_key": key}

    if provider in {"donemail", "done_mail"}:
        api_base = _clean(cfg.get("donemail_api_base")).rstrip("/")
        domains = _csv(cfg.get("donemail_domains") or cfg.get("defaultDomains"))
        if not api_base or not domains:
            raise RuntimeError("donemail_api_base_and_domain_required")
        prefix = _clean(cfg.get("donemail_email_prefix"))
        local = f"{prefix}_{_username()}" if prefix else _username()
        return {"provider": "donemail", "address": f"{local}@{_pick(domains)}", "api_base": api_base, "admin_key": _clean(cfg.get("donemail_admin_key")), "message_limit": _int(cfg.get("donemail_message_limit"), 20, 1, 50)}

    if provider == "moemail":
        api_base = _clean(cfg.get("moemail_api_base")).rstrip("/")
        key = _clean(cfg.get("moemail_api_key"))
        domains = _csv(cfg.get("moemail_domains") or cfg.get("defaultDomains"))
        if not api_base or not key or not domains:
            raise RuntimeError("moemail_api_base_key_domain_required")
        payload = {"name": _username(), "expiryTime": _int(cfg.get("moemail_expiry_time"), 0), "domain": _pick(domains)}
        data = _json_response(_request(cfg, "POST", f"{api_base}/api/emails/generate", headers={"X-API-Key": key, "Content-Type": "application/json"}, json=payload), provider, "create", (200, 201))
        return {"provider": provider, "address": _clean(data.get("email")), "email_id": _clean(data.get("id") or data.get("email_id")), "api_base": api_base, "api_key": key}

    if provider == "inbucket":
        api_base = _clean(cfg.get("inbucket_api_base")).rstrip("/")
        domains = _csv(cfg.get("inbucket_domains") or cfg.get("defaultDomains"))
        if not api_base or not domains:
            raise RuntimeError("inbucket_api_base_and_domain_required")
        base_domain = _pick(domains)
        domain = f"{_subdomain()}.{base_domain}" if _bool(cfg.get("inbucket_random_subdomain"), True) else base_domain
        local = _username()
        return {"provider": provider, "address": f"{local}@{domain}", "mailbox_name": local, "api_base": api_base}

    if provider == "ddg_mail":
        ddg_token = _clean(cfg.get("ddg_token"))
        inbox_jwt = _clean(cfg.get("ddg_cf_inbox_jwt"))
        cf_base = _clean(cfg.get("ddg_cf_api_base") or cfg.get("cloudflare_api_base")).rstrip("/")
        if not ddg_token or not inbox_jwt or not cf_base:
            raise RuntimeError("ddg_token_cf_api_base_cf_inbox_jwt_required")
        data = _json_response(_request(cfg, "POST", "https://quack.duckduckgo.com/api/email/addresses", headers={"Authorization": f"Bearer {ddg_token}", "Content-Type": "application/json"}, json={}), provider, "create", (200, 201))
        alias = _clean(data.get("address"))
        if not alias:
            raise RuntimeError("ddg_missing_address")
        address = f"{alias}@duck.com"
        used = _load_ddg_aliases()
        used.add(address.lower())
        DDG_ALIASES_FILE.write_text(json.dumps(sorted(used), indent=2), encoding="utf-8")
        return {"provider": provider, "address": address, "token": inbox_jwt, "cf_base": cf_base, "messages_path": _clean(cfg.get("ddg_cf_messages_path") or "/api/mails")}

    raise RuntimeError(f"unsupported_extra_provider:{provider}")


def _load_ddg_aliases() -> set[str]:
    if not DDG_ALIASES_FILE.exists():
        return set()
    try:
        data = json.loads(DDG_ALIASES_FILE.read_text(encoding="utf-8-sig"))
        return {str(x).strip().lower() for x in data if str(x).strip()}
    except Exception:
        return set()


def get_email_and_token(cfg: dict[str, Any]) -> tuple[str, str]:
    mailbox = create_mailbox(cfg)
    if not mailbox.get("address"):
        raise RuntimeError("mailbox_missing_address")
    return mailbox["address"], _provider_token(mailbox)


def _fetch_messages(cfg: dict[str, Any], mailbox: dict[str, Any]) -> list[dict[str, Any]]:
    provider = mailbox["provider"]
    address = mailbox.get("address", "")
    if provider == "tempmail_lol":
        data = _json_response(_request(cfg, "GET", "https://api.tempmail.lol/v2/inbox", params={"token": mailbox["token"]}), provider, "messages")
        return _list_payload(data)
    if provider == "gptmail":
        api_base = mailbox["api_base"].rstrip("/")
        headers = {"Accept": "application/json"}
        if mailbox.get("api_key"):
            headers["X-API-Key"] = mailbox["api_key"]
        data = _json_response(_request(cfg, "GET", f"{api_base}/api/emails", headers=headers, params={"email": address}), provider, "messages")
        if isinstance(data, dict) and "data" in data:
            data = data["data"]
        items = _list_payload(data)
        latest = _latest(items)
        if latest and latest.get("id"):
            detail = _json_response(_request(cfg, "GET", f"{api_base}/api/email/{latest['id']}", headers=headers), provider, "message_detail")
            return [detail.get("data", detail) if isinstance(detail, dict) else latest]
        return items
    if provider == "donemail":
        api_base = mailbox["api_base"].rstrip("/")
        data = _json_response(_request(cfg, "GET", f"{api_base}/api/mails", headers={"X-Admin-Key": mailbox.get("admin_key", "")}, params={"limit": mailbox.get("message_limit", 20), "to": address}), provider, "messages")
        return _list_payload(data)
    if provider == "moemail":
        api_base = mailbox["api_base"].rstrip("/")
        key = mailbox["api_key"]
        data = _json_response(_request(cfg, "GET", f"{api_base}/api/emails/{mailbox['email_id']}", headers={"X-API-Key": key}), provider, "messages")
        items = _list_payload(data.get("messages") if isinstance(data, dict) else data)
        latest = _latest(items)
        if latest and (latest.get("id") or latest.get("message_id")):
            mid = latest.get("id") or latest.get("message_id")
            detail = _json_response(_request(cfg, "GET", f"{api_base}/api/emails/{mailbox['email_id']}/{mid}", headers={"X-API-Key": key}), provider, "message_detail")
            return [detail.get("message", detail) if isinstance(detail, dict) else latest]
        return items
    if provider == "inbucket":
        api_base = mailbox["api_base"].rstrip("/")
        box = mailbox["mailbox_name"]
        data = _json_response(_request(cfg, "GET", f"{api_base}/api/v1/mailbox/{box}"), provider, "messages")
        items = _list_payload(data)
        result = []
        for item in items[:10]:
            mid = item.get("id")
            if not mid:
                continue
            detail = _json_response(_request(cfg, "GET", f"{api_base}/api/v1/mailbox/{box}/{mid}"), provider, "message_detail")
            if isinstance(detail, dict):
                body = detail.get("body") if isinstance(detail.get("body"), dict) else {}
                detail["text"] = body.get("text", detail.get("text", ""))
                detail["html"] = body.get("html", detail.get("html", ""))
                result.append(detail)
        return result
    if provider == "ddg_mail":
        data = _json_response(_request(cfg, "GET", f"{mailbox['cf_base']}{mailbox['messages_path']}", headers={"Authorization": f"Bearer {mailbox['token']}"}, params={"limit": 30, "offset": 0}), provider, "messages")
        return _list_payload(data)
    return []


def get_oai_code(
    dev_token: str,
    email: str,
    cfg: dict[str, Any],
    extract_code: Callable[[str, str], str | None],
    timeout: float = 180,
    poll_interval: float = 3,
    log_callback: Callable[[str], None] | None = None,
    cancel_callback: Callable[[], bool] | None = None,
) -> str:
    mailbox = _decode_token(dev_token)
    if email:
        mailbox["address"] = email
    deadline = time.time() + float(timeout or 180)
    seen: set[str] = set()
    last_error = ""
    while time.time() < deadline:
        if cancel_callback and cancel_callback():
            raise RuntimeError("cancelled")
        try:
            messages = _fetch_messages(cfg, mailbox)
        except Exception as exc:
            last_error = str(exc)
            if log_callback:
                log_callback(f"[Debug] {mailbox.get('provider')} fetch failed: {last_error}")
            time.sleep(max(0.2, float(poll_interval or 3)))
            continue
        if log_callback:
            log_callback(f"[Debug] {mailbox.get('provider')} messages: {len(messages)}")
        normalized = [_normalize_message(mailbox.get("provider", ""), mailbox, item) for item in messages if isinstance(item, dict)]
        normalized.sort(key=lambda x: x.get("received", 0), reverse=True)
        for msg in normalized:
            ref = msg.get("message_id") or f"{msg.get('subject')}|{msg.get('received')}"
            if ref in seen:
                continue
            seen.add(ref)
            subject = msg.get("subject", "")
            body = "\n".join([msg.get("text_content", ""), _strip_html(msg.get("html_content", ""))])
            code = extract_code(body, subject)
            if code:
                return code
        time.sleep(max(0.2, float(poll_interval or 3)))
    raise RuntimeError(f"{mailbox.get('provider')}_code_timeout: {last_error}")
