# -*- coding: utf-8 -*-
"""OpenClaw 微信通知发送器。

通过本地 openclaw gateway 的微信频道（``openclaw-weixin``）把消息推送到微信。
底层调用 ``openclaw message send`` CLI（或显式配置的二进制路径）。
openclaw 与微信之间通过 iLink 平台打通，bot 账号与接收人微信 userId 由配置指定。

gateway 兜底：本通道依赖本地 openclaw gateway（默认 127.0.0.1:18789）。
发送前会快速探测 gateway 是否在线；若不在线，则尝试自动重启
（``gateway restart`` -> ``gateway start`` -> ``gateway run --force`` 后台拉起），
等待其就绪后重试一次。重启失败不影响其他通知渠道。
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)

# 微信单条消息建议上限，超出自动截断。
OPENCLAW_WECHAT_MAX_BYTES = 4000
# 子进程调用默认超时（秒）；CLI（尤其 npx 冷启动）可能较慢，实际下限会拉高。
DEFAULT_SEND_TIMEOUT_SECONDS = 30
_OPENCLAW_CLI_MIN_TIMEOUT_SECONDS = 120

# 决策仪表盘微信推送失败后的轮询重试间隔（秒）。
_OPENCLAW_WECHAT_RETRY_INTERVAL_SECONDS = 1800  # 30 分钟

# gateway 探测/重启兜底参数。
_OPENCLAW_GATEWAY_HOST = "127.0.0.1"
_OPENCLAW_GATEWAY_DEFAULT_PORT = 18789
# TCP 探测超时（秒），仅用于快速判断 gateway 是否在线。
_OPENCLAW_GATEWAY_PROBE_TIMEOUT = 0.6
# 重启后最长等待 gateway 就绪的时间（秒）；gateway run 冷启动可能较慢（本机实测 ~45s）。
_OPENCLAW_GATEWAY_MAX_STARTUP_WAIT = 60.0
# 重启后轮询 gateway 就绪的间隔（秒）。
_OPENCLAW_GATEWAY_POLL_INTERVAL = 2.0
# 同进程内两次重启尝试的最小间隔（秒），避免 gateway 启动失败时反复拉起。
_OPENCLAW_GATEWAY_RESTART_COOLDOWN = 60.0
# 拉起 gateway（gateway run --force）子进程的超时（秒）。
_OPENCLAW_GATEWAY_START_TIMEOUT = 15
# 两次自动清理 context-tokens.json 的最小间隔（秒），避免巡检反复删除刚重建的 token。
_OPENCLAW_CONTEXT_TOKEN_COOLDOWN = 6 * 3600
# 两次因 crash-loop breaker 触发 gateway 重启的最小间隔（秒）：breaker 本身有 5 分钟
# 窗口观察，重启过频反而会被再次抑制，故限制为至少 7 分钟一次。
_OPENCLAW_CRASH_LOOP_RESTART_COOLDOWN = 7 * 60
# channels status 中 lastError 命中以下关键字即判定为「crash loop breaker 抑制通道」。
_OPENCLAW_CRASH_LOOP_PATTERNS = (
    "crash loop breaker",
    "crash_loop_breaker",
    "restart-loop breaker",
    "unclean boot",
    "boot-loop",
)

# 失败语义分类：让上层（告警/探活/重试策略）能区分不同根因，避免对持续性
# 故障做无效重试。命名与 ``last_error_type`` 字段一致（短横线风格字符串）。
OPENCLAW_ERR_OK = "ok"
OPENCLAW_ERR_AUTH_NEEDED = "auth_needed"   # 微信/iLink bot 授权失效，需重新扫码登录
OPENCLAW_ERR_CDN = "cdn_error"             # 图片 CDN 上传服务端错误
OPENCLAW_ERR_GATEWAY = "gateway_down"      # gateway 不可达 / 连接拒绝
OPENCLAW_ERR_TIMEOUT = "timeout"           # 子进程超时
OPENCLAW_ERR_NOT_CONFIGURED = "not_configured"
OPENCLAW_ERR_OTHER = "other"

# 错误文本 → 错误类型的关键字映射（小写匹配）。顺序敏感：先匹配更具体的
# 类型（auth_needed / cdn_error），避免 gateway_down 把它们抢走。
_AUTH_NEEDED_PATTERNS = (
    "prepare failed",
    "not logged in",
    "need login",
    "need relogin",
    "re-login",
    "qrcode",
    "scan qr",
    "auth expired",
    "token expired",
    "unauthorized",
    "登录态过期",
    "重新登录",
)
_CDN_ERROR_PATTERNS = (
    "cdn upload server error",
    "cdn upload failed",
    "media upload",
    "outbounddeliveryerror",
    "status 500",
    "status 502",
    "status 503",
)
_GATEWAY_DOWN_PATTERNS = (
    "connection refused",
    "econnrefused",
    "could not connect",
    "no route to host",
    "websocket",
    "ws disconnected",
    "ws closed",
    "ws not open",
)


def classify_openclaw_send_error(
    *,
    returncode: int | None = None,
    stderr: str = "",
    stdout: str = "",
    timed_out: bool = False,
) -> str:
    """根据 CLI 输出文本与返回码，把发送失败归类到 ``OPENCLAW_ERR_*`` 之一。

    命中 ``OPENCLAW_ERR_AUTH_NEEDED`` / ``OPENCLAW_ERR_CDN`` 通常意味着
    重试 gateway / 重试发送都无法恢复，需要人工介入（重新扫码登录 / 等待
    CDN 恢复），调度层据此跳过无效重试。
    """
    if timed_out:
        return OPENCLAW_ERR_TIMEOUT
    text = ((stderr or "") + "\n" + (stdout or "")).lower()
    for pattern in _AUTH_NEEDED_PATTERNS:
        if pattern in text:
            return OPENCLAW_ERR_AUTH_NEEDED
    for pattern in _CDN_ERROR_PATTERNS:
        if pattern in text:
            return OPENCLAW_ERR_CDN
    for pattern in _GATEWAY_DOWN_PATTERNS:
        if pattern in text:
            return OPENCLAW_ERR_GATEWAY
    if returncode is None or returncode == 0:
        return OPENCLAW_ERR_OK
    return OPENCLAW_ERR_OTHER


class OpenclawWechatSender:
    """通过 openclaw 微信频道推送文本/图片消息。"""

    # ---- 类级共享的冷却状态 ------------------------------------------------
    # 发送链路与巡检**每次都会新建 sender 实例**，若这些时间戳做成实例变量，
    # 冷却会在每次新建实例时被重置成 0 → 冷却完全失效。
    # 2026-08-31 事故：contextToken 失效时每 30 秒重启一次 gateway，1 小时触发
    # 47 次重启（gateway 总 runs 48），把 iLink 会话反复冲断，形成
    # 「prepare failed → 重启 → 会话断 → prepare failed」的死循环。
    _last_restart_attempt = 0.0
    _last_context_clear_ts = 0.0
    _last_context_failure_ts = 0.0
    _last_crash_loop_restart_ts = 0.0

    def __init__(self, config) -> None:
        self._account = (getattr(config, "openclaw_wechat_account", "") or "").strip()
        self._target = (getattr(config, "openclaw_wechat_target", "") or "").strip()
        self._cli_bin = (getattr(config, "openclaw_cli_bin", "") or "").strip()
        self._enabled = bool(self._account and self._target)
        # gateway 探测端口（loopback-only gateway，固定 127.0.0.1）。
        self._gateway_port = int(
            os.getenv("OPENCLAW_GATEWAY_PORT")
            or getattr(config, "openclaw_gateway_port", _OPENCLAW_GATEWAY_DEFAULT_PORT)
            or _OPENCLAW_GATEWAY_DEFAULT_PORT
        )
        # 注意：_last_restart_attempt / _last_context_clear_ts / _last_context_failure_ts /
        # _last_crash_loop_restart_ts 已提升为**类属性**（见类定义处说明），此处不再初始化，
        # 否则实例属性会遮蔽类属性、导致冷却失效。
        # 解析 CLI（缓存，避免每次发送都走 npx 冷启动）。
        self._cli = self._resolve_cli()
        # 最近一次自动修复的诊断信息（供调用方生成更精确的错误提示）。
        self.last_repair_info: dict | None = None
        # 最近一次发送的失败语义（``OPENCLAW_ERR_*`` 之一）。成功发送后会重置为 OK。
        self.last_error_type: str = OPENCLAW_ERR_NOT_CONFIGURED if not self._enabled else OPENCLAW_ERR_OK
        # 最近一次失败的原始文本（截断到 ~300 字符），供告警/日志展示。
        self.last_error_detail: str = ""
        # 最近一次自动修复是否判定为“需用户给 bot 发消息重建 context”。
        self.last_needs_user_message: bool = False

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @staticmethod
    def _truncate(content: str, max_bytes: int) -> str:
        data = content.encode("utf-8")
        if len(data) <= max_bytes:
            return content
        return data[:max_bytes].decode("utf-8", "ignore")

    @staticmethod
    def _split_text_by_bytes(text: str, limit: int) -> List[str]:
        """按 UTF-8 字节数安全切片（不切断多字节字符），用于超长消息分条发送。"""
        parts: List[str] = []
        cur = ""
        cur_bytes = 0
        for ch in text:
            b = len(ch.encode("utf-8"))
            if cur_bytes + b > limit and cur:
                parts.append(cur)
                cur = ch
                cur_bytes = b
            else:
                cur += ch
                cur_bytes += b
        if cur:
            parts.append(cur)
        return parts

    def _resolve_cli(self) -> list:
        """解析 openclaw CLI 可执行文件，按顺序：显式配置 > PATH > npx 缓存直接 bin > npx。"""
        if self._cli_bin and os.path.exists(self._cli_bin):
            return [self._cli_bin]
        on_path = shutil.which("openclaw")
        if on_path:
            return [on_path]
        direct = self._find_openclaw_bin()
        if direct:
            return [direct]
        return ["npx", "openclaw"]

    @staticmethod
    def _find_openclaw_bin():
        """在 npx 缓存中查找 openclaw 直接可执行文件，避免每次 npx 冷启动开销。"""
        base = os.path.expanduser(os.path.join("~", ".npm", "_npx"))
        if not os.path.isdir(base):
            return None
        for pattern in (
            os.path.join(base, "*", "node_modules", ".bin", "openclaw"),
            os.path.join(base, "*", "node_modules", "openclaw", "bin", "openclaw.js"),
        ):
            for candidate in glob.glob(pattern):
                if os.access(candidate, os.X_OK):
                    return candidate
        return None

    # ------------------------------------------------------------------
    # gateway 兜底
    # ------------------------------------------------------------------
    def _gateway_reachable(self) -> bool:
        """通过 TCP 连接快速判断本地 gateway 是否在线。"""
        try:
            with socket.create_connection(
                (_OPENCLAW_GATEWAY_HOST, self._gateway_port),
                timeout=_OPENCLAW_GATEWAY_PROBE_TIMEOUT,
            ):
                return True
        except OSError:
            return False

    def _launchctl_gateway_restart(self) -> bool:
        """gateway 由 launchd 托管时，用 ``launchctl kickstart -k`` 安全重启。

        2026-08-31 事故后新增的**首选**路径：本机 gateway 装成了 LaunchAgent
        （``com.openclaw.gateway``，KeepAlive）。此时再用
        ``gateway run --force`` 会与 KeepAlive 已拉起的实例抢 18789 端口 →
        ``EADDRINUSE`` → 计为 unclean boot → 3 次/5 分钟打满 crash-loop
        breaker → 通道被禁自启，自愈反而把通道彻底搞挂。

        返回 False 表示「未托管或重启失败」，调用方应回退到 ``run --force``。
        """
        label = os.environ.get(
            "OPENCLAW_GATEWAY_LAUNCHD_LABEL", "com.openclaw.gateway"
        )
        try:
            probe = subprocess.run(
                ["launchctl", "list", label],
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
            return False  # 非 macOS / 无 launchctl
        if probe.returncode != 0:
            return False  # 未托管：交由调用方回退
        try:
            proc = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{os.geteuid()}/{label}"],
                capture_output=True,
                text=True,
                timeout=_OPENCLAW_GATEWAY_START_TIMEOUT,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:  # noqa: BLE001
            logger.warning("OPENCLAW_WECHAT: launchctl kickstart 异常: %s", exc)
            return False
        if proc.returncode == 0:
            logger.info(
                "OPENCLAW_WECHAT: 已通过 launchctl kickstart -k 重启 gateway（%s）",
                label,
            )
            return True
        logger.warning(
            "OPENCLAW_WECHAT: launchctl kickstart -k 失败(rc=%s): %s",
            proc.returncode,
            (proc.stderr or proc.stdout or "").strip()[:300],
        )
        return False

    def _restart_gateway(self) -> bool:
        """尝试重启本地 gateway。

        路径优先级（2026-08-31 调整）：

        1. **launchd 托管**：``launchctl kickstart -k`` —— 安全，不会抢端口。
        2. **未托管**：``gateway run --force`` 后台拉起（kill 占用端口的残留监听
           后启动），能立即返回，把启动等待预算留给 gateway 真正就绪。
        3. **兜底**：``gateway restart`` / ``gateway start``（仅对已安装服务有意义）。
        """
        cli = self._cli
        if not cli:
            return False
        # 1) 首选：launchd 托管场景（避免 EADDRINUSE → crash-loop breaker）。
        if self._launchctl_gateway_restart():
            return True
        # 2) 未托管场景：后台拉起 gateway。
        run_cmd = [*cli, "gateway", "run", "--force", "--bind", "loopback"]
        try:
            subprocess.Popen(
                run_cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
            logger.info("OPENCLAW_WECHAT: 已后台拉起 gateway（run --force）")
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("OPENCLAW_WECHAT: gateway run --force 启动失败: %s", exc)
        # 2) 兜底：系统服务方式（仅当服务已安装时有效）。
        last_err: str | None = None
        for sub in ("restart", "start"):
            try:
                proc = subprocess.run(
                    [*cli, "gateway", sub],
                    capture_output=True,
                    text=True,
                    timeout=_OPENCLAW_GATEWAY_START_TIMEOUT,
                )
                if proc.returncode == 0:
                    logger.info("OPENCLAW_WECHAT: gateway %s 成功", sub)
                    return True
                last_err = (proc.stderr or proc.stdout or "").strip()
                logger.warning(
                    "OPENCLAW_WECHAT: gateway %s 失败(rc=%s): %s",
                    sub,
                    proc.returncode,
                    last_err,
                )
            except subprocess.TimeoutExpired:
                logger.warning("OPENCLAW_WECHAT: gateway %s 超时", sub)
            except Exception as exc:  # noqa: BLE001
                logger.warning("OPENCLAW_WECHAT: gateway %s 异常: %s", sub, exc)
        if last_err:
            logger.error("OPENCLAW_WECHAT: 所有 gateway 重启尝试均失败: %s", last_err)
        return False

    def _wait_for_gateway_ready(self, timeout=None) -> bool:
        """轮询等待本地 gateway 就绪（TCP 可连通）。"""
        deadline = time.time() + (timeout or _OPENCLAW_GATEWAY_MAX_STARTUP_WAIT)
        while time.time() < deadline:
            time.sleep(_OPENCLAW_GATEWAY_POLL_INTERVAL)
            if self._gateway_reachable():
                logger.info("OPENCLAW_WECHAT: gateway 已恢复")
                return True
        logger.error("OPENCLAW_WECHAT: 重启后 gateway 仍未就绪（>%ss）",
                     timeout or _OPENCLAW_GATEWAY_MAX_STARTUP_WAIT)
        return False

    def _ensure_gateway(self) -> bool:
        """确保 gateway 在线；若不在线则尝试重启一次并等待就绪。"""
        if self._gateway_reachable():
            return True
        now = time.time()
        if now - self._last_restart_attempt < _OPENCLAW_GATEWAY_RESTART_COOLDOWN:
            logger.warning("OPENCLAW_WECHAT: gateway 未运行且处于重启冷却期，跳过")
            return False
        type(self)._last_restart_attempt = now
        logger.warning("OPENCLAW_WECHAT: gateway 未运行，尝试重启兜底")
        if not self._restart_gateway():
            return False
        return self._wait_for_gateway_ready()

    # ------------------------------------------------------------------
    # iLink contextToken 自愈
    # ------------------------------------------------------------------
    def _context_tokens_path(self) -> str | None:
        """返回当前 bot 账号对应的 iLink context-tokens.json 路径；无账号时返回 None。"""
        if not self._account:
            return None
        env_dir = os.environ.get("OPENCLAW_CONTEXT_TOKENS_DIR")
        if env_dir:
            base = env_dir
        else:
            base = os.path.expanduser(
                os.path.join("~", ".openclaw", "openclaw-weixin", "accounts")
            )
        return os.path.join(base, f"{self._account}.context-tokens.json")

    def context_token_mtime(self) -> float | None:
        """返回 iLink context-tokens.json 的 mtime（秒）；无文件/不可读返回 None。

        该文件在「接收人给 bot 发消息重建对话上下文」时被重写，因此 mtime 可近似作为
        「上下文最近一次建立 / 刷新」的锚点，用于估算距离过期还有多久。
        """
        path = self._context_tokens_path()
        if not path:
            return None
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    def should_send_context_keepalive(self) -> tuple[bool, str]:
        """判断是否在 iLink contextToken 到期前发送「请发消息续期」提醒。

        OpenClaw 不暴露真实过期时间，这里用「context-tokens.json 的 mtime + 预估有效期」
        估算。通过环境变量微调（默认值按经验设定，建议按实际观察调整）：

        - ``OPENCLAW_CONTEXT_TTL_SECONDS``：预估有效期，默认 72h。
        - ``OPENCLAW_CONTEXT_REMIND_MARGIN_SECONDS``：提前量，默认 12h。

        返回 ``(是否应发送, 原因)``。仅当上下文已存在、且 ``age >= ttl - margin`` 时返回 True。
        实际的发送 / 限频由调用方（main.py 健康巡检）负责。
        """
        try:
            ttl = float(os.environ.get("OPENCLAW_CONTEXT_TTL_SECONDS", 72 * 3600))
            margin = float(os.environ.get("OPENCLAW_CONTEXT_REMIND_MARGIN_SECONDS", 12 * 3600))
        except (TypeError, ValueError):
            ttl, margin = 72 * 3600, 12 * 3600
        mt = self.context_token_mtime()
        if mt is None:
            return False, "no_context_token"
        age = time.time() - mt
        if age < max(0.0, ttl - margin):
            return False, "context_not_near_expiry"
        return True, "context_near_expiry"

    def _clear_stale_context_tokens(self, *, force: bool = False) -> bool:
        """删除过期的 iLink context-tokens.json，强制下次收消息时重建对话上下文。

        这是 ``prepare failed``（contextToken 失效）的根因自愈：重扫重置 iLink
        session 后旧 token 失效，bot 主动推送前必须回传有效 token。

        安全护栏：
        - 仅删除「当前 bot 账号」对应的 token 文件，不动其他账号 / 配置。
        - ``force=False`` 时：仅当文件 mtime 早于最近一次失败的确认时刻
          （即确实是旧 token）才删除，避免误删用户刚发消息重建的新 token；
          并受冷却期限制，避免巡检反复清理。
        - ``force=True`` 用于「刚检测到发送失败」的发送链路，此时 token 必然失效。
        """
        path = self._context_tokens_path()
        if not path or not os.path.exists(path):
            return False
        if not force:
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            # 文件比“已知 prepare failed 时刻”更新 → 是用户刚重建的 token，跳过。
            if self._last_context_failure_ts > 0 and mtime >= self._last_context_failure_ts:
                logger.debug("OPENCLAW_WECHAT: context token 已更新，跳过清理")
                return False
            if (time.time() - self._last_context_clear_ts) < _OPENCLAW_CONTEXT_TOKEN_COOLDOWN:
                logger.debug("OPENCLAW_WECHAT: context token 清理处于冷却期，跳过")
                return False
        try:
            os.remove(path)
            type(self)._last_context_clear_ts = time.time()
            logger.warning("OPENCLAW_WECHAT: 已删除过期 context-tokens.json: %s", path)
            return True
        except OSError as exc:  # noqa: BLE001
            logger.warning("OPENCLAW_WECHAT: 删除 context-tokens.json 失败: %s", exc)
            return False

    def _note_context_failure(self) -> None:
        """记录最近一次确认到的 contextToken 失效（prepare failed）时刻。"""
        if (
            self.last_error_type == OPENCLAW_ERR_AUTH_NEEDED
            and "prepare failed" in (self.last_error_detail or "").lower()
        ):
            type(self)._last_context_failure_ts = time.time()

    def _run_cli(self, extra_args: list, timeout_seconds) -> bool:
        cmd = [
            *self._cli,
            "message",
            "send",
            "--channel",
            "openclaw-weixin",
            "--account",
            self._account,
            "--target",
            self._target,
            *extra_args,
            "--json",
        ]
        run_timeout = max(int(timeout_seconds or 0), _OPENCLAW_CLI_MIN_TIMEOUT_SECONDS)
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=run_timeout,
            )
        except subprocess.TimeoutExpired:
            self.last_error_type = OPENCLAW_ERR_TIMEOUT
            self.last_error_detail = f"CLI 调用超时（>{run_timeout}s）"
            logger.error("OPENCLAW_WECHAT: CLI 调用超时（>%ss）", run_timeout)
            return False
        except Exception as exc:  # noqa: BLE001
            self.last_error_type = OPENCLAW_ERR_OTHER
            self.last_error_detail = f"CLI 调用异常: {exc}"[:300]
            logger.error("OPENCLAW_WECHAT: CLI 调用异常: %s", exc)
            return False

        if proc.returncode != 0:
            stderr_text = (proc.stderr or "").strip()
            self.last_error_type = classify_openclaw_send_error(
                returncode=proc.returncode,
                stderr=stderr_text,
                stdout=proc.stdout or "",
            )
            self.last_error_detail = stderr_text[:300]
            self._note_context_failure()
            logger.error(
                "OPENCLAW_WECHAT: CLI 返回码 %s (类型=%s): %s",
                proc.returncode,
                self.last_error_type,
                stderr_text,
            )
            return False

        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # 不带 --json 或输出非 JSON：以回显中是否包含 sent 为准。
            sent = "sent" in out.lower()
            self.last_error_type = (
                OPENCLAW_ERR_OK if sent else classify_openclaw_send_error(
                    returncode=0, stderr=out, stdout=out
                )
            )
            self.last_error_detail = "" if sent else out[:300]
            return sent
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        candidates = [data.get("deliveryStatus"), payload.get("deliveryStatus")]
        for outcome in payload.get("payloadOutcomes", []) or []:
            if isinstance(outcome, dict):
                candidates.append(outcome.get("status"))
        status = next((str(c).lower() for c in candidates if c), "")
        if status == "sent":
            self.last_error_type = OPENCLAW_ERR_OK
            self.last_error_detail = ""
            return True
        # 投递状态非 sent —— 可能是 prepare failed / 配额 / 风控 等持久性问题，
        # 调用分类器识别根因，避免上层盲目重试。
        error_text = json.dumps(data, ensure_ascii=False)[:300]
        self.last_error_type = classify_openclaw_send_error(
            returncode=0, stderr=error_text, stdout=error_text
        )
        self.last_error_detail = f"投递状态={status}, payload={error_text}"
        self._note_context_failure()
        logger.warning(
            "OPENCLAW_WECHAT: 投递状态=%s (类型=%s), payload=%s",
            status, self.last_error_type, data,
        )
        return False

    # ------------------------------------------------------------------
    # 自动修复（gateway / iLink bot 自愈）
    # ------------------------------------------------------------------
    def _bot_healthy(self, timeout_seconds: int = 10) -> bool | None:
        """探测 iLink bot（openclaw-weixin 账号）是否在线。

        返回 ``True``=在线, ``False``=未在线/需重新登录, ``None``=无法判定
        （避免误杀重试，交由发送结果兜底）。
        """
        cli = self._cli
        if not cli:
            return None
        try:
            proc = subprocess.run(
                [*cli, "channels", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        accounts = (data.get("channelAccounts") or {}).get("openclaw-weixin") or []
        if not accounts:
            return None
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            if acc.get("lastError"):
                return False
            if acc.get("running") is True:
                return True
        # 无 running=True 也无错误：视为未就绪
        return False

    def _crash_loop_suppressed(self, timeout_seconds: int = 10) -> bool | None:
        """探测 gateway 是否因 crash-loop breaker 抑制了通道。

        gateway 进程可能仍在监听端口（TCP 可达），但 ``channels status`` 里账号
        的 ``lastError`` 含 ``crash loop breaker tripped`` / ``unclean boot`` 等
        关键字：此时通道被强制暂停，消息进不来。该状态**不会被** ``_bot_healthy``
        之外的常规分支恢复，必须强制重启 gateway 来清除 breaker。

        返回 ``True``=确认被抑制，``False``=未抑制，``None``=无法判定（CLI/解析失败）。
        """
        cli = self._cli
        if not cli:
            return None
        try:
            proc = subprocess.run(
                [*cli, "channels", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        accounts = (data.get("channelAccounts") or {}).get("openclaw-weixin") or []
        if not accounts:
            return None
        haystack = []
        for acc in accounts:
            if isinstance(acc, dict) and acc.get("lastError"):
                haystack.append(str(acc["lastError"]).lower())
        if not haystack:
            return False
        joined = "\n".join(haystack)
        return any(pat in joined for pat in _OPENCLAW_CRASH_LOOP_PATTERNS)

    def _repair_service(
        self, *, force_clear_token: bool = True, restart_gateway: bool = True
    ) -> dict:
        """根据最近一次发送失败语义自动修复 gateway / iLink contextToken。

        - gateway 失活：拉起并等待就绪（发送链路可能立即重试）。
        - 端口可达但 iLink 长连接可能已掉（锁屏/休眠）：``restart_gateway=True``
          时强制重启以恢复连接（与历史行为一致）。
        - ``prepare failed``（contextToken 失效）：删除过期 ``context-tokens.json``，
          标记 ``needs_user_message`` 等待接收人发消息重建对话上下文。此情形 bot 单向
          推送前必须等用户发消息，**无法自动完成**。
          **不重启 gateway**（2026-08-31 修正）：token 只能由用户发消息重建，重启
          治不了，反而冲断 iLink 会话并累积 unclean boot → crash-loop breaker。

        不会改动 openclaw 配置（避免 ``doctor --repair`` 旋转 token / 启用插件等副作用）。
        ``force_clear_token`` 仅发送链路在刚检测到失败时应为 ``True``；巡检场景用
        ``False`` 以走 mtime / 冷却护栏，避免误删用户刚重建的 token。
        ``restart_gateway`` 控制“端口可达时是否仍强制重启”；巡检场景传 ``False``
        避免每轮探活都重启一个健康的 gateway。

        返回诊断信息 dict（含 ``gateway_restarted`` / ``context_token_cleared`` /
        ``needs_user_message`` / ``bot_healthy``）。
        """
        info: dict = {
            "gateway_restarted": False,
            "context_token_cleared": False,
            "needs_user_message": False,
            "crash_loop_recovered": False,
            "bot_healthy": None,
        }
        # contextToken 失效（prepare failed）：清掉过期 token 并重启 gateway 加载空 context。
        is_context_issue = (
            self.last_error_type == OPENCLAW_ERR_AUTH_NEEDED
            and "prepare failed" in (self.last_error_detail or "").lower()
        )
        # crash-loop breaker 抑制：gateway 可能仍监听端口（TCP 可达），但通道被强制
        # 暂停（lastError 含 "crash loop breaker tripped" / "unclean boot"）。此时必须
        # 强制重启 gateway 来清除 breaker，否则消息永远进不来。受冷却期限制，避免
        # 巡检每轮都重启（breaker 本身有 5 分钟观察窗口，过频反而被再次抑制）。
        is_crash_loop = bool(self._crash_loop_suppressed())
        if is_crash_loop and (
            (time.time() - self._last_crash_loop_restart_ts)
            >= _OPENCLAW_CRASH_LOOP_RESTART_COOLDOWN
        ):
            logger.warning(
                "OPENCLAW_WECHAT: 检测到 gateway crash-loop breaker 抑制通道，强制重启以清除"
            )
            if self._restart_gateway():
                info["gateway_restarted"] = True
                info["crash_loop_recovered"] = True
                type(self)._last_crash_loop_restart_ts = time.time()
                self._wait_for_gateway_ready()
            else:
                logger.error("OPENCLAW_WECHAT: crash-loop 重启 gateway 失败")
        elif is_context_issue:
            # ⚠️ 2026-09-01 实锤修正（推翻 10:36 的误判）：
            # prepare failed 两种根因：
            #  (a) iLink 长连临时掉线 → channels.start 能重建，发送方重试即可成功；
            #  (b) contextToken 彻底 missing（iLink 服务端缓存清除，约 12h 后）→
            #      channels.start 返回 started:true 但 iLink 真实日志 contextToken missing，
            #      CLI 还可能假返回 sent（**10:36 那次 sent=True 就是假成功，微信根本没收到**），
            #      bot 侧任何操作都救不回，只能由用户在微信给 bot 发消息重建对话上下文。
            # repair 时无法区分 (a)/(b)，故先清废 token + channels.start，交由发送方重试；
            # 若发送方重试仍失败，由 send 方法标记 needs_user_message 兜底（见下）。
            # 优化：若上次已确认 needs_user_message（用户尚未发消息重建），跳过 channels.start
            # 空转，直接提示用户发消息，避免每条报警都重启通道刷日志。
            if self.last_needs_user_message:
                info["needs_user_message"] = True
                return info
            if self._clear_stale_context_tokens(force=force_clear_token):
                info["context_token_cleared"] = True
            if self.ensure_channel_started():
                info["channel_started"] = True
                # 通道重建后不在此标记 needs_user_message，交由发送方重试判断 (a)/(b)。
            else:
                logger.error(
                    "OPENCLAW_WECHAT: channels.start 重建 iLink 失败，需接收人给 bot 发消息重建"
                )
                info["needs_user_message"] = True
        elif not self._gateway_reachable():
            # 1) gateway 兜底：不在线则拉起并等待就绪（`_ensure_gateway` 自带冷却）。
            if self._ensure_gateway():
                info["gateway_restarted"] = True
        elif restart_gateway:
            # 2) 端口可达但 iLink 长连可能已掉（锁屏/休眠）：强制重启恢复连接。
            #    同样受冷却期约束 —— 原本无冷却，任何一次发送失败都会重启 gateway。
            now = time.time()
            if now - self._last_restart_attempt < _OPENCLAW_GATEWAY_RESTART_COOLDOWN:
                logger.info(
                    "OPENCLAW_WECHAT: gateway 重启处于冷却期(%.0fs)，跳过本轮强制重启",
                    _OPENCLAW_GATEWAY_RESTART_COOLDOWN,
                )
            elif self._restart_gateway():
                type(self)._last_restart_attempt = now
                info["gateway_restarted"] = True
                self._wait_for_gateway_ready()
        # 探测 iLink bot 是否在线（若需重新扫码登录则无法自动修复）。
        info["bot_healthy"] = self._bot_healthy()
        self.last_repair_info = info
        return info

    # ------------------------------------------------------------------
    # 主动探活（定时任务 / 健康检查入口）
    # ------------------------------------------------------------------
    def check_bot_health(
        self, *, restart_gateway_if_down: bool = False, auto_heal: bool = False
    ) -> dict:
        """返回 openclaw 微信通道的健康快照，供调度层定期探活 / 自愈。

        字段：
        - ``enabled``：是否配置账号/接收人。
        - ``account``：配置的 bot 账号。
        - ``gateway_reachable``：gateway 端口是否可达。
        - ``bot_healthy``：``True`` 在线 / ``False`` 需重新登录 / ``None`` 无法判定。
        - ``needs_relogin``：bot 已知需要重新扫码登录时为 ``True``。
        - ``needs_user_message``：contextToken 失效已自动重置，需用户给 bot 发消息重建上下文。
        - ``repaired``：本次是否执行了自动修复（拉起 gateway / 清 token）。
        - ``context_token_cleared``：本次是否删除了过期 context-tokens.json。
        - ``last_error``：bot 账号 ``lastError`` 文本（若存在）。
        - ``last_send_error_type``：最近一次发送失败的语义类型（``OPENCLAW_ERR_*``）。
        - ``last_send_error_detail``：最近一次发送失败的原始文本（截断）。
        - ``crash_loop_suppressed``：gateway 因 crash-loop breaker 暂停了通道（即使端口可达）。
        - ``crash_loop_recovered``：本次巡检是否因 crash-loop 强制重启了 gateway。
        - ``detail``：组合的可读描述，便于日志打印。

        默认仅探测不修复；``restart_gateway_if_down=True`` 时 gateway 不可达会尝试拉起
        （与发送时行为一致，避免静默轮询导致反复重启）。
        ``auto_heal=True`` 时还会根据最近一次发送失败语义执行自愈（gateway 拉起 /
        contextToken 失效自动清 token + 重启 gateway / crash-loop breaker 强制重启），
        并回填上述修复字段。
        """
        info: dict = {
            "enabled": self._enabled,
            "account": self._account,
            "gateway_reachable": False,
            "bot_healthy": None,
            "needs_relogin": False,
            "needs_user_message": False,
            "repaired": False,
            "context_token_cleared": False,
            "crash_loop_suppressed": False,
            "crash_loop_recovered": False,
            "last_error": None,
            "last_send_error_type": self.last_error_type,
            "last_send_error_detail": self.last_error_detail,
            "detail": "",
        }
        if not self._enabled:
            info["detail"] = "未配置账号/接收人，跳过"
            return info
        repair = None
        # 无论 auto_heal 与否，都先探测 crash-loop breaker 抑制（端口可能仍可达）。
        crash_loop = self._crash_loop_suppressed()
        if crash_loop is True:
            info["crash_loop_suppressed"] = True
        if auto_heal:
            # 根据最近一次失败语义自愈（gateway 拉起 / contextToken 清 token + 重启 /
            # crash-loop breaker 强制重启）。巡检场景传 restart_gateway=False：gateway
            # 在线且不处于 crash-loop 时不反复重启，仅在其失活或 contextToken 失效时重启。
            repair = self._repair_service(force_clear_token=False, restart_gateway=False)
        elif restart_gateway_if_down and not self._gateway_reachable():
            self._ensure_gateway()
        info["gateway_reachable"] = self._gateway_reachable()
        bot_state = self._bot_healthy()
        info["bot_healthy"] = bot_state
        # 进一步从 channels status 抓取 lastError 文案，辅助日志/告警定位。
        try:
            proc = subprocess.run(
                [*self._cli, "channels", "status", "--json"],
                capture_output=True, text=True, timeout=10,
            )
            if proc.returncode == 0 and (proc.stdout or "").strip():
                data = json.loads(proc.stdout)
                accounts = (data.get("channelAccounts") or {}).get("openclaw-weixin") or []
                for acc in accounts:
                    if not isinstance(acc, dict):
                        continue
                    if acc.get("id") == self._account and acc.get("lastError"):
                        info["last_error"] = acc.get("lastError")
                        break
        except Exception:  # noqa: BLE001
            pass
        if repair is not None:
            info["repaired"] = bool(
                repair.get("gateway_restarted") or repair.get("context_token_cleared")
            )
            info["context_token_cleared"] = bool(repair.get("context_token_cleared"))
            info["needs_user_message"] = bool(repair.get("needs_user_message"))
            info["crash_loop_recovered"] = bool(repair.get("crash_loop_recovered"))
        if info["needs_user_message"]:
            # contextToken 失效是“对话上下文丢失”，不是授权过期，只需用户发消息。
            info["needs_relogin"] = False
        elif info["crash_loop_suppressed"] or info["crash_loop_recovered"]:
            # crash-loop breaker 抑制是 gateway 稳定性问题，已（或正在）自动重启恢复，
            # 不是授权过期，不应误报需重扫登录。
            info["needs_relogin"] = False
        elif bot_state is False or info.get("last_error"):
            info["needs_relogin"] = True
        info["detail"] = (
            f"account={info['account']} gateway={'up' if info['gateway_reachable'] else 'down'} "
            f"bot={'ok' if bot_state is True else ('relogin' if bot_state is False else 'unknown')} "
            f"repaired={info['repaired']} needs_user_msg={info['needs_user_message']} "
            f"crash_loop_suppressed={info['crash_loop_suppressed']} "
            f"crash_loop_recovered={info['crash_loop_recovered']} "
            f"last_error={info.get('last_error') or '-'}"
        )
        return info

    def channel_running(self) -> bool | None:
        """返回微信通道是否 ``running``；无法判定（CLI 失败/未配置）返回 ``None``。

        注意：gateway 端口可达 ≠ 通道在跑。crash-loop breaker 跳闸时端口正常，
        但通道被禁止自动启动，此时 ``running=false``、推送静默失败。
        """
        if not self._cli or not self._enabled:
            return None
        try:
            proc = subprocess.run(
                [*self._cli, "channels", "status", "--json"],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except Exception:  # noqa: BLE001
            return None
        if proc.returncode != 0 or not (proc.stdout or "").strip():
            return None
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return None
        accounts = (data.get("channelAccounts") or {}).get("openclaw-weixin") or []
        for acc in accounts:
            if not isinstance(acc, dict):
                continue
            if self._account and acc.get("accountId") not in (None, self._account):
                continue
            if "running" in acc:
                return bool(acc["running"])
        return None

    def ensure_channel_started(self) -> bool:
        """通道未运行时用 RPC ``channels.start`` 拉起（breaker 抑制时的可靠兜底）。

        CLI **没有** ``channels start`` 子命令，必须走 ``gateway call``。
        相比重启 gateway，这种方式不会制造 unclean boot，也不会触发 breaker。
        """
        if not self._cli or not self._enabled:
            return False
        try:
            proc = subprocess.run(
                [
                    *self._cli,
                    "gateway",
                    "call",
                    "channels.start",
                    "--params",
                    '{"channel":"openclaw-weixin"}',
                    "--json",
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("OPENCLAW_WECHAT: channels.start 调用异常: %s", exc)
            return False
        if proc.returncode != 0:
            logger.warning(
                "OPENCLAW_WECHAT: channels.start 失败(rc=%s): %s",
                proc.returncode,
                (proc.stderr or proc.stdout or "").strip()[:300],
            )
            return False
        logger.info("OPENCLAW_WECHAT: 已通过 RPC channels.start 拉起微信通道")
        # iLink 长连重建需要短暂稳定期，等待几秒再让发送方重试，避免首条必败。
        time.sleep(3.0)
        return True

    def _try_send_image(self, image_bytes, timeout_seconds) -> bool:
        """单次尝试发送图片（不含自动修复）。"""
        if not self._ensure_gateway():
            return False
        try:
            with tempfile.NamedTemporaryFile(
                prefix="dsa_openclaw_", suffix=".png", delete=False
            ) as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name
        except Exception as exc:  # noqa: BLE001
            logger.error("OPENCLAW_WECHAT: 临时图片写入失败: %s", exc)
            return False
        try:
            return self._run_cli(["--media", tmp_path], timeout_seconds)
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------
    def send_to_openclaw_wechat(
        self,
        content,
        *,
        timeout_seconds=DEFAULT_SEND_TIMEOUT_SECONDS,
        auto_repair: bool = True,
    ) -> bool:
        """发送文本消息到微信。

        首次发送失败后，若 ``auto_repair`` 为真，会根据失败语义自动修复后重试：
        - gateway 失活：拉起 gateway 后重试。
        - iLink contextToken 失效（``prepare failed``）：iLink 长连掉线所致，自动清掉
          过期 token 后走 ``channels.start`` 重建 iLink 会话（**无需用户发消息**，
          2026-09-01 实测验证），随后立即重试推送。仅当 channels.start 也失败时才会
          标记 ``needs_user_message`` 兜底。
        """
        if not self._enabled:
            logger.warning("OPENCLAW_WECHAT: 未配置账号/接收人，跳过推送")
            return False
        if not self._ensure_gateway():
            return False
        text = (content or "").strip()
        if not text:
            return False
        # 超长内容按字节上限切片，分多条发送（不截断）；仅多条时每条加 [i/N] 序号。
        if len(text.encode("utf-8")) <= OPENCLAW_WECHAT_MAX_BYTES:
            parts = [text]
        else:
            parts = self._split_text_by_bytes(text, OPENCLAW_WECHAT_MAX_BYTES - 24)
        n = len(parts)
        if self._send_text_parts(parts, n, timeout_seconds):
            self.last_repair_info = None
            self.last_needs_user_message = False
            return True
        if not auto_repair:
            return False
        logger.warning(
            "OPENCLAW_WECHAT: 文本推送首次失败，尝试自动修复后重试"
        )
        repair = self._repair_service()
        self.last_repair_info = repair
        if repair and repair.get("needs_user_message"):
            # contextToken 已失效：清掉过期 token + 重启后仍需用户发消息才能恢复，
            # 盲目重试必然失败，直接返回并给出明确提示。
            self.last_needs_user_message = True
            logger.warning(
                "OPENCLAW_WECHAT: 检测到 iLink contextToken 失效，已自动清掉过期 token；"
                "请给 bot 发一条任意消息以重建对话上下文，之后即可正常推送。"
            )
            self._register_retry(content)
            return False
        # channels.start 已尝试重建 iLink：再重试一次。若仍失败，说明 contextToken 已
        # 彻底 missing（iLink 服务端缓存清除，实锤 2026-09-01：channels.start 返回
        # started:true 但 iLink 真实日志 contextToken missing，CLI 还可能假返回 sent），
        # bot 侧任何操作都救不回，只能由用户在微信给 bot 发消息重建 → 标记 needs_user_message。
        ok = self._send_text_parts(parts, n, timeout_seconds)
        if not ok:
            self.last_needs_user_message = True
            logger.warning(
                "OPENCLAW_WECHAT: channels.start 后仍 prepare failed（contextToken 彻底 missing），"
                "已自动清掉过期 token；请给 bot 发一条任意消息以重建对话上下文。"
            )
            self._register_retry(content)
        return ok

    def _register_retry(self, content) -> None:
        """决策仪表盘微信推送失败（contextToken 失效/通道未恢复）时，登记已生成内容。

        由后台 daemon 线程每 30 分钟重试一次，直到通道恢复（用户给 bot 发消息重建上下文后）
        推送成功。直接重推首次生成好的文本，不重新计算选股/分析结果。
        """
        _OPENCLAW_WECHAT_RETRY.register(self, content)

    def _send_text_parts(self, parts, n, timeout_seconds) -> bool:
        """按切片顺序发送多条文本；全部成功返回 True，任一失败返回 False（不回滚已发部分）。"""
        for i, part in enumerate(parts):
            prefix = f"[{i + 1}/{n}] " if n > 1 else ""
            if not self._run_cli(["--message", prefix + part], timeout_seconds):
                return False
        return True

    def _send_openclaw_wechat_image(
        self, image_bytes, *, timeout_seconds=DEFAULT_SEND_TIMEOUT_SECONDS, auto_repair: bool = True
    ) -> bool:
        """发送图片消息到微信（via --media）。

        首次发送失败后，若 ``auto_repair`` 为真，会根据失败语义自动修复后重试；
        若判定为 iLink contextToken 失效，不再盲目重试，而是提示用户给 bot 发消息
        重建对话上下文（详见 ``send_to_openclaw_wechat``）。
        """
        if not self._enabled:
            logger.warning("OPENCLAW_WECHAT: 未配置账号/接收人，跳过图片推送")
            return False
        if not image_bytes:
            return False
        if self._try_send_image(image_bytes, timeout_seconds):
            self.last_repair_info = None
            self.last_needs_user_message = False
            return True
        if not auto_repair:
            return False
        logger.warning(
            "OPENCLAW_WECHAT: 图片推送首次失败，尝试自动修复后重试"
        )
        repair = self._repair_service()
        self.last_repair_info = repair
        if repair and repair.get("needs_user_message"):
            self.last_needs_user_message = True
            logger.warning(
                "OPENCLAW_WECHAT: 检测到 iLink contextToken 失效，已自动清掉过期 token；"
                "请给 bot 发一条任意消息以重建对话上下文，之后即可正常推送。"
            )
            return False
        # 同 send_to_openclaw_wechat：重试仍失败 ⇒ contextToken 彻底 missing，需用户发消息。
        ok = self._try_send_image(image_bytes, timeout_seconds)
        if not ok:
            self.last_needs_user_message = True
            logger.warning(
                "OPENCLAW_WECHAT: channels.start 后仍 prepare failed（contextToken 彻底 missing），"
                "请给 bot 发一条任意消息以重建对话上下文。"
            )
        return ok


# ----------------------------------------------------------------------
# 决策仪表盘微信推送失败后的轮询重试调度器
# ----------------------------------------------------------------------
class _OpenClawWechatRetryScheduler:
    """微信(OpenClaw)通道推送失败后，周期性重试「已生成」的决策仪表盘内容。

    - 不重新计算：直接重推首次生成好的文本（send_to_openclaw_wechat 内部会按发送上限截断）。
    - 每 30 分钟轮询一次，直到通道恢复（用户给 bot 发消息重建上下文后）推送成功。
    - 仅保留当天待重推内容；跨天的旧内容自动丢弃（次日新分析会重新生成并推送）。
    """

    def __init__(self, interval_seconds: int = _OPENCLAW_WECHAT_RETRY_INTERVAL_SECONDS):
        self._interval = interval_seconds
        self._lock = threading.Lock()
        self._pending: Dict[str, str] = {}
        self._thread: Optional[threading.Thread] = None

    def register(self, sender, content: str) -> None:
        content = (content or "").strip()
        if not content:
            return
        today_key = datetime.now().strftime("%Y-%m-%d")
        with self._lock:
            self._pending[today_key] = content
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(
                    target=self._run, args=(sender,), daemon=True,
                    name="openclaw-wechat-retry",
                )
                self._thread.start()

    def _run(self, sender) -> None:
        while True:
            # 首轮也等待一个间隔再重试，符合「每半小时轮询一次」语义（不立即重发）。
            time.sleep(self._interval)
            with self._lock:
                today_key = datetime.now().strftime("%Y-%m-%d")
                # 丢弃非今天的待重推（跨天旧内容不再推送）
                for old_key in [k for k in self._pending if k != today_key]:
                    self._pending.pop(old_key, None)
                if not self._pending:
                    self._thread = None
                    return
                items = dict(self._pending)
            for key, content in list(items.items()):
                try:
                    ok = sender.send_to_openclaw_wechat(content)
                except Exception as exc:  # 防御：重试过程异常不终止线程
                    logger.warning("OPENCLAW_WECHAT 重试异常: %s", exc)
                    ok = False
                if ok:
                    with self._lock:
                        self._pending.pop(key, None)
                    logger.info("OPENCLAW_WECHAT 通道已恢复，决策仪表盘重推成功 (%s)", key)
                else:
                    logger.info(
                        "OPENCLAW_WECHAT 通道仍未恢复，%d 分钟后重试决策仪表盘推送 (%s)",
                        self._interval // 60, key,
                    )
            with self._lock:
                if not self._pending:
                    self._thread = None
                    return


_OPENCLAW_WECHAT_RETRY = _OpenClawWechatRetryScheduler()
