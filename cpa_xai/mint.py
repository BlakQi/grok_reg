"""High-level: mint CPA xai-*.json for one free registered account."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Callable

from .browser_confirm import mint_with_browser
from .probe import probe_mini_response, probe_models
from .proxyutil import proxy_log_label, resolve_proxy, set_runtime_proxy
from .schema import DEFAULT_BASE_URL, build_cpa_xai_auth, jwt_payload
from .writer import write_cpa_xai_auth

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    return None


def mint_and_export(
    *,
    email: str,
    password: str,
    auth_dir: str | Path,
    page: Any | None = None,
    proxy: str | None = None,
    headless: bool = False,
    base_url: str = DEFAULT_BASE_URL,
    probe: bool = True,
    probe_chat: bool = False,
    browser_timeout_sec: float = 240.0,
    force_standalone: bool = True,
    cookies: Any | None = None,
    reuse_browser: bool = True,
    recycle_every: int = 15,
    log: LogFn | None = None,
    cancel: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    """Full pipeline: device-auth → write CPA file → optional probe.

    Returns dict with keys: ok, path, email, probe, error?
    """
    log = log or _noop
    email = (email or "").strip()
    if not email or not password:
        return {"ok": False, "email": email, "error": "missing email/password"}

    # Config/explicit proxy wins over shell https_proxy (common 7890 trap).
    # Thread-local pin — safe under concurrent mint workers.
    resolved = resolve_proxy(proxy)
    set_runtime_proxy(resolved or None)
    log(f"mint start: {email} proxy={proxy_log_label(resolved) or '(none)'}")
    try:
        tokens = mint_with_browser(
            email=email,
            password=password,
            page=None if force_standalone else page,
            proxy=resolved or None,
            headless=headless,
            browser_timeout_sec=browser_timeout_sec,
            force_standalone=force_standalone,
            cookies=cookies,
            reuse_browser=reuse_browser,
            recycle_every=recycle_every,
            poll_log=log,
            cancel=cancel,
        )
    except Exception as e:  # noqa: BLE001
        log(f"mint failed: {e}")
        return {"ok": False, "email": email, "error": str(e)}

    payload = build_cpa_xai_auth(
        email=email,
        access_token=tokens["access_token"],
        refresh_token=tokens["refresh_token"],
        id_token=tokens.get("id_token"),
        expires_in=tokens.get("expires_in"),
        base_url=base_url,
    )
    try:
        access_payload = jwt_payload(tokens["access_token"])
        result_bits = {
            "referrer": access_payload.get("referrer"),
            "client_id": access_payload.get("client_id"),
            "scope": access_payload.get("scope"),
            "aud": access_payload.get("aud"),
            "principal_type": access_payload.get("principal_type"),
        }
        log(f"access token payload: {json.dumps(result_bits, ensure_ascii=False)}")
    except Exception as e:  # noqa: BLE001
        log(f"access token payload decode failed: {e}")
    path = write_cpa_xai_auth(auth_dir, payload)
    log(f"wrote {path}")

    result: dict[str, Any] = {
        "ok": True,
        "email": email,
        "path": str(path),
        "user_code": tokens.get("user_code"),
        "base_url": base_url,
        "proxy": proxy_log_label(resolved),
    }

    if probe:
        pr = probe_models(tokens["access_token"], base_url=base_url, proxy=resolved or None)
        result["probe_models"] = pr
        log(f"probe models: ok={pr.get('ok')} has_grok_45={pr.get('has_grok_45')} ids={pr.get('model_ids')}")
        if not pr.get("has_grok_45"):
            result["ok"] = False
            result["error"] = "token ok but grok-4.5 not listed"
        if probe_chat and pr.get("has_grok_45"):
            ch = probe_mini_response(
                tokens["access_token"], base_url=base_url, proxy=resolved or None
            )
            result["probe_chat"] = ch
            log(f"probe chat: ok={ch.get('ok')} model={ch.get('model')} text={ch.get('text')!r}")
            if not ch.get("ok"):
                result["ok"] = False
                result["error"] = f"chat probe failed: {ch.get('error') or ch.get('status')}"
        if not result.get("ok"):
            try:
                failed_dir = Path(auth_dir) / "_failed"
                failed_dir.mkdir(parents=True, exist_ok=True)
                failed_path = failed_dir / Path(path).name
                shutil.copy2(path, failed_path)
                log(f"kept failed CPA debug copy: {failed_path}")
            except Exception as e:  # noqa: BLE001
                log(f"keep failed CPA debug copy failed: {e}")
            try:
                Path(path).unlink(missing_ok=True)
                log(f"removed invalid CPA file after failed probe: {path}")
            except Exception as e:  # noqa: BLE001
                log(f"remove invalid CPA file failed: {e}")
    return result
