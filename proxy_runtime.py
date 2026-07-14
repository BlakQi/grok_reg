"""Turn proxy node URLs into a proxy endpoint understood by HTTP clients.

The registrar natively consumes unauthenticated HTTP/SOCKS proxy URLs. VLESS
nodes and authenticated forward proxies are adapted through sing-box on a
loopback-only mixed (HTTP + SOCKS) port. This also makes authenticated HTTP
proxies work in Chromium, which cannot put userinfo in ``--proxy-server``.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
_LOCK = threading.Lock()
_POOL_LOCK = threading.Lock()
_POOL_INDEX: dict[tuple[str, ...], int] = {}
_POOL_THREAD = threading.local()


class ProxyRuntimeError(RuntimeError):
    """Raised when a configured proxy node cannot be made available."""


@dataclass
class _SingBoxRuntime:
    process: subprocess.Popen[Any]
    endpoint: str
    config_path: Path | None
    log_path: Path
    log_file: Any
    owners: set[int] = field(default_factory=set)
    persistent: bool = False


_RUNTIMES: dict[str, _SingBoxRuntime] = {}


def _first(query: dict[str, list[str]], name: str, default: str = "") -> str:
    values = query.get(name)
    return values[0] if values else default


def _as_bool(value: str) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _build_sing_box_config(vless_url: str, local_port: int) -> dict[str, Any]:
    """Convert a standard VLESS share URL to a sing-box configuration."""
    parsed = urlparse((vless_url or "").strip())
    if parsed.scheme.lower() != "vless":
        raise ProxyRuntimeError("proxy URL is not a VLESS node")

    uuid = unquote(parsed.username or "").strip()
    server = parsed.hostname or ""
    try:
        server_port = parsed.port
    except ValueError as exc:
        raise ProxyRuntimeError("VLESS node has an invalid server port") from exc
    if not uuid or not server or not server_port:
        raise ProxyRuntimeError("VLESS node must contain UUID, server and port")

    query = parse_qs(parsed.query, keep_blank_values=True)
    outbound: dict[str, Any] = {
        "type": "vless",
        "tag": "vless-out",
        "server": server,
        "server_port": server_port,
        "uuid": uuid,
    }

    flow = _first(query, "flow")
    if flow:
        outbound["flow"] = flow

    security = _first(query, "security", "none").lower()
    if security in {"tls", "reality"}:
        tls: dict[str, Any] = {
            "enabled": True,
            "server_name": _first(query, "sni", server),
        }
        fingerprint = _first(query, "fp")
        if fingerprint:
            tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        if _as_bool(_first(query, "allowInsecure")):
            tls["insecure"] = True
        alpn = [item for item in _first(query, "alpn").split(",") if item]
        if alpn:
            tls["alpn"] = alpn
        outbound["tls"] = tls

    transport_type = _first(query, "type", "tcp").lower()
    if transport_type == "ws":
        transport: dict[str, Any] = {
            "type": "ws",
            "path": _first(query, "path", "/") or "/",
        }
        ws_host = _first(query, "host")
        if ws_host:
            transport["headers"] = {"Host": ws_host}
        outbound["transport"] = transport
    elif transport_type not in {"", "tcp", "none"}:
        raise ProxyRuntimeError(f"unsupported VLESS transport: {transport_type}")

    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": local_port,
            }
        ],
        "outbounds": [outbound],
        "route": {"final": "vless-out"},
    }


def _build_forward_proxy_config(proxy_url: str, local_port: int) -> dict[str, Any]:
    """Wrap an authenticated HTTP/SOCKS proxy behind a local mixed endpoint."""
    parsed = urlparse((proxy_url or "").strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https", "socks", "socks5", "socks5h"}:
        raise ProxyRuntimeError(f"unsupported forward proxy scheme: {scheme or '(empty)'}")
    server = parsed.hostname or ""
    try:
        server_port = parsed.port
    except ValueError as exc:
        raise ProxyRuntimeError("forward proxy has an invalid server port") from exc
    if not server or not server_port:
        raise ProxyRuntimeError("forward proxy must contain server and port")

    outbound_type = "http" if scheme in {"http", "https"} else "socks"
    outbound: dict[str, Any] = {
        "type": outbound_type,
        "tag": "proxy-out",
        "server": server,
        "server_port": server_port,
    }
    if parsed.username is not None:
        outbound["username"] = unquote(parsed.username)
    if parsed.password is not None:
        outbound["password"] = unquote(parsed.password)
    if scheme == "https":
        outbound["tls"] = {"enabled": True, "server_name": server}

    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [
            {
                "type": "mixed",
                "tag": "mixed-in",
                "listen": "127.0.0.1",
                "listen_port": local_port,
            }
        ],
        "outbounds": [outbound],
        "route": {"final": "proxy-out"},
    }


def _find_free_port(preferred: int | None = None) -> int:
    if preferred:
        if not 1 <= int(preferred) <= 65535:
            raise ProxyRuntimeError("vless_local_port must be between 1 and 65535")
        return int(preferred)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_sing_box(explicit: str | None = None) -> Path:
    candidates: list[Path] = []
    for raw in (explicit, os.environ.get("SING_BOX_PATH")):
        if raw:
            path = Path(raw).expanduser()
            if not path.is_absolute():
                path = BASE_DIR / path
            candidates.append(path)

    found = shutil.which("sing-box") or shutil.which("sing-box.exe")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        [
            BASE_DIR / ".runtime" / "sing-box.exe",
            BASE_DIR / ".runtime" / "sing-box",
        ]
    )
    runtime_dir = BASE_DIR / ".runtime"
    if runtime_dir.is_dir():
        candidates.extend(runtime_dir.glob("**/sing-box.exe"))
        candidates.extend(runtime_dir.glob("**/sing-box"))

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise ProxyRuntimeError(
        "this proxy requires sing-box. Put sing-box.exe in .runtime/ "
        "or set sing_box_path/SING_BOX_PATH."
    )


def _port_ready(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _read_log(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return text[-1200:]
    except OSError:
        return ""


def _stop_runtime(runtime: _SingBoxRuntime) -> None:
    try:
        if runtime.process.poll() is None:
            runtime.process.terminate()
            try:
                runtime.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                runtime.process.kill()
    finally:
        try:
            runtime.log_file.close()
        except Exception:
            pass
        for path in (runtime.config_path, runtime.log_path):
            if path:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass


def _stop_all() -> None:
    with _LOCK:
        runtimes = list(_RUNTIMES.values())
        _RUNTIMES.clear()
    for runtime in runtimes:
        _stop_runtime(runtime)


atexit.register(_stop_all)


def proxy_candidates(config: dict[str, Any]) -> list[str]:
    """Return configured proxy exits, with the legacy single value as fallback."""
    raw_pool = config.get("proxies")
    values: list[str] = []
    if isinstance(raw_pool, (list, tuple)):
        values = [str(item or "").strip() for item in raw_pool]
    elif isinstance(raw_pool, str):
        # Newline-separated strings are convenient for environment-generated JSON.
        values = [item.strip() for item in raw_pool.splitlines()]
    values = [item for item in values if item]

    proxy_file = str(config.get("proxy_file") or "").strip()
    if proxy_file:
        path = Path(proxy_file).expanduser()
        if not path.is_absolute():
            path = BASE_DIR / path
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except OSError as exc:
            raise ProxyRuntimeError(f"cannot read proxy_file: {path}") from exc
        values.extend(
            line.strip()
            for line in lines
            if line.strip() and not line.lstrip().startswith(("#", ";", "//"))
        )

    single = str(config.get("proxy") or "").strip()
    if not values and single:
        values = [single]
    return list(dict.fromkeys(values))


def proxy_pool_size(config: dict[str, Any]) -> int:
    return len(proxy_candidates(config))


def clear_thread_proxy_selection() -> None:
    selection = getattr(_POOL_THREAD, "selection", None)
    _POOL_THREAD.selection = None
    if selection:
        _release_runtime_owner(str(selection.get("raw") or ""), threading.get_ident())


def selected_config_proxy_raw(config: dict[str, Any]) -> str:
    candidates = proxy_candidates(config)
    if not candidates:
        return ""
    selection = getattr(_POOL_THREAD, "selection", None)
    key = tuple(candidates)
    if selection and selection.get("key") == key:
        return str(selection.get("raw") or "")
    return ""


def resolve_config_proxy(config: dict[str, Any], *, rotate: bool = False) -> str:
    """Resolve one stable proxy exit for the current registration thread.

    Selection is round-robin across threads/accounts.  Without ``rotate``, all
    calls made by one registration flow reuse the same browser/HTTP endpoint.
    """
    candidates = proxy_candidates(config)
    if not candidates:
        clear_thread_proxy_selection()
        return ""

    key = tuple(candidates)
    selection = getattr(_POOL_THREAD, "selection", None)
    if not rotate and selection and selection.get("key") == key:
        return str(selection.get("endpoint") or "")

    with _POOL_LOCK:
        index = _POOL_INDEX.get(key, 0)
        _POOL_INDEX[key] = index + 1
    raw = candidates[index % len(candidates)]
    owner = threading.get_ident()
    endpoint = resolve_proxy_url(
        raw,
        sing_box_path=str(config.get("sing_box_path") or "").strip() or None,
        # A fixed port can only host one sing-box instance. Pools therefore
        # always allocate independent loopback ports.
        local_port=(int(config.get("vless_local_port") or 0) or None) if len(candidates) == 1 else None,
        _owner=owner,
    )
    if selection and selection.get("raw") != raw:
        _release_runtime_owner(str(selection.get("raw") or ""), owner)
    _POOL_THREAD.selection = {"key": key, "raw": raw, "endpoint": endpoint}
    return endpoint


def _release_runtime_owner(proxy: str, owner: int) -> None:
    if not proxy:
        return
    victim: _SingBoxRuntime | None = None
    with _LOCK:
        runtime = _RUNTIMES.get(proxy)
        if runtime:
            runtime.owners.discard(owner)
            if not runtime.owners and not runtime.persistent:
                victim = _RUNTIMES.pop(proxy, None)
    if victim:
        _stop_runtime(victim)


def resolve_proxy_url(
    proxy: str | None,
    *,
    sing_box_path: str | None = None,
    local_port: int | None = None,
    startup_timeout: float = 12.0,
    _owner: int | None = None,
) -> str:
    """Return a directly usable HTTP proxy URL for ``proxy``.

    Unauthenticated HTTP/SOCKS URLs pass through unchanged. VLESS URLs and
    authenticated forward proxies start (or reuse) a loopback sing-box
    process. Startup failures are fatal so registration never silently leaks
    to a direct connection.
    """
    value = str(proxy or "").strip()
    parsed = urlparse(value)
    scheme = parsed.scheme.lower()
    needs_adapter = scheme == "vless" or (
        scheme in {"http", "https", "socks", "socks5", "socks5h"}
        and parsed.username is not None
    )
    if not value or not needs_adapter:
        return value

    with _LOCK:
        existing = _RUNTIMES.get(value)
        if existing and existing.process.poll() is None:
            if _owner is None:
                existing.persistent = True
            else:
                existing.owners.add(_owner)
            return existing.endpoint
        if existing:
            _stop_runtime(existing)
            _RUNTIMES.pop(value, None)

        executable = _find_sing_box(sing_box_path)
        port = _find_free_port(local_port)
        config = (
            _build_sing_box_config(value, port)
            if scheme == "vless"
            else _build_forward_proxy_config(value, port)
        )

        config_fd, config_name = tempfile.mkstemp(prefix="grok-proxy-", suffix=".json")
        config_path = Path(config_name)
        with os.fdopen(config_fd, "w", encoding="utf-8") as handle:
            json.dump(config, handle, ensure_ascii=False, separators=(",", ":"))

        log_fd, log_name = tempfile.mkstemp(prefix="grok-proxy-", suffix=".log")
        log_file = os.fdopen(log_fd, "w", encoding="utf-8")
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        try:
            process = subprocess.Popen(
                [str(executable), "run", "-c", str(config_path)],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception:
            log_file.close()
            config_path.unlink(missing_ok=True)
            Path(log_name).unlink(missing_ok=True)
            raise

        runtime = _SingBoxRuntime(
            process=process,
            endpoint=f"http://127.0.0.1:{port}",
            config_path=config_path,
            log_path=Path(log_name),
            log_file=log_file,
            owners={_owner} if _owner is not None else set(),
            persistent=_owner is None,
        )
        deadline = time.monotonic() + max(1.0, startup_timeout)
        while time.monotonic() < deadline:
            if process.poll() is not None:
                log_file.flush()
                detail = _read_log(runtime.log_path)
                _stop_runtime(runtime)
                raise ProxyRuntimeError(f"sing-box exited during startup: {detail or 'no log'}")
            if _port_ready(port):
                # sing-box has loaded the node; remove the credential-bearing file.
                config_path.unlink(missing_ok=True)
                runtime.config_path = None
                _RUNTIMES[value] = runtime
                return runtime.endpoint
            time.sleep(0.1)

        _stop_runtime(runtime)
        raise ProxyRuntimeError("timed out waiting for the local proxy adapter")
