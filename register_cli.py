"""CLI wrapper for grok_register_ttk — multi-thread register + async CPA mint pipeline.

Architecture:
  Register workers (R)  →  accounts_cli + mint_queue
  Mint workers (M)      →  cpa_auths/xai-*.json + optional hotload

Browser lifecycle:
  - One Chromium per register worker, reused via TabPool.clear_session
  - Full recycle every N accounts or on error
  - Register browser released BEFORE mint (mint always standalone Chromium)
  - Peak browsers ≈ R + M (not 2×R)
"""
from __future__ import annotations

import argparse
import os
import queue
import sys
import threading
import time
import traceback
from typing import Any

# 强制走本目录的 grok_register_ttk
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import grok_register_ttk as reg  # noqa: E402


# Linux 适配: DrissionPage 默认找 'chrome', 我们装的是 chromium
# 保留原版 slim flags + proxy，再补 chromium 路径与 turnstilePatch。
_orig_create_browser_options = reg.create_browser_options


def _patched_create_browser_options():
    # Prefer original factory (proxy + CHROMIUM_SLIM_FLAGS + extension)
    try:
        opts = _orig_create_browser_options()
    except Exception:
        from DrissionPage import ChromiumOptions

        opts = ChromiumOptions()
        opts.auto_port()
        opts.set_timeouts(base=1)
        for flag in getattr(reg, "CHROMIUM_SLIM_FLAGS", ()) or ():
            try:
                opts.set_argument(flag)
            except Exception:
                pass

    try:
        opts.auto_port()
    except Exception:
        pass
    try:
        opts.set_timeouts(base=1)
    except Exception:
        pass

    for cand in (
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
    ):
        if os.path.isfile(cand):
            try:
                opts.set_browser_path(cand)
            except Exception:
                pass
            break

    ext_path = os.path.join(os.path.dirname(os.path.abspath(reg.__file__)), "turnstilePatch")
    if os.path.isdir(ext_path):
        try:
            opts.add_extension(ext_path)
        except Exception:
            pass
    return opts


reg.create_browser_options = _patched_create_browser_options


# \u2500\u2500 \u7ebf\u7a0b\u5b89\u5168\u65e5\u5fd7 \u2500\u2500

_log_queue: queue.Queue = queue.Queue()


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


def fix_mojibake(value: Any) -> str:
    text = str(value)
    for bad, good in _RUNTIME_LOG_REPLACEMENTS.items():
        text = text.replace(bad, good)
    return text
def _log_writer():
    while True:
        msg = _log_queue.get()
        if msg is None:
            break
        print(msg, flush=True)


def log(worker_id: int | str, msg: str) -> None:
    _log_queue.put(f"[{time.strftime('%H:%M:%S')}] [W{worker_id}] {fix_mojibake(msg)}")


def is_mail_stage_retryable_error(message: str) -> bool:
    lower = str(message or "").lower()
    return any(
        term in lower
        for term in (
            "验证码",
            "verification",
            "code",
            "timeout",
            "outlook_code_timeout",
            "outlooktoken",
            "token_refresh",
            "refresh_token",
            "invalid_grant",
            "aadsts",
            "login_required",
            "unauthorized",
            "mail fetch",
        )
    )


# ── 统计 ──

_stats_lock = threading.Lock()
_stats = {
    "reg_success": 0,
    "reg_fail": 0,
    "mint_success": 0,
    "mint_fail": 0,
    "mint_skip": 0,
}


def _inc(key: str, n: int = 1) -> None:
    with _stats_lock:
        _stats[key] = _stats.get(key, 0) + n


# forever 任务索引
_next_idx_lock = threading.Lock()
_next_idx = [1]

# mint 队列结束哨兵
_MINT_STOP = object()


def resolve_mint_workers(
    *,
    cli_value: int,
    threads: int,
    config: dict,
    inline_mint: bool,
) -> int:
    """Resolve mint worker count.

    Priority: --inline-mint > CLI --mint-workers (>=0) > config cpa_mint_workers > auto.
    auto (-1): min(threads, 4) when CPA export enabled, else 0.
    0: inline mint on register threads.
    """
    if inline_mint:
        return 0
    if cli_value >= 0:
        return max(0, min(int(cli_value), 10))
    cfg_v = config.get("cpa_mint_workers", -1)
    try:
        cfg_v = int(cfg_v)
    except Exception:
        cfg_v = -1
    if cfg_v >= 0:
        return max(0, min(cfg_v, 10))
    # auto
    if config.get("cpa_export_enabled", True):
        return max(1, min(int(threads), 4))
    return 0


def resolve_mint_queue_max(config: dict, mint_workers: int, cli_value: int | None = None) -> int:
    if cli_value is not None and cli_value >= 0:
        return int(cli_value)
    try:
        v = int(config.get("cpa_mint_queue_max", 0) or 0)
    except Exception:
        v = 0
    if v > 0:
        return v
    # default backpressure: 2 × mint workers (0 if no mint pool)
    return max(0, mint_workers * 2) if mint_workers > 0 else 0


class DummyStop:
    def __call__(self) -> bool:
        return False


def _ensure_browser(worker_id: int, force_recycle: bool = False):
    """Start browser if missing; optional full recycle."""
    if force_recycle:
        try:
            reg.stop_browser()
        except Exception:
            pass
    if reg.TabPool.get_browser() is None:
        reg.start_browser(log_callback=lambda m: log(worker_id, m))


def register_one(
    worker_id: int,
    idx: int,
    total: int,
    accounts_file: str,
    *,
    do_mint_inline: bool = False,
    mint_queue: queue.Queue | None = None,
    cancel_callback=None,
) -> dict | None:
    """Run one registration. Enqueue CPA mint (default) instead of blocking.

    Returns dict(email, sso, profile) or None.
    """
    email = ""
    dev_token = ""
    max_mail_retry = 3
    cancel = cancel_callback or DummyStop()
    if cancel():
        log(worker_id, f"[cancel] skip account {idx}")
        return None

    try:
        _endpoint, pool_size = reg.rotate_registration_proxy()
        if pool_size > 1 and reg.TabPool.get_browser() is not None:
            reg.stop_browser()
        log(worker_id, f"[*] 注册出口: {reg.registration_proxy_label()}")
    except Exception as exc:
        log(worker_id, f"! 代理初始化失败: {exc}")
        return None

    try:
        _ensure_browser(worker_id, force_recycle=False)
    except Exception as exc:
        log(worker_id, f"! 浏览器启动失败: {exc}")
        return None

    for mail_try in range(1, max_mail_retry + 1):
        try:
            log(worker_id, f"--- 第 {idx}/{total} 个账号, 邮箱尝试 {mail_try}/{max_mail_retry} ---")
            log(worker_id, "1. 打开注册页")
            reg.open_signup_page(log_callback=lambda m: log(worker_id, m), cancel_callback=cancel)
            log(worker_id, "2. 创建邮箱并提交")
            email, dev_token = reg.fill_email_and_submit(
                log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
            )
            log(worker_id, f"邮箱: {email}")
            log(worker_id, "3. 拉取验证码")
            code = reg.fill_code_and_submit(
                email,
                dev_token,
                log_callback=lambda m: log(worker_id, m),
                cancel_callback=cancel,
            )
            log(worker_id, f"验证码: {code}")
            break
        except reg.RegistrationCancelled:
            log(worker_id, f"[cancel] account {idx} cancelled")
            return None
        except Exception as exc:
            msg = str(exc)
            reg.mark_error(email or "", reason=msg[:120])
            if is_mail_stage_retryable_error(msg) and mail_try < max_mail_retry:
                log(worker_id, f"! 本邮箱不可用或未取到验证码，换邮箱重试: {msg}")
                try:
                    reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                except Exception:
                    pass
                reg.sleep_with_cancel(1, cancel)
                continue
            log(worker_id, f"! 邮箱阶段失败: {msg}")
            traceback.print_exc()
            _inc("reg_fail")
            try:
                reg.restart_browser(log_callback=lambda m: log(worker_id, m))
            except Exception:
                pass
            return None

    try:
        log(worker_id, "4. 填写资料")
        profile = reg.fill_profile_and_submit(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        log(worker_id, f"资料已填: {profile.get('given_name')} {profile.get('family_name')}")
        log(worker_id, "5. 等待 sso cookie")
        sso = reg.wait_for_sso_cookie(
            log_callback=lambda m: log(worker_id, m), cancel_callback=cancel
        )
        password = profile.get("password", "") or ""
        account_proxy = ""
        try:
            account_proxy = reg.selected_config_proxy_raw(getattr(reg, "config", {}) or {})
        except Exception:
            account_proxy = ""
        line = f"{email}----{password}----{sso}\n"
        with open(accounts_file, "a", encoding="utf-8") as f:
            f.write(line)
        log(worker_id, f"+ 注册成功: {email}")
        reg.mark_used(email, password)

        # Capture cookies BEFORE releasing browser (for mint cookie inject)
        page = reg._get_page()
        cookies = []
        try:
            import cpa_export as _cpa_exp

            cookies = _cpa_exp.export_cookies_from_page(page) if page is not None else []
        except Exception:
            cookies = []
        if cookies:
            log(worker_id, f"[*] 导出 cookie {len(cookies)} 条供 mint 注入")

        if page and reg.PERF_FLAGS.get("cookie_snapshot", True):
            try:
                reg.save_cookies_snapshot(page, "success", email)
            except Exception:
                pass
        try:
            reg.add_token_to_grok2api_pools(
                sso, email=email, log_callback=lambda m: log(worker_id, m)
            )
        except Exception as exc:
            log(worker_id, f"[Debug] grok2api: {exc}")

        # Release / recycle register browser BEFORE mint so peak browsers ≈ R+M
        try:
            reg.prepare_browser_for_next_account(log_callback=lambda m: log(worker_id, m))
        except Exception:
            try:
                reg.stop_browser()
            except Exception:
                pass

        job = {
            "email": email,
            "password": password,
            "sso": sso,
            "profile": profile,
            "idx": idx,
            "cookies": cookies,
            "proxy": account_proxy,
        }

        if do_mint_inline:
            _run_mint_job(f"R{worker_id}", job, getattr(reg, "config", {}) or {})
        elif mint_queue is not None:
            # backpressure: wait while queue is saturated
            qmax = int(getattr(mint_queue, "_reg_qmax", 0) or 0)
            while qmax > 0 and mint_queue.qsize() >= qmax:
                log(worker_id, f"[cpa] mint 队列背压 qsize={mint_queue.qsize()}≥{qmax}，等待...")
                time.sleep(1.0)
            mint_queue.put(job)
            log(worker_id, f"[cpa] enqueued mint for {email} (queue≈{mint_queue.qsize()})")
        else:
            log(worker_id, "[cpa] mint skipped (no queue / inline)")

        _inc("reg_success")
        return job
    except reg.RegistrationCancelled:
        log(worker_id, f"[cancel] account {idx} cancelled")
        return None
    except Exception as exc:
        log(worker_id, f"! 注册失败: {exc}")
        reg.mark_error(email or "", reason=str(exc)[:120])
        traceback.print_exc()
        _inc("reg_fail")
        try:
            reg.restart_browser(log_callback=lambda m: log(worker_id, m))
        except Exception:
            pass
        return None


def _run_mint_job(worker_id: int | str, job: dict[str, Any], config: dict) -> dict:
    """Standalone CPA mint (own Chromium). Never reuses register browser."""
    email = job.get("email") or ""
    password = job.get("password") or ""
    if not email or not password:
        _inc("mint_fail")
        return {"ok": False, "error": "missing email/password", "email": email}
    if not config.get("cpa_export_enabled", True):
        _inc("mint_skip")
        log(worker_id, f"[cpa] export disabled, skip {email}")
        return {"ok": False, "skipped": True, "email": email}
    try:
        import cpa_export
        mint_config = dict(config or {})
        if "proxy" in job:
            job_proxy = str(job.get("proxy") or "").strip()
            mint_config["proxy"] = job_proxy
            mint_config["cpa_proxy"] = job_proxy

        # page=None always — force standalone path inside export
        result = cpa_export.export_cpa_xai_for_account(
            email,
            password,
            page=None,
            cookies=job.get("cookies"),
            sso=job.get("sso") or "",
            config=mint_config,
            log_callback=lambda m: log(worker_id, m),
        )
        if result.get("ok"):
            log(worker_id, f"+ CPA auth: {result.get('path')}")
            _inc("mint_success")
        elif result.get("skipped"):
            _inc("mint_skip")
            log(worker_id, f"[cpa] skipped: {result.get('reason')}")
        else:
            _inc("mint_fail")
            log(worker_id, f"! CPA auth 未成功: {result.get('error') or result}")
        return result
    except Exception as exc:
        _inc("mint_fail")
        log(worker_id, f"! CPA export 异常: {exc}")
        traceback.print_exc()
        return {"ok": False, "error": str(exc), "email": email}


def _register_worker(
    worker_id: int,
    task_queue: queue.Queue,
    total: int,
    accounts_file: str,
    mint_queue: queue.Queue | None,
    forever: bool,
    do_mint_inline: bool,
    cancel_callback=None,
):
    while True:
        if cancel_callback and cancel_callback():
            break
        try:
            idx = task_queue.get_nowait()
        except queue.Empty:
            if not forever:
                break
            with _next_idx_lock:
                nxt = _next_idx[0]
                _next_idx[0] = nxt + 5
            for i in range(nxt, nxt + 5):
                task_queue.put(i)
            continue

        retry = 0
        while retry < 2:
            if cancel_callback and cancel_callback():
                break
            try:
                result = register_one(
                    worker_id,
                    idx,
                    total,
                    accounts_file,
                    do_mint_inline=do_mint_inline,
                    mint_queue=mint_queue,
                    cancel_callback=cancel_callback,
                )
                if result:
                    break
                retry += 1
                if retry < 2 and not (cancel_callback and cancel_callback()):
                    log(worker_id, f"[retry] 账号 {idx} 失败，重试 {retry}/1")
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass
            except Exception:
                retry += 1
                if retry < 2 and not (cancel_callback and cancel_callback()):
                    log(worker_id, f"[retry] 账号 {idx} 异常，重试 {retry}/1")
                    traceback.print_exc()
                    try:
                        reg.restart_browser(log_callback=lambda m: log(worker_id, m))
                    except Exception:
                        pass

        if retry >= 2:
            # register_one already counted fail on exception path; if both returned None, count once more only if needed
            pass

    # worker exit: free browser
    try:
        reg.stop_browser()
    except Exception:
        pass
    try:
        reg.release_registration_proxy()
    except Exception:
        pass
    log(worker_id, "register worker exit")


def _mint_worker(worker_id: str, mint_queue: queue.Queue, config: dict):
    while True:
        job = mint_queue.get()
        try:
            if job is _MINT_STOP:
                break
            if not isinstance(job, dict):
                continue
            _run_mint_job(worker_id, job, config)
        finally:
            mint_queue.task_done()
    try:
        from cpa_xai.browser_confirm import shutdown_mint_browsers

        shutdown_mint_browsers()
    except Exception:
        pass
    log(worker_id, "mint worker exit")


def main() -> int:
    parser = argparse.ArgumentParser(description="CLI runner for grok_register_ttk (pipelined).")
    parser.add_argument("--count", type=int, default=1, help="账号总数目标（0=不限；含已有）")
    parser.add_argument(
        "--extra",
        type=int,
        default=0,
        help="在已有 accounts 基础上再新注册 N 个",
    )
    parser.add_argument("--threads", type=int, default=1, help="注册并发线程数（1-10）")
    parser.add_argument(
        "--mint-workers",
        type=int,
        default=-1,
        help="CPA mint 并发：-1=用 config/auto；0=内联；1-10=固定。覆盖 config.cpa_mint_workers",
    )
    parser.add_argument(
        "--mint-queue-max",
        type=int,
        default=-1,
        help="mint 队列背压上限：-1=用 config/auto(2×workers)；0=不限制",
    )
    parser.add_argument("--accounts-file", default=os.path.join(os.path.dirname(__file__), "accounts_cli.txt"))
    parser.add_argument("--fast", action="store_true", default=True, help="快速模式（默认开）：压缩 sleep、关截图")
    parser.add_argument("--no-fast", action="store_true", help="关闭快速模式")
    parser.add_argument("--no-browser-reuse", action="store_true", help="每号强制 quit 浏览器")
    parser.add_argument("--browser-recycle-every", type=int, default=25, help="复用 N 次后完整回收")
    parser.add_argument("--cookie-snapshot", action="store_true", help="注册成功写 cookie 快照（默认关，fast）")
    parser.add_argument("--inline-mint", action="store_true", help="强制注册线程内联 mint（调试用）")
    args = parser.parse_args()

    reg.load_config()
    cfg0 = getattr(reg, "config", {}) or {}
    threads = max(1, min(args.threads, 10))
    fast = bool(args.fast) and not bool(args.no_fast)

    mint_workers = resolve_mint_workers(
        cli_value=args.mint_workers,
        threads=threads,
        config=cfg0,
        inline_mint=bool(args.inline_mint),
    )
    do_mint_inline = mint_workers == 0
    mint_qmax = resolve_mint_queue_max(
        cfg0,
        mint_workers,
        cli_value=(None if args.mint_queue_max < 0 else args.mint_queue_max),
    )

    # perf knobs
    reg.configure_perf(
        fast=fast,
        sleep_scale=0.15 if fast else 1.0,
        skip_debug_io=fast,
        cookie_snapshot=bool(args.cookie_snapshot) or not fast,
        async_side_effects=True,
        browser_reuse=not args.no_browser_reuse,
        browser_recycle_every=max(1, int(args.browser_recycle_every)),
    )

    # 断点续跑
    done_count = 0
    if os.path.exists(args.accounts_file):
        with open(args.accounts_file) as f:
            done_count = sum(1 for line in f if line.strip())

    if args.extra and args.extra > 0:
        target_total = done_count + args.extra
        remaining = args.extra
        print(
            f"[*] 配置加载完成，额外新注册 {args.extra} 个（当前已有 {done_count} → 目标 {target_total}），"
            f"注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
        args.count = target_total
    elif args.count == 0:
        remaining = None
        print(
            f"[*] 配置加载完成，不限数量，注册线程={threads} mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    else:
        remaining = max(0, args.count - done_count)
        print(
            f"[*] 配置加载完成，目标 {args.count} 个账号，注册线程={threads} "
            f"mint_workers={mint_workers} mint_queue_max={mint_qmax} fast={fast}",
            flush=True,
        )
    print(f"[*] accounts_file = {args.accounts_file}", flush=True)
    if done_count > 0:
        print(f"[*] 断点续跑：已完成 {done_count}", flush=True)
    if remaining is not None and remaining <= 0:
        print("[*] 所有账号已完成，无需继续（可用 --extra N 再注册）", flush=True)
        return 0

    log_thread = threading.Thread(target=_log_writer, daemon=True)
    log_thread.start()

    try:
        reg.TabPool.init(reg.create_browser_options, log_callback=lambda m: log(0, m))
    except Exception as exc:
        print(f"[!] 浏览器初始化失败: {exc}", flush=True)
        return 1

    task_queue: queue.Queue = queue.Queue()
    mint_queue: queue.Queue | None = queue.Queue() if not do_mint_inline else None
    if mint_queue is not None:
        mint_queue._reg_qmax = mint_qmax  # type: ignore[attr-defined]
    global _next_idx
    _next_idx[0] = done_count + 1
    if remaining is not None:
        for i in range(done_count + 1, args.count + 1):
            task_queue.put(i)
    else:
        for i in range(done_count + 1, done_count + threads * 5 + 1):
            task_queue.put(i)
        _next_idx[0] = done_count + threads * 5 + 1

    forever = remaining is None
    cfg = getattr(reg, "config", {}) or {}

    # mint workers first (so queue consumers ready)
    mint_threads: list[threading.Thread] = []
    if mint_queue is not None and mint_workers > 0:
        for i in range(1, mint_workers + 1):
            wid = f"M{i}"
            t = threading.Thread(
                target=_mint_worker,
                args=(wid, mint_queue, cfg),
                daemon=True,
                name=f"mint-{i}",
            )
            t.start()
            mint_threads.append(t)

    reg_threads: list[threading.Thread] = []
    for wid in range(1, threads + 1):
        t = threading.Thread(
            target=_register_worker,
            args=(wid, task_queue, args.count, args.accounts_file, mint_queue, forever, do_mint_inline),
            daemon=True,
            name=f"reg-{wid}",
        )
        t.start()
        reg_threads.append(t)

    try:
        for t in reg_threads:
            t.join()
    except KeyboardInterrupt:
        print("\n[!] 用户中断", flush=True)

    # drain mint queue
    if mint_queue is not None:
        log(0, f"[cpa] 等待 mint 队列清空（qsize≈{mint_queue.qsize()}）...")
        mint_queue.join()
        for _ in mint_threads:
            mint_queue.put(_MINT_STOP)
        for t in mint_threads:
            t.join(timeout=600)

    try:
        reg.shutdown_browser()
    except Exception:
        pass

    # stop side-effect pool
    try:
        pool = getattr(reg, "_side_effect_pool", None)
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
    except Exception:
        pass

    _log_queue.put(None)
    log_thread.join(timeout=2)

    with _stats_lock:
        s = dict(_stats)
    print(
        f"=== 完成: 注册成功 {s.get('reg_success', 0)}, 注册失败 {s.get('reg_fail', 0)}, "
        f"CPA成功 {s.get('mint_success', 0)}, CPA失败 {s.get('mint_fail', 0)}, "
        f"CPA跳过 {s.get('mint_skip', 0)} ===",
        flush=True,
    )
    return 0 if s.get("reg_success", 0) > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
