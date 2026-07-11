# Grok 注册机（grok_reg）

基于 **Chromium + DrissionPage + turnstilePatch** 的免费 Grok 账号注册机。

一条成功链路会产出两类凭证：

| 产物 | 用途 | 路径 |
|------|------|------|
| **SSO** | grok.com / grok2api Web 池 | `accounts_cli.txt` 第三段 + 可选推远端池 |
| **OIDC（CPA xAI）** | 免费 **Grok 4.5**（Grok Build / cli-chat-proxy） | `cpa_auths/xai-<email>.json` |

> **硬约束：SSO ≠ OIDC。**  
> 免费 Grok 4.5 **不能**用 `accounts_cli` 里的 sso JWT 直接打；必须再走  
> `accounts.x.ai` device-auth 铸 OIDC，写成 CPA 的 `type=xai` 认证文件。

本仓库**自包含** OIDC/CPA 铸造代码（已内置 `cpa_xai/`）：

- 库：`./cpa_xai/`
- 成功 hook：`./cpa_export.py`
- 批量补认证：`./scripts/backfill_cpa_xai_from_accounts.py`
- 从 grok auth 导出：`./scripts/export_cpa_xai_from_grok_auth.py`

---

## 整链示意

```
[邮箱 CloudMail] → 注册 accounts.x.ai
       ↓
 accounts_cli.txt          email----password----sso
       ↓
 grok2api 池 (可选)        SSO → Web 非 4.5 模型
       ↓
 device-auth OIDC mint     有头 Chromium + turnstilePatch
       ↓
 cpa_auths/xai-email.json  【注册机主导出，必须】
       ↓ (cpa_copy_to_hotload=true 时)
 CPA auth-dir 热加载       【可选】
       ↓
 CLIProxyAPI :8317         /v1/chat/completions  model=grok-4.5
```

---

## 环境

| 依赖 | 说明 |
|------|------|
| Linux + 桌面 `DISPLAY` | OIDC 确认默认**有头**浏览器；纯 headless 易被 Cloudflare 拦 |
| `mise` + `uv` + Python 3.12/3.13 | 本目录 `mise.toml` / `uv.lock` |
| `chromium` | DrissionPage 驱动 |
| 代理 | xAI / accounts.x.ai 通常需要，如 `http://127.0.0.1:7890` |
| 可选 | 本机 grok2api `:8000`、CLIProxyAPI(CPA) `:8317` |

```bash
cd /path/to/grok_reg
mise install
uv sync
uv run python -c "from DrissionPage import Chromium; print('OK')"
```

---

## 配置

1. 复制模板并编辑（模板内 `"//…"` 键是注释，加载时忽略）：

```bash
cp config.example.json config.json
# 编辑 config.json：邮箱、proxy、grok2api、cpa_*
```

2. **每个字段含义见 `config.example.json` 内注释键**，勿删示例里的 `//_readme` 段。  
   运行时读取的是 `config.json`（可同样使用 `//` 注释键）。

### 代理优先级（重要）

| 字段 | 作用 |
|------|------|
| `proxy` | **注册** Chromium + 邮箱等 HTTP |
| `cpa_proxy` | **OIDC mint**（device-code / token 轮询 / probe / 确认浏览器） |

解析顺序（配置优先，**盖过** shell 的 `https_proxy`）：

```
cpa_proxy  >  proxy  >  环境变量 https_proxy/http_proxy
```

- 只配 `proxy=http://127.0.0.1:7890` 且 `cpa_proxy` 为空 → mint 也走 7890  
- 两者都配 → mint 只用 `cpa_proxy`  
- 以前调试时在 shell 里 `export https_proxy=7890` **不会再压过** config  
- Chromium `--proxy-server` **不能**带 `user:pass`（账号会剥掉，仅 host:port）；HTTP 库仍可用带认证的 URL  

### 与 CPA 相关的关键项（摘要）

| 字段 | 含义 | 建议 |
|------|------|------|
| `cpa_export_enabled` | 注册成功后是否 mint OIDC | `true` |
| `cpa_auth_dir` | **主导出目录**（注册机内） | `./cpa_auths` 或绝对路径 |
| `cpa_copy_to_hotload` | 是否复制到 CPA 热加载目录 | **可选**，默认模板 `false` |
| `cpa_hotload_dir` | CPA `auth-dir` 挂载路径 | 仅 copy 时需要 |
| `cpa_base_url` | 上游 API 根 | **必须** `https://cli-chat-proxy.grok.com/v1` |
| `cpa_headless` | 浏览器无头 | **`false`**（推荐） |
| `cpa_force_standalone` | 独立浏览器做 consent | **`true`** |
| `cpa_proxy` | mint 专用代理 | 如 `http://127.0.0.1:7890`；空则用 `proxy` |
| `proxy` | 注册主代理 | 如 本机 HTTP 代理 `7890` |
| `api_reverse_tools` | 高级：自定义 `cpa_xai` 父目录；空=本仓库根 | 一般留空 |
| `cpa_mint_required` | mint 失败是否整号失败 | 通常 `false` |

### 落盘约定

| 路径 | 是否必须 | 说明 |
|------|----------|------|
| `accounts_cli.txt` | 是 | 主账本 `email----password----sso` |
| `cpa_auths/xai-*.json` | 是（开 export 时） | CPA 格式 OIDC 归档 |
| CPA `…/auths/xai-*.json` | 可选 | 热加载；由 `cpa_copy_to_hotload` 控制 |
| `cookies/grok/` | 可选 | 调试 cookie 快照 |
| ~~`tokens/grok/`~~ | **已移除** | 与 accounts_cli 冗余，不再写入 |

---

## 命令：批量注册 + 认证（整链）

前置：

```bash
export https_proxy=http://127.0.0.1:7890
export http_proxy=http://127.0.0.1:7890
export DISPLAY=${DISPLAY:-:0}
cd /path/to/grok_reg
```

### A. 新注册 N 个号（含 SSO + 可选 OIDC 导出）

```bash
# 在已有 accounts_cli 基础上再注册 1 个（推荐）
uv run python register_cli.py --extra 1 --threads 1

# 再注册 5 个
uv run python register_cli.py --extra 5 --threads 2

# --count 是「总数目标（含已有）」；已有 326 时 --count 326 会直接结束
# 不要用 --count 1 期望「只新开 1 个」——请用 --extra
uv run python register_cli.py --count 0 --threads 1   # 0=不限（慎用）
```

成功时：

1. 追加 `accounts_cli.txt`
2. 可选：推 grok2api `ssoBasic`
3. 若 `cpa_export_enabled`：device-auth → 写 `cpa_auths/xai-<email>.json`
4. 若 `cpa_copy_to_hotload`：再拷到 `cpa_hotload_dir`

有头窗口会弹出 Chromium；OIDC 段需点选流程由脚本自动完成（继续 → 邮箱登录 → **真实点击「允许」**）。

### B. 存量号补认证（只 mint OIDC，不重新注册）

对 `accounts_cli.txt` 里还没有 `xai-*.json` 的账号批量 device-auth：

```bash
cd /path/to/grok_reg
export DISPLAY=${DISPLAY:-:0}
# 代理可写 config.json 的 proxy / cpa_proxy

# 先跑 1 个验证
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --limit 1 --probe --timeout 300

# 全量补齐（跳过已有 xai 文件；约 40–60s/号）
uv run python -u scripts/backfill_cpa_xai_from_accounts.py \
  --limit 0 --probe --timeout 300 --sleep 3
```

常用参数：

| 参数 | 含义 |
|------|------|
| `--limit N` | 本次最多处理 N 个缺失号；`0`=全部缺失 |
| `--email x@y` | 只处理指定邮箱 |
| `--out-dir` | 注册机主导出目录 |
| `--cpa-dir` | 成功后**复制**到此（CPA 热加载）；不需要可指空目录或改脚本 |
| `--probe` | 检查 token 是否列出 `grok-4.5`（默认开） |
| `--probe-chat` | 再打一发最小 chat（更慢） |
| `--headless` | 无头（不推荐；易 CF） |
| `--no-skip-existing` | 强制重铸已有文件 |

> backfill 默认 **headed**。需本机显示与代理。

### C. 从已有 Grok Build `~/.grok/auth.json` 导出单个 CPA 文件

```bash
cd /path/to/grok_reg
uv run python scripts/export_cpa_xai_from_grok_auth.py \
  --out-dir ./cpa_auths
# 需要热加载时再手动 cp 到 CPA auth-dir
```

### D. 手动导入 CPA 热加载（`cpa_copy_to_hotload=false` 时）

```bash
# 单文件（CPA_AUTH_DIR = 你的 CLIProxyAPI auth-dir）
cp -a ./cpa_auths/xai-USER@domain.json "$CPA_AUTH_DIR"/
chmod 600 "$CPA_AUTH_DIR"/xai-USER@domain.json

# 或整目录同步（慎用：勿覆盖非 free 的 xai 付费文件）
# rsync -a --ignore-existing cpa_auths/xai-*.json "$CPA_AUTH_DIR"/
```

CPA 一般会监视 auth-dir；若无热加载，重启/刷新 CLIProxyAPI 后再测。

### E. 调用验证（免费 Grok 4.5）

```bash
# 读 CPA api-keys（示例）
KEY="<你的 CPA API KEY>"

curl -sS http://127.0.0.1:8317/v1/models -H "Authorization: Bearer $KEY" | head

curl -sS http://127.0.0.1:8317/v1/chat/completions \
  -H "Authorization: Bearer $KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "grok-4.5",
    "messages": [{"role":"user","content":"Reply with exactly OK"}],
    "stream": false
  }'
```

期望：`model` 类似 `grok-4.5-build-free`，HTTP 200。  
Agent（如 pi）可把 provider `baseUrl` 指到 `http://127.0.0.1:8317/v1`，**无需 agskin**。

---

## CLI 参数速查（`register_cli.py`）

| 参数 | 含义 |
|------|------|
| `--extra N` | **再新注册 N 个**（推荐） |
| `--count N` | 账号**总数目标**（含 `accounts_cli` 已有行）；已达标则退出 |
| `--threads N` | 并发 1–10 |
| `--accounts-file` | 账本路径，默认本目录 `accounts_cli.txt` |

---

## 故障排查（OIDC / CPA）

| 现象 | 原因 / 处理 |
|------|-------------|
| 一直 `authorization_pending` | 未真实完成 consent；需到「设备已授权」页且 token poll 200 |
| `Invalid action` | 用 JS 点了「允许」导致表单 action 空 → 必须**真实 click**（库已修） |
| 误点「全部允许」 | 按钮匹配必须 **exact**（`允许` ≠ `全部允许`） |
| Cloudflare Attention Required | 关掉 headless；确认 turnstilePatch、代理、有头 `DISPLAY` |
| 有 token 但无 grok-4.5 | `base_url` 是否为 `cli-chat-proxy`；probe 失败勿导入 |
| CPA 401 | api-key 错；或 auth 未进热加载目录 |
| 注册成功但无 `cpa_auths` | `cpa_export_enabled`？是否含 `cpa_xai/`？看日志 / `cpa_auth_failed.txt` |

调试原则：**逐步看真实页面**，不要只信 UI 文案里的 “return to your device”（整页 HTML 会误触发假成功）。以 **token 端点返回 `access_token`** 为准。

---

## 目录结构（精简）

```
grok_reg/
  register_cli.py          # CLI 批量注册入口
  grok_register_ttk.py     # 浏览器注册核心
  cpa_export.py            # 成功 hook → 本仓库 cpa_xai
  cpa_xai/                 # OIDC mint / CPA 写出（自包含）
  scripts/
    backfill_cpa_xai_from_accounts.py
    export_cpa_xai_from_grok_auth.py
  config.example.json      # 带 // 注释的字段模板
  config.json              # 本地实配（勿提交密钥）
  accounts_cli.txt         # 主账本 email----password----sso
  cpa_auths/               # 【本地归档】xai-<email>.json
  cookies/                 # 可选 cookie 快照
  turnstilePatch/          # CF Turnstile 扩展
```

---

## 安全

- `config.json`、`accounts_cli.txt`、`cpa_auths/*.json` 含密码与 refresh_token，**权限 600 / 勿提交 git**
- CPA 热加载目录同样 intermediate 密钥，注意备份与权限
- 免费 Build 有额度/风控；批量 mint 请控速（`--sleep`）

---


## 相关

- CLIProxyAPI / CPA：自备；把 `cpa_auths/xai-*.json` 拷到 CPA 的 auth-dir 即可热加载
- free Grok 4.5 只走 Build OIDC + `cli-chat-proxy`，不是网页 SSO
