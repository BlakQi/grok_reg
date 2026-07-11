"""Clean runtime overrides for registration paths affected by mojibake."""

from __future__ import annotations

import os
import queue
import time


def install_clean_runtime_overrides(g):
    original_start_browser = g.start_browser

    def start_browser(log_callback=None):
        try:
            return original_start_browser(log_callback=log_callback)
        except Exception as exc:
            raise Exception(f"浏览器启动失败: {exc}") from exc

    def prepare_browser_for_next_account(log_callback=None, force_recycle: bool = False):
        reuse = bool(g.PERF_FLAGS.get("browser_reuse", True)) and not force_recycle
        every = int(g.PERF_FLAGS.get("browser_recycle_every", 25) or 25)
        served = g.TabPool.served_count()
        if reuse and g.TabPool.get_browser() is not None and (every <= 0 or served < every):
            if g.TabPool.clear_session(log_callback=log_callback):
                g.TabPool.mark_served()
                return g.TabPool.get_browser(), g._get_page()
        if log_callback:
            log_callback(f"[*] 浏览器完整回收(reuse={reuse}, served={served}, every={every})")
        g.TabPool.release_tab()
        return start_browser(log_callback=log_callback)

    def click_email_signup_button(timeout=10, log_callback=None, cancel_callback=None):
        page = g._get_page()
        deadline = time.time() + timeout
        while time.time() < deadline:
            g.raise_if_cancelled(cancel_callback)
            if log_callback:
                log_callback("[Debug] 查找邮箱注册入口...")
            clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const usableInput = Array.from(document.querySelectorAll('input, textarea')).find((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (['password', 'hidden', 'checkbox', 'radio', 'submit', 'button'].includes(type)) return false;
    const meta = [
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
        node.getAttribute('aria-label'),
        node.getAttribute('placeholder'),
        node.getAttribute('inputmode'),
        type,
    ].join(' ').toLowerCase();
    if (meta.includes('email') || meta.includes('mail')) return true;
    const rect = node.getBoundingClientRect();
    return rect.width >= 160 && rect.height >= 28;
}) || null;
if (usableInput) {
    usableInput.focus();
    return 'email-input-ready';
}
const candidates = Array.from(document.querySelectorAll('button, a, [role="button"]')).filter((node) => isVisible(node));
const target = candidates.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    const meta = [
        node.getAttribute('aria-label'),
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('href'),
        node.id,
        node.className,
    ].join(' ').toLowerCase();
    return (
        text.includes('使用邮箱注册') ||
        text.includes('使用邮箱') ||
        text.includes('邮箱注册') ||
        text.includes('电子邮件') ||
        text.includes('邮箱') ||
        lower.includes('signupwithemail') ||
        lower.includes('continuewithemail') ||
        lower.includes('email') ||
        meta.includes('email') ||
        meta.includes('mail')
    );
});
if (!target) return false;
target.click();
return 'clicked';
            """)
            if clicked:
                if log_callback:
                    log_callback("[*] 邮箱输入框已就绪" if clicked == "email-input-ready" else "[*] 已点击邮箱注册入口")
                if clicked != "email-input-ready":
                    g.human_sleep(2, cancel_callback)
                return True
            if log_callback:
                current_url = page.url if page else "none"
                log_callback(f"[Debug] 当前URL: {current_url}")
            g.human_sleep(1, cancel_callback)
        if log_callback:
            page_html = page.html[:500] if page else "no page"
            log_callback(f"[Debug] 页面内容片段: {page_html}")
        raise Exception("未找到邮箱注册入口")

    def open_signup_page(log_callback=None, cancel_callback=None):
        browser = g._get_browser()
        page = g._get_page()
        g.raise_if_cancelled(cancel_callback)
        if browser is None:
            browser, page = start_browser(log_callback=log_callback)
            if log_callback:
                log_callback("[*] 浏览器已启动")
        try:
            page = g._get_page()
            page.get(g.SIGNUP_URL)
        except Exception as e:
            if log_callback:
                log_callback(f"[Debug] 打开URL异常: {e}")
            try:
                g.TabPool.release_tab()
                start_browser(log_callback=log_callback)
                page = g._get_page()
                page.get(g.SIGNUP_URL)
            except Exception as e2:
                if log_callback:
                    log_callback(f"[Debug] 创建新标签页异常: {e2}")
                g.restart_browser(log_callback=log_callback)
                page = g._get_page()
                page.get(g.SIGNUP_URL)
        page.wait.doc_loaded()
        g.dump_state(page, "signup-loaded")
        g.take_screenshot(page, "signup")
        g.human_sleep(2, cancel_callback)
        if log_callback:
            log_callback(f"[*] 当前URL: {page.url}")
        click_email_signup_button(log_callback=log_callback, cancel_callback=cancel_callback)
        g.dump_state(page, "after-email-signup-click")

    def fill_email_and_submit(timeout=20, log_callback=None, cancel_callback=None):
        page = g._get_page()
        g.raise_if_cancelled(cancel_callback)
        g.check_timeout(time.time())
        email, dev_token = g.get_email_and_token()
        if not email or not dev_token:
            raise Exception("获取邮箱失败")
        g.mark_reserved(email, reason="acquired")
        if log_callback:
            log_callback(f"[*] 准备填写邮箱: {email}")
        deadline = time.time() + timeout
        while time.time() < deadline:
            g.raise_if_cancelled(cancel_callback)
            filled = page.run_js(
                """
const email = arguments[0];
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input, textarea')).find((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const type = String(node.getAttribute('type') || '').toLowerCase();
    if (['password', 'hidden', 'checkbox', 'radio', 'submit', 'button'].includes(type)) return false;
    const meta = [
        node.getAttribute('data-testid'),
        node.getAttribute('name'),
        node.getAttribute('id'),
        node.getAttribute('autocomplete'),
        node.getAttribute('aria-label'),
        node.getAttribute('placeholder'),
        node.getAttribute('inputmode'),
        type,
    ].join(' ').toLowerCase();
    if (meta.includes('email') || meta.includes('mail')) return true;
    const rect = node.getBoundingClientRect();
    return rect.width >= 160 && rect.height >= 28 && !node.value;
}) || null;
if (!input) return 'not-ready';
input.focus();
input.click();
const valueSetter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
const tracker = input._valueTracker;
if (tracker) tracker.setValue('');
if (valueSetter) valueSetter.call(input, email);
else input.value = email;
input.dispatchEvent(new Event('focus', { bubbles: true }));
input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new InputEvent('input', { bubbles: true, data: email, inputType: 'insertText' }));
input.dispatchEvent(new Event('change', { bubbles: true }));
input.dispatchEvent(new Event('blur', { bubbles: true }));
const current = (input.value || '').trim();
if (current === email) return 'filled';
input.value = '';
input.dispatchEvent(new Event('input', { bubbles: true }));
for (const ch of email) {
    input.dispatchEvent(new KeyboardEvent('keydown', { key: ch, bubbles: true }));
    input.value += ch;
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: ch, inputType: 'insertText' }));
    input.dispatchEvent(new KeyboardEvent('keyup', { key: ch, bubbles: true }));
}
input.dispatchEvent(new Event('change', { bubbles: true }));
return (input.value || '').trim() === email ? 'filled' : input.value;
                """,
                email,
            )
            if filled == "not-ready":
                g.human_sleep(0.5, cancel_callback)
                continue
            if filled != "filled":
                if log_callback:
                    log_callback(f"[Debug] 邮箱输入框已出现，但写入失败: {filled}")
                g.human_sleep(0.5, cancel_callback)
                continue
            g.human_sleep(0.8, cancel_callback)
            clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const input = Array.from(document.querySelectorAll('input, textarea')).find((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    return String(node.value || '').trim().includes('@');
}) || null;
if (!input || !(input.value || '').trim()) return false;
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const submitButton = buttons.find((node) => {
    const text = (node.innerText || node.textContent || '').replace(/\s+/g, '');
    const lower = text.toLowerCase();
    const meta = [
        node.getAttribute('data-testid'),
        node.getAttribute('aria-label'),
        node.getAttribute('name'),
        node.id,
        node.className,
        node.getAttribute('type'),
    ].join(' ').toLowerCase();
    return (
        text === '注册' ||
        text.includes('注册') ||
        text.includes('继续') ||
        text.includes('下一步') ||
        text.includes('确认') ||
        text.includes('提交') ||
        lower.includes('sign up') ||
        lower.includes('continue') ||
        lower.includes('next') ||
        lower.includes('submit') ||
        meta.includes('submit')
    );
}) || buttons.find((node) => String(node.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0];
if (submitButton && !submitButton.disabled) {
    submitButton.click();
    return 'clicked';
}
input.focus();
input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', bubbles: true }));
input.dispatchEvent(new KeyboardEvent('keyup', { key: 'Enter', code: 'Enter', bubbles: true }));
return 'enter';
            """)
            if clicked:
                if log_callback:
                    log_callback(f"[*] 已填写邮箱并提交: {email}")
                g.dump_state(page, "email-submitted")
                g.take_screenshot(page, "email-submitted")
                return email, dev_token
            g.human_sleep(0.5, cancel_callback)
        raise Exception("未找到邮箱输入框或注册按钮")

    def fill_code_and_submit(email, dev_token, timeout=180, log_callback=None, cancel_callback=None):
        page = g._get_page()
        g.check_timeout(time.time())
        g.dump_state(page, "wait-code")
        g.take_screenshot(page, "wait-code")

        def _resend_code():
            page.run_js(r"""
const nodes = Array.from(document.querySelectorAll('button, a, [role="button"]'));
const target = nodes.find((node) => {
  const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
  return t.includes('重新发送') || t.includes('重新获取') || t.includes('再发') || t.includes('resend') || t.includes('sendagain');
});
if (target && !target.disabled) { target.click(); return true; }
return false;
            """)

        code = g.get_oai_code(
            dev_token,
            email,
            log_callback=log_callback,
            cancel_callback=cancel_callback,
            resend_callback=_resend_code,
        )
        if not code:
            raise Exception("获取验证码失败")
        clean_code = str(code).replace("-", "").strip()
        deadline = time.time() + timeout
        while time.time() < deadline:
            g.raise_if_cancelled(cancel_callback)
            filled = page.run_js(
                """
const code = String(arguments[0] || '').trim();
if (!code) return 'empty-code';
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
function setInputValue(input, value) {
    const nativeSetter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
    const tracker = input._valueTracker;
    if (tracker) tracker.setValue('');
    if (nativeSetter) nativeSetter.call(input, value);
    else input.value = value;
    input.dispatchEvent(new InputEvent('beforeinput', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new InputEvent('input', { bubbles: true, data: value, inputType: 'insertText' }));
    input.dispatchEvent(new Event('change', { bubbles: true }));
}
const aggregate = Array.from(document.querySelectorAll(
  'input[data-input-otp="true"], input[name="code"], input[autocomplete="one-time-code"], input[inputmode="numeric"], input[inputmode="text"]'
)).find((node) => isVisible(node) && !node.disabled && !node.readOnly && Number(node.maxLength || 6) > 1);
if (aggregate) {
    aggregate.focus();
    aggregate.click();
    setInputValue(aggregate, code);
    return String(aggregate.value || '').replace(/\\s+/g, '') ? 'filled-aggregate' : 'aggregate-failed';
}
const otpBoxes = Array.from(document.querySelectorAll('input')).filter((node) => {
    if (!isVisible(node) || node.disabled || node.readOnly) return false;
    const maxLength = Number(node.maxLength || 0);
    const ac = String(node.autocomplete || '').toLowerCase();
    return maxLength === 1 || ac === 'one-time-code';
});
if (otpBoxes.length >= code.length) {
    for (let i = 0; i < code.length; i += 1) {
        const ch = code[i] || '';
        const box = otpBoxes[i];
        box.focus();
        box.click();
        setInputValue(box, ch);
        box.dispatchEvent(new KeyboardEvent('keydown', { bubbles: true, key: ch }));
        box.dispatchEvent(new KeyboardEvent('keyup', { bubbles: true, key: ch }));
    }
    const merged = otpBoxes.slice(0, code.length).map((x) => String(x.value || '').trim()).join('');
    return merged.length ? 'filled-boxes' : 'boxes-failed';
}
return 'not-ready';
                """,
                clean_code,
            )
            if filled == "not-ready":
                g.human_sleep(0.5, cancel_callback)
                continue
            if "failed" in str(filled):
                if log_callback:
                    log_callback(f"[Debug] 验证码填写失败: {filled}")
                g.human_sleep(0.5, cancel_callback)
                continue
            clicked = page.run_js(r"""
function isVisible(node) {
    if (!node) return false;
    const style = window.getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
}
const buttons = Array.from(document.querySelectorAll('button[type="submit"], button, [role="button"]')).filter((node) => {
    return isVisible(node) && !node.disabled && node.getAttribute('aria-disabled') !== 'true';
});
const btn = buttons.find((node) => {
    const t = (node.innerText || node.textContent || '').replace(/\s+/g, '').toLowerCase();
    return (
        t.includes('确认') ||
        t.includes('继续') ||
        t.includes('下一步') ||
        t.includes('验证') ||
        t.includes('confirm') ||
        t.includes('continue') ||
        t.includes('next') ||
        t.includes('verify')
    );
}) || buttons.find((node) => String(node.getAttribute('type') || '').toLowerCase() === 'submit') || buttons[0];
if (!btn) return 'no-button';
btn.focus();
btn.click();
return 'clicked';
            """)
            if clicked == "clicked" or clicked == "no-button":
                if log_callback:
                    log_callback(f"[*] 已填写验证码并提交: {code}")
                g.human_sleep(1.5, cancel_callback)
                return code
            g.human_sleep(0.5, cancel_callback)
        raise Exception("验证码已获取，但自动填写/提交失败")

    def gui_run_single_registration(self, idx, total, logf):
        email = ""
        dev_token = ""
        code = ""
        mail_ok = False
        max_mail_retry = 3
        for mail_try in range(1, max_mail_retry + 1):
            logf(f"[*] 1. 打开注册页 (尝试 {mail_try}/{max_mail_retry})")
            open_signup_page(log_callback=logf, cancel_callback=self.should_stop)
            logf("[*] 2. 创建邮箱并提交")
            email, dev_token = fill_email_and_submit(log_callback=logf, cancel_callback=self.should_stop)
            logf(f"[*] 邮箱: {email}")
            try:
                with open(os.path.join(os.path.dirname(g.__file__), "mail_credentials.txt"), "a", encoding="utf-8") as f:
                    f.write(f"{email}\t{dev_token}\n")
            except Exception:
                pass
            logf("[*] 3. 拉取验证码")
            try:
                code = fill_code_and_submit(email, dev_token, log_callback=logf, cancel_callback=self.should_stop)
                mail_ok = True
                break
            except Exception as mail_exc:
                msg = str(mail_exc)
                g.mark_error(email or "", reason=msg[:120])
                if any(
                    term in msg.lower()
                    for term in (
                        "code",
                        "verification",
                        "timeout",
                        "验证码",
                        "未收到",
                        "outlook_code_timeout",
                        "token_refresh",
                        "refresh_token",
                        "invalid_grant",
                        "aadsts",
                        "login_required",
                        "unauthorized",
                    )
                ) and mail_try < max_mail_retry:
                    logf(f"[!] 本邮箱未取到验证码，自动更换新邮箱重试: {msg}")
                    g.restart_browser(log_callback=logf)
                    g.sleep_with_cancel(1, self.should_stop)
                    continue
                raise
        if not mail_ok:
            raise Exception("验证码阶段失败，已达到最大重试次数")
        logf(f"[*] 验证码: {code}")
        logf("[*] 4. 填写资料")
        profile = g.fill_profile_and_submit(log_callback=logf, cancel_callback=self.should_stop)
        logf(f"[*] 资料已填写: {profile.get('given_name')} {profile.get('family_name')}")
        logf("[*] 5. 等待 sso cookie")
        sso = g.wait_for_sso_cookie(log_callback=logf, cancel_callback=self.should_stop)
        with self.stats_lock:
            self.results.append({"email": email, "sso": sso, "profile": profile})
            self.success_count += 1
            line = f"{email}----{profile.get('password','')}----{sso}\n"
            try:
                with open(self.accounts_output_file, "a", encoding="utf-8") as f:
                    f.write(line)
            except Exception as file_exc:
                logf(f"[Debug] 保存账号文件失败: {file_exc}")
        g.add_token_to_grok2api_pools(sso, email=email, log_callback=logf)
        g.mark_used(email, profile.get("password", ""))
        logf(f"[+] 注册成功: {email}")

    def gui_worker_loop(self, worker_id, total, task_queue):
        prefix = f"[T{worker_id}]"
        logf = lambda m: self.log(f"{prefix} {m}")
        try:
            start_browser(log_callback=logf)
            logf("[*] 浏览器已启动")
            while not self.should_stop():
                try:
                    idx = task_queue.get_nowait()
                except queue.Empty:
                    break
                logf(f"--- 开始第 {idx}/{total} 个账号 ---")
                try:
                    self._run_single_registration(idx, total, logf)
                except g.RegistrationCancelled:
                    logf("[!] 注册被用户停止")
                    break
                except Exception as exc:
                    with self.stats_lock:
                        self.fail_count += 1
                    logf(f"[-] 注册失败: {exc}")
                finally:
                    self.update_stats()
                    if self.should_stop():
                        break
                    g.restart_browser(log_callback=logf)
                    g.sleep_with_cancel(1, self.should_stop)
        except Exception as exc:
            logf(f"[!] 线程异常: {exc}")
        finally:
            g.stop_browser()

    g.start_browser = start_browser
    g.prepare_browser_for_next_account = prepare_browser_for_next_account
    g.click_email_signup_button = click_email_signup_button
    g.open_signup_page = open_signup_page
    g.fill_email_and_submit = fill_email_and_submit
    g.fill_code_and_submit = fill_code_and_submit
    g.GrokRegisterGUI._run_single_registration = gui_run_single_registration
    g.GrokRegisterGUI._worker_loop = gui_worker_loop
