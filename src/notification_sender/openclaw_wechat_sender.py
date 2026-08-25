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
_OPENCLAW_CLI_MIN_TIMEOUT_SECONDS = 60

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
            logger.error("OPENCLAW_WECHAT: CLI 调用超时（>%ss）", run_timeout)
            return False
        except Exception as exc:  # noqa: BLE001
            logger.error("OPENCLAW_WECHAT: CLI 调用异常: %s", exc)
            return False

        if proc.returncode != 0:
            logger.error(
                "OPENCLAW_WECHAT: CLI 返回码 %s: %s",
                proc.returncode,
                (proc.stderr or "").strip(),
            )
            return False

        out = (proc.stdout or "").strip()
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            # 不带 --json 或输出非 JSON：以回显中是否包含 sent 为准。
            return "sent" in out.lower()
        payload = data.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        candidates = [data.get("deliveryStatus"), payload.get("deliveryStatus")]
        for outcome in payload.get("payloadOutcomes", []) or []:
            if isinstance(outcome, dict):
                candidates.append(outcome.get("status"))
        status = next((str(c).lower() for c in candidates if c), "")
        if status == "sent":
            return True
        logger.warning("OPENCLAW_WECHAT: 投递状态=%s, payload=%s", status, data)
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

    def _repair_service(self) -> dict:
        """自动修复 openclaw gateway / iLink bot：强制重启 gateway 后探测 bot 状态。

        锁屏 / 休眠后最常见的是 gateway 进程失活，强制重启是最可靠的恢复手段，
        且不会改动 openclaw 配置（避免 ``doctor --repair`` 旋转 token / 启用插件等副作用）。
        若重启后仍失败，再探测 iLink bot 是否需要重新扫码登录，用于给出精确提示。

        返回诊断信息 dict（供调用方生成更精确的错误提示）。
        """
        info: dict = {"gateway_restarted": False, "bot_healthy": None}
        # 强制重启 gateway（绕过冷却），并等待就绪。
        if self._restart_gateway():
            info["gateway_restarted"] = True
            self._wait_for_gateway_ready()
        # 探测 iLink bot 是否在线（若需重新扫码登录则无法自动修复）。
        info["bot_healthy"] = self._bot_healthy()
        self.last_repair_info = info
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

        首次发送失败后，若 ``auto_repair`` 为真，会自动修复 openclaw gateway /
        iLink bot（强制重启 gateway 并探测 bot 在线状态）后重试一次，以应对锁屏、
        休眠等导致的服务失活。
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
            return True
        if not auto_repair:
            return False
        logger.warning(
            "OPENCLAW_WECHAT: 文本推送首次失败，尝试自动修复 gateway / iLink bot 后重试"
        )
        self._repair_service()
        return self._run_cli(args, timeout_seconds)

    def _send_openclaw_wechat_image(
        self, image_bytes, *, timeout_seconds=DEFAULT_SEND_TIMEOUT_SECONDS, auto_repair: bool = True
    ) -> bool:
        """发送图片消息到微信（via --media）。

        首次发送失败后，若 ``auto_repair`` 为真，会自动修复 openclaw gateway /
        iLink bot（强制重启 gateway 并探测 bot 在线状态）后重试一次，以应对锁屏、
        休眠等导致的服务失活。
        """
        if not self._enabled:
            logger.warning("OPENCLAW_WECHAT: 未配置账号/接收人，跳过图片推送")
            return False
        if not image_bytes:
            return False
        if self._try_send_image(image_bytes, timeout_seconds):
            self.last_repair_info = None
            return True
        if not auto_repair:
            return False
        logger.warning(
            "OPENCLAW_WECHAT: 图片推送首次失败，尝试自动修复 gateway / iLink bot 后重试"
        )
        self._repair_service()
        return self._try_send_image(image_bytes, timeout_seconds)
