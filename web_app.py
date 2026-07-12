"""Web UI for grok_reg - Flask backend with SSE log streaming."""
from __future__ import annotations
import base64, io, json, os, queue, sys, threading, time, traceback, pathlib
from pathlib import Path
from flask import Flask, Response, jsonify, render_template, request, send_file

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "config.json"
CONFIG_EXAMPLE = BASE_DIR / "config.example.json"
ACCOUNTS_FILE = BASE_DIR / "accounts_cli.txt"
OUTLOOK_POOL_FILE = BASE_DIR / "outlook_mailboxes.txt"
OUTLOOK_STATE_FILE = BASE_DIR / "outlook_mail_state.json"
CPA_DIR = BASE_DIR / "cpa_auths"
sys.path.insert(0, str(BASE_DIR))

app = Flask(__name__, template_folder="templates", static_folder="static")

# --------------- SSE log bus ---------------
_log_listeners: list[queue.Queue] = []
_log_lock = threading.Lock()
_log_history: list[dict] = []
MAX_HISTORY = 500


def _u(value: str) -> str:
    return value.encode("ascii").decode("unicode_escape")


_RUNTIME_LOG_REPLACEMENTS = {
    _u("Cloudflare \\u673d\\u90bb\\u6570\\u91cf"): _u("Cloudflare \\u672c\\u8f6e\\u90ae\\u4ef6\\u6570\\u91cf"),
    _u("CloudMail \\u673d\\u90bb\\u6570\\u91cf"): _u("CloudMail \\u672c\\u8f6e\\u90ae\\u4ef6\\u6570\\u91cf"),
    _u("\\u5f53\\u524dtoken\\u95c0\\u57ae\\u5bb3"): _u("\\u5f53\\u524dtoken\\u957f\\u5ea6"),
    _u("token\\u95c0\\u57ae\\u5bb3"): _u("token\\u957f\\u5ea6"),
    _u("\\u6722\\u7ec8\\u9875\\u72b6?"): _u("\\u6700\\u7ec8\\u9875\\u72b6\\u6001:"),
    _u("\\u5bb8\\u63d2\\u5553\\u934f?grok2api \\u671c?"): _u("\\u5df2\\u5199\\u5165 grok2api \\u672c\\u5730\\u6c60:"),
    _u("\\u5bb8\\u63d2\\u5553\\u934f?grok2api \\u6769\\u6ec5\\u59f9?"): _u("\\u5df2\\u5199\\u5165 grok2api \\u8fdc\\u7a0b\\u6c60:"),
    _u("grok2api \\u59f9\\u72b2\\u5553\\u934f\\u30e5\\u51e1\\u5bee\\u509b\\u93bb\\u612a\\u6c26"): _u("grok2api \\u6c60\\u5199\\u5165\\u5df2\\u5f02\\u6b65\\u63d0\\u4ea4"),
    _u("\\u90bb"): _u("\\u90ae\\u4ef6"),
}
_RUNTIME_LOG_REPLACEMENTS["[Debug] " + ("?" * 4) + ":"] = _u("[Debug] \\u6536\\u5230\\u90ae\\u4ef6:")
_RUNTIME_LOG_REPLACEMENTS["[*] " + ("?" * 10) + ":"] = _u("[*] \\u4ece\\u90ae\\u4ef6\\u4e2d\\u63d0\\u53d6\\u5230\\u9a8c\\u8bc1\\u7801:")


def _fix_mojibake(value):
    text = str(value)
    for bad, good in _RUNTIME_LOG_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text
def _broadcast(entry: dict):
    if isinstance(entry, dict) and "msg" in entry:
        entry = dict(entry)
        entry["msg"] = _fix_mojibake(entry.get("msg", ""))
    with _log_lock:
        _log_history.append(entry)
        if len(_log_history) > MAX_HISTORY:
            _log_history.pop(0)
        dead = []
        for q in _log_listeners:
            try: q.put_nowait(entry)
            except Exception: dead.append(q)
        for q in dead:
            _log_listeners.remove(q)

def _subscribe():
    q = queue.Queue(maxsize=200)
    with _log_lock:
        _log_listeners.append(q)
    return q

def _unsubscribe(q):
    with _log_lock:
        try: _log_listeners.remove(q)
        except ValueError: pass

# --------------- registration state ---------------
_run_lock = threading.Lock()
_running = False
_stats = dict(reg_success=0, reg_fail=0, mint_success=0, mint_fail=0, mint_skip=0)
_cancel_event = threading.Event()
_accounts_lock = threading.Lock()

# CPA mint queue lock
_mint_lock = threading.Lock()
_minting_emails = set()

def _is_running():
    with _run_lock:
        return _running

def _strip_comments(obj):
    return {k: v for k, v in obj.items() if not k.startswith("//") and not k.startswith("#")}

def load_config():
    path = CONFIG_FILE if CONFIG_FILE.exists() else CONFIG_EXAMPLE
    # 兼容编码问题：优先 utf-8，失败则回退至 gb18030
    try:
        with open(path, encoding="utf-8-sig") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(path, encoding="gb18030") as f:
            content = f.read()
    raw = json.loads(content)
    return _strip_comments(raw)

def save_config(data):
    existing = {}
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8-sig") as f:
                content = f.read()
        except UnicodeDecodeError:
            with open(CONFIG_FILE, encoding="gb18030") as f:
                content = f.read()
        existing = json.loads(content)

    for k, v in data.items():
        existing[k] = v
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(existing, f, ensure_ascii=False, indent=2)

def cpa_auth_dir():
    try:
        raw = str(load_config().get("cpa_auth_dir") or "").strip()
    except Exception:
        raw = ""
    if not raw:
        return CPA_DIR
    path = Path(raw).expanduser()
    if path.is_absolute():
        return path
    return (BASE_DIR / path).resolve()

def cpa_path_for_email(email: str):
    try:
        from cpa_xai.schema import credential_file_name
        filename = credential_file_name(email=email)
    except Exception:
        filename = f"xai-{email}.json"
    return cpa_auth_dir() / filename


def _cpa_access_token_has_build_referrer(cpa_data: dict) -> bool:
    try:
        token = str(cpa_data.get("access_token") or "")
        parts = token.split(".")
        if len(parts) < 2:
            return False
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
        return str(data.get("referrer") or "").strip() == "grok-build"
    except Exception:
        return False

def read_accounts():
    results = []
    if not ACCOUNTS_FILE.exists():
        return results
    for i, line in enumerate(ACCOUNTS_FILE.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("----")
        email = parts[0] if len(parts) > 0 else ""
        password = parts[1] if len(parts) > 1 else ""
        sso = parts[2] if len(parts) > 2 else ""

        cpa_path = cpa_path_for_email(email)
        has_cpa = cpa_path.exists()
        cpa_expiry = ""
        cpa_raw = ""
        if has_cpa:
            try:
                cpa_data = json.loads(cpa_path.read_text(encoding="utf-8"))
                if _cpa_access_token_has_build_referrer(cpa_data):
                    cpa_raw = json.dumps(cpa_data)
                    exp = cpa_data.get("expires_at") or cpa_data.get("expires")
                    if exp:
                        cpa_expiry = str(exp)
                else:
                    has_cpa = False
                    cpa_expiry = "旧 CPA 缺少 referrer=grok-build，需要重新获取"
            except Exception:
                has_cpa = False
                cpa_expiry = "CPA 文件损坏，需要重新获取"

        is_minting = False
        with _mint_lock:
            is_minting = email in _minting_emails

        results.append(dict(
            id=i, email=email,
            password=password,
            sso=sso,
            has_cpa=has_cpa,
            cpa_expiry=cpa_expiry,
            cpa_raw=cpa_raw,
            is_minting=is_minting
        ))
    return results

def import_accounts(raw_text: str):
    existing = {a["email"].strip().lower() for a in read_accounts() if a.get("email")}
    seen = set()
    rows = []
    invalid = []
    skipped = []

    for line_no, raw_line in enumerate((raw_text or "").splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        parts = line.split("----", 2)
        if len(parts) != 3:
            invalid.append(dict(line=line_no, reason="格式应为 邮箱----密码----SSO"))
            continue
        email, password, sso = [p.strip() for p in parts]
        email_key = email.lower()
        if not email or "@" not in email:
            invalid.append(dict(line=line_no, reason="邮箱为空或格式不正确"))
            continue
        if not password:
            invalid.append(dict(line=line_no, reason="密码为空"))
            continue
        if not sso:
            invalid.append(dict(line=line_no, reason="SSO 为空"))
            continue
        if email_key in existing or email_key in seen:
            skipped.append(dict(line=line_no, email=email, reason="邮箱已存在"))
            continue
        seen.add(email_key)
        rows.append(f"{email}----{password}----{sso}")

    if rows:
        ACCOUNTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with _accounts_lock:
            needs_prefix_newline = ACCOUNTS_FILE.exists() and ACCOUNTS_FILE.stat().st_size > 0
            if needs_prefix_newline:
                with open(ACCOUNTS_FILE, "rb") as f:
                    f.seek(-1, os.SEEK_END)
                    needs_prefix_newline = f.read(1) not in (b"\n", b"\r")
            with open(ACCOUNTS_FILE, "a", encoding="utf-8", newline="\n") as f:
                if needs_prefix_newline:
                    f.write("\n")
                f.write("\n".join(rows))
                f.write("\n")

    return dict(
        imported=len(rows),
        skipped_duplicates=len(skipped),
        invalid_count=len(invalid),
        skipped=skipped,
        invalid=invalid,
    )

def _outlook_paths(cfg=None):
    cfg = cfg or load_config()

    def resolve(value, default_path):
        raw = str(value or "").strip()
        if not raw:
            return default_path
        path = Path(raw)
        if path.is_absolute():
            return path
        return BASE_DIR / path

    return (
        resolve(cfg.get("outlook_pool_file"), OUTLOOK_POOL_FILE),
        resolve(cfg.get("outlook_state_file"), OUTLOOK_STATE_FILE),
    )

def _outlook_cfg(cfg=None):
    cfg = cfg or load_config()
    return {
        "outlook_alias_count": int(cfg.get("outlook_alias_count") or 0),
        "outlook_alias_include_original": bool(cfg.get("outlook_alias_include_original", True)),
        "outlook_alias_prefix": str(cfg.get("outlook_alias_prefix") or "grok"),
        "outlook_mode": str(cfg.get("outlook_mode") or "auto"),
        "outlook_imap_host": str(cfg.get("outlook_imap_host") or "outlook.office365.com"),
        "outlook_message_limit": int(cfg.get("outlook_message_limit") or 10),
    }

def outlook_pool_summary():
    import outlook_mail

    cfg = load_config()
    pool_file, state_file = _outlook_paths(cfg)
    stats = outlook_mail.pool_stats(pool_file, state_file, _outlook_cfg(cfg))
    return dict(
        pool_file=str(pool_file),
        state_file=str(state_file),
        stats=stats,
    )

def cpa_file_count():
    try:
        import cpa_sub2api_export

        return int(cpa_sub2api_export.export_summary(cpa_auth_dir()).get("valid_cpa") or 0)
    except Exception:
        cpa_dir = cpa_auth_dir()
        if not cpa_dir.exists():
            return 0
        return len(list(cpa_dir.glob("xai-*.json")))

# --------------- CPA Mint single task thread ---------------
def _bg_mint_single(email: str, password: str, sso: str, force_overwrite: bool = False):
    def log_cb(msg):
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg=f"[CPA-Mint] [{email}] {msg}"))

    with _mint_lock:
        if email in _minting_emails:
            return
        _minting_emails.add(email)

    try:
        log_cb("开始 CPA OIDC 重新获取流程...")
        import cpa_export

        reg_cfg = load_config()
        proxy = reg_cfg.get("cpa_proxy") or reg_cfg.get("proxy") or None
        cpa_dir = str(reg_cfg.get("cpa_hotload_dir") or "").strip()

        if force_overwrite:
            old_path = cpa_path_for_email(email)
            try:
                if old_path.exists():
                    old_path.unlink()
                    log_cb(f"已删除旧 CPA 凭证文件: {old_path}")
            except Exception as ex:
                log_cb(f"删除旧 CPA 凭证文件失败: {ex}")

            try:
                if cpa_dir:
                    hotload_dir = Path(cpa_dir).expanduser()
                    if not hotload_dir.is_absolute():
                        hotload_dir = (BASE_DIR / hotload_dir).resolve()
                    hotload_path = hotload_dir / old_path.name
                    if hotload_path.exists():
                        hotload_path.unlink()
                        log_cb(f"已删除热加载目录旧 CPA 文件: {hotload_path}")
            except Exception as ex:
                log_cb(f"删除热加载目录旧 CPA 文件失败: {ex}")

        log_cb(f"正在拉起 Chromium 访问 accounts.x.ai 进行授权确认 (代理: {proxy or '直连'})...")

        r = cpa_export.export_cpa_xai_for_account(
            email=email,
            password=password,
            page=None,
            cookies=None,
            sso=sso,
            config=reg_cfg,
            log_callback=log_cb
        )

        if r.get("ok") and r.get("path"):
            log_cb(f"CPA OIDC 生成成功! 路径: {r.get('path')}")
            if cpa_dir:
                try:
                    import shutil
                    src = Path(r["path"])
                    hotload_dir = Path(cpa_dir).expanduser()
                    if not hotload_dir.is_absolute():
                        hotload_dir = (BASE_DIR / hotload_dir).resolve()
                    dst = hotload_dir / src.name
                    hotload_dir.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    os.chmod(dst, 0o600)
                    log_cb(f"已同步复制至热加载目录: {dst}")
                except Exception as ex:
                    log_cb(f"同步复制至热加载目录失败: {ex}")
        else:
            log_cb(f"CPA 补签失败: {r.get('error') or r}")
    except Exception as exc:
        log_cb(f"CPA 补签发生异常: {exc}")
        log_cb(traceback.format_exc())
    finally:
        with _mint_lock:
            _minting_emails.discard(email)
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg="__DONE__"))

# --------------- batch register run ---------------
def _run_registration(extra: int, threads: int):
    global _running, _stats
    with _run_lock:
        if _running: return
        _running = True
        _stats = dict(reg_success=0, reg_fail=0, mint_success=0, mint_fail=0, mint_skip=0)
    _cancel_event.clear()
    def log_cb(msg: str):
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg=msg))
    try:
        log_cb(f"[Web] 开始批量注册任务: 数量={extra}, 线程={threads}")
        import register_cli as cli
        import grok_register_ttk as reg
        reg.load_config()
        cfg = getattr(reg, "config", {}) or {}
        threads = max(1, min(threads, 10))
        mint_workers = cli.resolve_mint_workers(cli_value=-1, threads=threads, config=cfg, inline_mint=False)
        do_mint_inline = mint_workers == 0
        mint_qmax = cli.resolve_mint_queue_max(cfg, mint_workers)
        reg.configure_perf(fast=True, sleep_scale=0.15, skip_debug_io=True,
            cookie_snapshot=False, async_side_effects=True, browser_reuse=True, browser_recycle_every=25)
        done_count = 0
        af = str(ACCOUNTS_FILE)
        if os.path.exists(af):
            with open(af) as f:
                done_count = sum(1 for line in f if line.strip())
        target_total = done_count + extra
        try:
            reg.TabPool.init(reg.create_browser_options, log_callback=log_cb)
        except Exception as exc:
            log_cb(f"[!] 浏览器初始化失败: {exc}")
            with _run_lock:
                _running = False
            return
        task_queue = queue.Queue()
        mint_queue = queue.Queue() if not do_mint_inline else None
        if mint_queue is not None:
            mint_queue._reg_qmax = mint_qmax
        for i in range(done_count + 1, target_total + 1):
            task_queue.put(i)
        original_log = cli.log
        def patched_log(wid, msg):
            original_log(wid, msg)
            log_cb(f"[W{wid}] {msg}")
        cli.log = patched_log
        mint_threads = []
        if mint_queue is not None and mint_workers > 0:
            for i in range(1, mint_workers + 1):
                wid = f"M{i}"
                t = threading.Thread(target=cli._mint_worker, args=(wid, mint_queue, cfg), daemon=True)
                t.start()
                mint_threads.append(t)
        reg_threads = []
        for wid in range(1, threads + 1):
            t = threading.Thread(target=cli._register_worker,
                args=(wid, task_queue, target_total, af, mint_queue, False, do_mint_inline), daemon=True)
            t.start()
            reg_threads.append(t)
        for t in reg_threads:
            while t.is_alive():
                if _cancel_event.is_set():
                    log_cb("[Web] 收到取消信号，正在等待当前浏览器会话结束...")
                    break
                t.join(timeout=1.0)
            if _cancel_event.is_set():
                break
        if mint_queue is not None:
            log_cb("[Web] 正在等待 CPA Mint 队列处理完成...")
            mint_queue.join()
            for _ in mint_threads:
                mint_queue.put(cli._MINT_STOP)
            for t in mint_threads:
                t.join(timeout=120)
        try: reg.shutdown_browser()
        except Exception: pass
        with cli._stats_lock:
            _stats.update(cli._stats)
        log_cb(f"[Web] 注册已完成: 注册成功={_stats['reg_success']}, 注册失败={_stats['reg_fail']}, CPA成功={_stats['mint_success']}, CPA失败={_stats['mint_fail']}, CPA跳过={_stats['mint_skip']}")
    except Exception as exc:
        log_cb(f"[!] 任务异常: {exc}")
        log_cb(traceback.format_exc())
    finally:
        with _run_lock:
            _running = False
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg="__DONE__"))

# --------------- routes ---------------

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def api_status():
    accounts = read_accounts()
    return jsonify(dict(
        running=_is_running(),
        stats=dict(_stats),
        accounts_count=len(accounts),
        cpa_count=cpa_file_count()
    ))

@app.route("/api/accounts")
def api_accounts():
    return jsonify(read_accounts())

@app.route("/api/accounts/import", methods=["POST"])
def api_accounts_import():
    data = request.get_json(force=True) or {}
    raw_text = data.get("text") or data.get("content") or ""
    if not str(raw_text).strip():
        return jsonify(dict(ok=False, error="导入内容为空")), 400
    result = import_accounts(str(raw_text))
    _broadcast(dict(
        ts=time.strftime("%H:%M:%S"),
        msg=f"[Web] 导入账号完成: 新增={result['imported']}, 重复={result['skipped_duplicates']}, 格式错误={result['invalid_count']}"
    ))
    return jsonify(dict(ok=True, **result))

@app.route("/api/cpa/export/summary", methods=["GET"])
def api_cpa_export_summary():
    try:
        import cpa_sub2api_export

        return jsonify(dict(ok=True, **cpa_sub2api_export.export_summary(cpa_auth_dir())))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/cpa/export/sub2api", methods=["GET"])
def api_cpa_export_sub2api():
    try:
        import cpa_sub2api_export

        document, summary = cpa_sub2api_export.build_sub2api_document(cpa_auth_dir())
        if not document.get("accounts"):
            return jsonify(dict(ok=False, error="no_valid_cpa_files")), 404
        data = json.dumps(document, ensure_ascii=False, indent=2).encode("utf-8")
        _broadcast(dict(
            ts=time.strftime("%H:%M:%S"),
            msg=f"[Web] CPA exported as sub2api: count={summary['count']}, skipped={len(summary['skipped'])}"
        ))
        return send_file(
            io.BytesIO(data),
            mimetype="application/json; charset=utf-8",
            as_attachment=True,
            download_name=summary["filename"],
        )
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/cpa/export/cpa", methods=["GET"])
def api_cpa_export_cpa_zip():
    try:
        import cpa_sub2api_export

        data, summary = cpa_sub2api_export.build_cpa_zip(cpa_auth_dir())
        if summary.get("count", 0) <= 0:
            return jsonify(dict(ok=False, error="no_valid_cpa_files")), 404
        _broadcast(dict(
            ts=time.strftime("%H:%M:%S"),
            msg=f"[Web] CPA zip exported: count={summary['count']}, skipped={len(summary['skipped'])}"
        ))
        return send_file(
            io.BytesIO(data),
            mimetype="application/zip",
            as_attachment=True,
            download_name=summary["filename"],
        )
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/mailboxes/outlook", methods=["GET"])
def api_outlook_mailboxes():
    try:
        return jsonify(dict(ok=True, **outlook_pool_summary()))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/mailboxes/outlook/import", methods=["POST"])
def api_outlook_mailboxes_import():
    try:
        import outlook_mail

        data = request.get_json(force=True) or {}
        raw_text = data.get("text") or data.get("content") or ""
        if not str(raw_text).strip():
            return jsonify(dict(ok=False, error="empty_import_text")), 400
        pool_file, _state_file = _outlook_paths()
        result = outlook_mail.import_credentials(pool_file, str(raw_text))
        summary = outlook_pool_summary()
        _broadcast(dict(
            ts=time.strftime("%H:%M:%S"),
            msg=f"[Web] Outlook mailboxes imported: new={result['imported']}, updated={result['updated']}, invalid={result['invalid']}"
        ))
        return jsonify(dict(ok=True, **result, **summary))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/mailboxes/outlook/reset", methods=["POST"])
def api_outlook_mailboxes_reset():
    try:
        import outlook_mail

        data = request.get_json(force=True) or {}
        scope = str(data.get("scope") or "all")
        _pool_file, state_file = _outlook_paths()
        removed = outlook_mail.reset_state(state_file, scope)
        summary = outlook_pool_summary()
        _broadcast(dict(ts=time.strftime("%H:%M:%S"), msg=f"[Web] Outlook mailbox state reset: scope={scope}, removed={removed}"))
        return jsonify(dict(ok=True, removed=removed, **summary))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

@app.route("/api/config", methods=["GET"])
def api_config_get():
    return jsonify(load_config())

@app.route("/api/config", methods=["POST"])
def api_config_post():
    data = request.get_json(force=True)
    save_config(data)
    return jsonify(dict(ok=True))

@app.route("/api/start", methods=["POST"])
def api_start():
    if _is_running():
        return jsonify(dict(ok=False, error="当前有注册任务正在运行")), 409
    data = request.get_json(force=True) or {}
    extra = int(data.get("extra", 1))
    threads = int(data.get("threads", 1))
    t = threading.Thread(target=_run_registration, args=(extra, threads), daemon=True)
    t.start()
    return jsonify(dict(ok=True, extra=extra, threads=threads))

@app.route("/api/stop", methods=["POST"])
def api_stop():
    if not _is_running():
        return jsonify(dict(ok=False, error="注册任务没有运行")), 400
    _cancel_event.set()
    return jsonify(dict(ok=True))

# CPA probe API
@app.route("/api/cpa/probe", methods=["POST"])
def api_cpa_probe():
    data = request.get_json(force=True) or {}
    email = data.get("email")
    if not email:
        return jsonify(dict(ok=False, error="邮箱参数缺失")), 400

    cpa_path = cpa_path_for_email(email)
    if not cpa_path.exists():
        return jsonify(dict(ok=False, error="对应的 CPA OIDC 凭证文件不存在")), 404

    try:
        cpa_data = json.loads(cpa_path.read_text(encoding="utf-8"))
        access_token = cpa_data.get("access_token")
        if not access_token:
            return jsonify(dict(ok=False, error="凭证文件中无 access_token")), 400

        reg_cfg = load_config()
        proxy = reg_cfg.get("cpa_proxy") or reg_cfg.get("proxy") or None

        from cpa_xai.probe import probe_mini_response, probe_models
        t0 = time.time()
        base_url = cpa_data.get("base_url") or reg_cfg.get("cpa_base_url") or "https://cli-chat-proxy.grok.com/v1"
        res = probe_models(access_token, base_url=base_url, proxy=proxy)
        if res.get("ok") and res.get("has_grok_45"):
            res["probe_chat"] = probe_mini_response(access_token, base_url=base_url, proxy=proxy)
            if not res["probe_chat"].get("ok"):
                res["ok"] = False
                res["error"] = res["probe_chat"].get("error") or f"chat probe failed: {res['probe_chat'].get('status')}"
        res["elapsed"] = round(time.time() - t0, 2)
        return jsonify(res)
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

# Single Account CPA Mint
@app.route("/api/cpa/mint_single", methods=["POST"])
def api_cpa_mint_single():
    data = request.get_json(force=True) or {}
    email = data.get("email")
    password = data.get("password")
    sso = data.get("sso")
    force_overwrite = bool(data.get("force") or data.get("overwrite"))

    if not email or not password:
        return jsonify(dict(ok=False, error="参数不完整")), 400

    with _mint_lock:
        if email in _minting_emails:
            return jsonify(dict(ok=False, error="该账号已在补签任务中")), 409

    t = threading.Thread(target=_bg_mint_single, args=(email, password, sso, force_overwrite), daemon=True)
    t.start()
    return jsonify(dict(ok=True))

# Test email connection
@app.route("/api/test/mail", methods=["POST"])
def api_test_mail():
    try:
        import grok_register_ttk as reg
        reg.load_config()
        cfg = load_config()
        provider = (cfg.get("email_provider") or "cloudmail").strip().lower()
        provider = {
            "cloudmail_gen": "cloudmail",
            "cloudflare_temp_email": "cloudflare",
            "yyds_mail": "yyds",
            "outlook_token": "outlook",
            "done_mail": "donemail",
        }.get(provider, provider)

        t0 = time.time()
        if provider == "cloudmail":
            url = reg.get_cloudmail_url()
            admin = reg.get_cloudmail_admin_email()
            pwd = reg.get_cloudmail_password()
            if not url:
                return jsonify(dict(ok=False, error="未配置 CloudMail 地址 (cloudmail_url)")), 400
            token = reg.cloudmail_gen_public_token(url, admin, pwd)
            elapsed = round(time.time() - t0, 2)
            preview = (str(token)[:15] + "...") if token else ""
            return jsonify(dict(ok=True, provider=provider, token=preview, elapsed=elapsed))

        # 其它服务商：做基础配置完整性检查
        if provider == "outlook":
            summary = outlook_pool_summary()
            stats = summary.get("stats", {})
            if int(stats.get("expanded") or 0) <= 0:
                return jsonify(dict(ok=False, error="Outlook mailbox pool is empty")), 400
            elapsed = round(time.time() - t0, 2)
            return jsonify(dict(
                ok=True,
                provider=provider,
                token=f"pool={stats.get('base', 0)}, expanded={stats.get('expanded', 0)}, available={stats.get('available', 0)}",
                elapsed=elapsed,
                note="Outlook pool config checked"
            ))

        required = {
            "cloudflare": ["cloudflare_api_base"],
            "duckmail": ["duckmail_api_key"],
            "yyds": ["yyds_api_key"],
            "tempmail_lol": [],
            "gptmail": ["gptmail_api_base"],
            "donemail": ["donemail_api_base", "donemail_admin_key", "donemail_domains"],
            "moemail": ["moemail_api_base", "moemail_api_key", "moemail_domains"],
            "inbucket": ["inbucket_api_base", "inbucket_domains"],
            "ddg_mail": ["ddg_token", "ddg_cf_api_base", "ddg_cf_inbox_jwt"],
        }.get(provider, [])
        missing = [k for k in required if not str(cfg.get(k) or "").strip()]
        if missing:
            return jsonify(dict(ok=False, error=f"当前服务商 {provider} 缺少配置: {', '.join(missing)}")), 400
        elapsed = round(time.time() - t0, 2)
        return jsonify(dict(
            ok=True,
            provider=provider,
            token="配置项检查通过",
            elapsed=elapsed,
            note=f"已校验 {provider} 关键配置项，未实际发起收信请求"
        ))
    except Exception as e:
        return jsonify(dict(ok=False, error=str(e))), 500

# Workspace diagnostics
@app.route("/api/test/sys_check", methods=["GET"])
def api_test_sys_check():
    import sys, platform, shutil
    chrome_path = ""
    for cand in ("/usr/bin/chromium", "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe", "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe"):
        if os.path.exists(cand):
            chrome_path = cand
            break
    if not chrome_path:
        chrome_path = shutil.which("chrome") or shutil.which("chromium") or "未检测到系统Chrome，请确认安装"

    res = {
        "python_version": sys.version,
        "platform": platform.platform(),
        "chrome_path": chrome_path,
        "workspace": str(BASE_DIR)
    }
    return jsonify(res)

@app.route("/api/logs/stream")
def api_logs_stream():
    q = _subscribe()
    def generate():
        with _log_lock:
            for entry in list(_log_history[-50:]):
                yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
        try:
            while True:
                try:
                    entry = q.get(timeout=30)
                    yield "data: " + json.dumps(entry, ensure_ascii=False) + "\n\n"
                except queue.Empty:
                    yield ": keepalive\n\n"
        except GeneratorExit:
            _unsubscribe(q)
    return Response(generate(), mimetype="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    print("[*] Grok Register Web UI: http://127.0.0.1:5000")
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
