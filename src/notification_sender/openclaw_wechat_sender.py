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
import time

logger = logging.getLogger(__name__)

# 微信单条消息建议上限，超出自动截断。
OPENCLAW_WECHAT_MAX_BYTES = 4000
# 子进程调用默认超时（秒）；CLI（尤其 npx 冷启动）可能较慢，实际下限会拉高。
DEFAULT_SEND_TIMEOUT_SECONDS = 30
_OPENCLAW_CLI_MIN_TIMEOUT_SECONDS = 120

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
        # 同进程内上次重启尝试时间戳，用于冷却。
        self._last_restart_attempt = 0.0
        # 解析 CLI（缓存，避免每次发送都走 npx 冷启动）。
        self._cli = self._resolve_cli()
        # 最近一次自动修复的诊断信息（供调用方生成更精确的错误提示）。
        self.last_repair_info: dict | None = None
        # 最近一次发送的失败语义（``OPENCLAW_ERR_*`` 之一）。成功发送后会重置为 OK。
        self.last_error_type: str = OPENCLAW_ERR_NOT_CONFIGURED if not self._enabled else OPENCLAW_ERR_OK
        # 最近一次失败的原始文本（截断到 ~300 字符），供告警/日志展示。
        self.last_error_detail: str = ""
        # 最近一次自动清理 context token 的时间戳（用于冷却，避免反复删除）。
        self._last_context_clear_ts = 0.0
        # 最近一次确认到 contextToken 失效（prepare failed）的时间戳，用于判断
        # token 文件是否过期（文件 mtime < 该值 = 旧 token，可安全删除）。
        self._last_context_failure_ts = 0.0
        # 最近一次因 crash-loop breaker 强制重启 gateway 的时间戳，用于冷却，避免
        # 巡检每轮都重启（breaker 自身有 5 分钟观察窗口，过频反而被再次抑制）。
        self._last_crash_loop_restart_ts = 0.0
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

    def _restart_gateway(self) -> bool:
        """尝试重启本地 gateway。

        优先用 ``gateway run --force`` 后台拉起一个 gateway（kill 占用端口的残留
        监听后启动），该方式在「已安装为系统服务」与「未安装」两种场景下都有效，
        且能立即返回，把全部启动等待预算留给 gateway 真正就绪。
        仅当 ``run --force`` 无法拉起进程时，才回退到 ``gateway restart`` /
        ``gateway start``（仅对 installed 服务有意义）。
        """
        cli = self._cli
        if not cli:
            return False
        # 1) 通用首选：后台拉起 gateway。
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
        self._last_restart_attempt = now
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
            self._last_context_clear_ts = time.time()
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
            self._last_context_failure_ts = time.time()

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
        - ``prepare failed``（contextToken 失效）：删除过期 ``context-tokens.json``
          并重启 gateway，强制下次接收人发消息时重建对话上下文；此情形 bot 单向
          推送前必须等用户发消息，**无法自动完成**，故标记 ``needs_user_message``
          且不阻塞等待 gateway 就绪（发送本就会失败）。

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
                self._last_crash_loop_restart_ts = time.time()
                self._wait_for_gateway_ready()
            else:
                logger.error("OPENCLAW_WECHAT: crash-loop 重启 gateway 失败")
        elif is_context_issue:
            if self._clear_stale_context_tokens(force=force_clear_token):
                info["context_token_cleared"] = True
            # 清 token 后重启 gateway；发送本就会失败（需用户发消息），不阻塞等待就绪。
            if self._restart_gateway():
                info["gateway_restarted"] = True
            info["needs_user_message"] = True
        elif not self._gateway_reachable():
            # 1) gateway 兜底：不在线则拉起并等待就绪。
            if self._ensure_gateway():
                info["gateway_restarted"] = True
        elif restart_gateway:
            # 2) 端口可达但 iLink 长连可能已掉（锁屏/休眠）：强制重启恢复连接。
            if self._restart_gateway():
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
        - iLink contextToken 失效（``prepare failed``）：自动清掉过期 token 并重启
          gateway，但 bot 单向推送前必须等接收人发消息重建 context，无法自动完成，
          因此不再盲目重试，而是标记 ``needs_user_message`` 并提示用户。
        """
        if not self._enabled:
            logger.warning("OPENCLAW_WECHAT: 未配置账号/接收人，跳过推送")
            return False
        if not self._ensure_gateway():
            return False
        text = (content or "").strip()
        if not text:
            return False
        text = self._truncate(text, OPENCLAW_WECHAT_MAX_BYTES)
        args = ["--message", text]
        if self._run_cli(args, timeout_seconds):
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
                "OPENCLAW_WECHAT: 检测到 iLink contextToken 失效，已自动清掉过期 token 并重启 gateway；"
                "请给 bot 发一条任意消息以重建对话上下文，之后即可正常推送。"
            )
            return False
        return self._run_cli(args, timeout_seconds)

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
                "OPENCLAW_WECHAT: 检测到 iLink contextToken 失效，已自动清掉过期 token 并重启 gateway；"
                "请给 bot 发一条任意消息以重建对话上下文，之后即可正常推送。"
            )
            return False
        return self._try_send_image(image_bytes, timeout_seconds)
