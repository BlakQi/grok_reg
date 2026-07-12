"""xAI OAuth authorization-code + PKCE flow for Grok Build."""

from __future__ import annotations

import base64
import hashlib
import http.server
import secrets
import threading
import urllib.parse
from dataclasses import dataclass
from typing import Any

from .oauth_device import CLIENT_ID, SCOPE, TOKEN_URL, TokenResult, _post_form

AUTHORIZATION_URL = "https://auth.x.ai/oauth2/authorize"
DEFAULT_REDIRECT_HOST = "127.0.0.1"
DEFAULT_REDIRECT_PORT = 56121
REFERRER = "grok-build"
PLAN = "generic"


class OAuthPKCEError(RuntimeError):
    pass


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _code_verifier() -> str:
    return _b64url(secrets.token_bytes(48))


def _code_challenge(verifier: str) -> str:
    return _b64url(hashlib.sha256(verifier.encode("ascii")).digest())


class _CallbackServer(http.server.ThreadingHTTPServer):
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], handler_cls: type[http.server.BaseHTTPRequestHandler]):
        super().__init__(server_address, handler_cls)
        self.expected_state = ""
        self.code = ""
        self.error = ""
        self.done = threading.Event()


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    server: _CallbackServer

    def log_message(self, *_: Any) -> None:
        return None

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        state = (qs.get("state") or [""])[0]
        code = (qs.get("code") or [""])[0]
        err = (qs.get("error") or [""])[0]
        err_desc = (qs.get("error_description") or [""])[0]

        if state != self.server.expected_state:
            self.server.error = "oauth_state_mismatch"
        elif err:
            self.server.error = f"{err}: {err_desc}".strip(": ")
        elif not code:
            self.server.error = "oauth_callback_missing_code"
        else:
            self.server.code = code

        self.server.done.set()
        body = (
            "<html><body><h3>OAuth complete</h3>"
            "<p>You can close this tab and return to Grok Register.</p>"
            "</body></html>"
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@dataclass
class PKCESession:
    authorization_url: str
    redirect_uri: str
    code_verifier: str
    state: str
    server: _CallbackServer
    thread: threading.Thread

    def wait_code(self, timeout: float) -> str:
        if not self.server.done.wait(timeout=max(timeout, 1.0)):
            raise OAuthPKCEError("oauth_callback_timeout")
        if self.server.error:
            raise OAuthPKCEError(self.server.error)
        if not self.server.code:
            raise OAuthPKCEError("oauth_callback_missing_code")
        return self.server.code

    def close(self) -> None:
        try:
            self.server.shutdown()
        except Exception:
            pass
        try:
            self.server.server_close()
        except Exception:
            pass


def create_pkce_session(
    *,
    client_id: str = CLIENT_ID,
    scope: str = SCOPE,
    referrer: str = REFERRER,
    plan: str = PLAN,
    host: str = DEFAULT_REDIRECT_HOST,
    port: int = DEFAULT_REDIRECT_PORT,
) -> PKCESession:
    verifier = _code_verifier()
    challenge = _code_challenge(verifier)
    state = secrets.token_urlsafe(24)

    last_err: BaseException | None = None
    server: _CallbackServer | None = None
    for cand_port in [port, *range(port + 1, port + 30)]:
        try:
            server = _CallbackServer((host, cand_port), _CallbackHandler)
            break
        except OSError as exc:
            last_err = exc
    if server is None:
        raise OAuthPKCEError(f"cannot_bind_oauth_callback: {last_err}")

    server.expected_state = state
    redirect_uri = f"http://{host}:{server.server_address[1]}/callback"
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "plan": plan,
        "referrer": referrer,
    }
    authorization_url = f"{AUTHORIZATION_URL}?{urllib.parse.urlencode(params)}"
    thread = threading.Thread(target=server.serve_forever, name="xai-oauth-callback", daemon=True)
    thread.start()
    return PKCESession(
        authorization_url=authorization_url,
        redirect_uri=redirect_uri,
        code_verifier=verifier,
        state=state,
        server=server,
        thread=thread,
    )


def exchange_authorization_code(
    session: PKCESession,
    code: str,
    *,
    client_id: str = CLIENT_ID,
    timeout: float = 30.0,
    proxy: str | None = None,
) -> TokenResult:
    form = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": client_id,
        "redirect_uri": session.redirect_uri,
        "code_verifier": session.code_verifier,
    }
    status, body = _post_form(TOKEN_URL, form, timeout=timeout, proxy=proxy, retries=2)
    if status != 200 or not isinstance(body, dict) or not body.get("access_token"):
        raise OAuthPKCEError(f"authorization_code exchange failed HTTP {status}: {body!r}")
    access = str(body["access_token"]).strip()
    refresh = str(body.get("refresh_token") or "").strip()
    if not refresh:
        raise OAuthPKCEError("authorization_code response missing refresh_token")
    return TokenResult(
        access_token=access,
        refresh_token=refresh,
        id_token=(str(body["id_token"]).strip() if body.get("id_token") else None),
        token_type=str(body.get("token_type") or "Bearer"),
        expires_in=int(body.get("expires_in") or 21600),
        raw=body,
    )
