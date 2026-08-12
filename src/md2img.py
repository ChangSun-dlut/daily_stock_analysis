# -*- coding: utf-8 -*-
"""
===================================
Markdown 转图片工具模块
===================================

将 Markdown 转为 PNG 图片（用于不支持 Markdown 的通知渠道）。
支持 wkhtmltoimage (imgkit) 与 markdown-to-file (m2f)，后者对 emoji 支持更好 (Issue #455)。

Security note: imgkit passes HTML to wkhtmltoimage via stdin, not argv, so
command injection from content is not applicable. Output is rasterized to PNG
(no script execution). Input is from system-generated reports, not raw user
input. Risk is considered low for the current use case.
"""

import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from src.share_image import (
    ShareImageBranding,
    build_share_image_html,
    share_image_branding_from_config,
)

logger = logging.getLogger(__name__)

# Cache m2f (markdown-to-file) health status so we don't keep retrying a
# broken engine on every API call.  The bundled Puppeteer browser can crash
# (e.g. SEGV on macOS ARM) while the subprocess returns exit 0, making the
# failure silent but reproducible across requests.
#
# To avoid locking out the engine for the entire process lifetime after a
# single transient failure (e.g. CDN unreachable, m2f transient error), we
# track the time of the last negative verdict and force a re-probe after
# ``_M2F_HEALTH_TTL_SECONDS``.
_m2f_healthy: Optional[bool] = None
_m2f_unhealthy_since: Optional[float] = None
_M2F_HEALTH_TTL_SECONDS = 60.0


def warmup_m2f() -> None:
    """Pre-warm m2f's bundled puppeteer Chromium on server startup.

    On macOS ARM the first launch of puppeteer-core Chromium almost always
    exits with rc=0 without producing a file (silent crash).  Running a
    trivial one-shot conversion once at boot absorbs this cold-start failure
    so that subsequent real requests succeed on the first attempt.
    """
    global _m2f_healthy, _m2f_unhealthy_since

    m2f_bin = shutil.which("m2f")
    if m2f_bin is None:
        return

    # Restore "unknown" health so the warm-up call is not blocked by a
    # stale negative TTL leftover from a previous process incarnation.
    _m2f_healthy = None
    _m2f_unhealthy_since = None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp(prefix="m2f_warmup_")
        md_path = os.path.join(temp_dir, "warmup.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write("# warmup\n")

        for attempt in range(3):
            result = subprocess.run(
                [m2f_bin, md_path, "png", f"outputDirectory={temp_dir}"],
                capture_output=True,
                timeout=30,
                check=False,
            )
            png_path = os.path.join(temp_dir, "warmup.png")
            if result.returncode == 0 and os.path.isfile(png_path):
                _m2f_healthy = True
                _m2f_unhealthy_since = None
                logger.info("m2f warmup succeeded (attempt %d)", attempt + 1)
                return
            if attempt < 2:
                logger.debug("m2f warmup attempt %d/3 (cold-start), retrying…", attempt + 1)
                shutil.rmtree(temp_dir, ignore_errors=True)
                temp_dir = tempfile.mkdtemp(prefix="m2f_warmup_")
                md_path = os.path.join(temp_dir, "warmup.md")
                with open(md_path, "w", encoding="utf-8") as f:
                    f.write("# warmup\n")
                time.sleep(1)
                continue
            logger.warning("m2f warmup failed after 3 attempts (m2f may be unavailable)")

    except Exception as exc:
        logger.debug("m2f warmup skipped (%s)", exc)
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def _share_image_branding(config: object) -> ShareImageBranding:
    return share_image_branding_from_config(config)


def _resolve_playwright_command() -> Optional[str]:
    """Resolve a global or repository-local Playwright CLI executable."""
    command = shutil.which("playwright")
    if command:
        return command

    repository_root = Path(__file__).resolve().parent.parent
    executable_name = "playwright.cmd" if os.name == "nt" else "playwright"
    local_command = repository_root / "apps" / "dsa-web" / "node_modules" / ".bin" / executable_name
    if local_command.is_file():
        return str(local_command)
    return None


def _markdown_to_image_playwright(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert a share-poster HTML document to PNG with Playwright Chromium."""
    playwright_command = _resolve_playwright_command()
    if playwright_command is None:
        logger.warning(
            "Playwright CLI not found. Install Web dependencies with: "
            "cd apps/dsa-web && npm ci. Fallback to text."
        )
        return None

    temp_dir = tempfile.mkdtemp()
    html_path = Path(temp_dir) / "report.html"
    png_path = Path(temp_dir) / "report.png"
    try:
        html_path.write_text(
            build_share_image_html(
                markdown_text,
                structured_payload=structured_payload,
                branding=branding,
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                playwright_command,
                "screenshot",
                "--browser",
                "chromium",
                "--viewport-size",
                "1080,720",
                "--full-page",
                html_path.resolve().as_uri(),
                str(png_path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
        if result.returncode == 0 and png_path.exists():
            return png_path.read_bytes()

        stderr = result.stderr.decode("utf-8", errors="replace")
        logger.error("Playwright image conversion failed: %s", stderr[:500])
        return None
    except Exception as exc:
        logger.error("Playwright image conversion error: %s", exc)
        return None
    finally:
        try:
            shutil.rmtree(temp_dir)
        except Exception as exc:
            logger.debug("Failed to remove temp dir %s: %s", temp_dir, exc)


def _markdown_to_image_m2f(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert Markdown to PNG via markdown-to-file (m2f) CLI. Better emoji support (Issue #455)."""
    global _m2f_healthy, _m2f_unhealthy_since
    # Honour the cached negative verdict only within the TTL window; after
    # that, force a re-probe so transient failures don't lock out the engine.
    if _m2f_healthy is False and _m2f_unhealthy_since is not None:
        if (time.monotonic() - _m2f_unhealthy_since) < _M2F_HEALTH_TTL_SECONDS:
            return None
        logger.info("markdown_to_image m2f TTL expired, re-probing")
        _m2f_healthy = None
        _m2f_unhealthy_since = None

    m2f_command = shutil.which("m2f")
    if m2f_command is None:
        logger.warning(
            "m2f (markdown-to-file) not found in PATH. "
            "Install with: npm i -g markdown-to-file. Fallback to text."
        )
        _m2f_healthy = False
        return None

    temp_dir = None
    try:
        # m2f's bundled puppeteer-core Chromium can crash on first cold-start
        # (especially macOS ARM).  Retry up to 2 extra times before giving up.
        for attempt in range(3):
            temp_dir = tempfile.mkdtemp()
            md_path = os.path.join(temp_dir, "report.md")
            with open(md_path, "w", encoding="utf-8") as f:
                f.write(
                    build_share_image_html(
                        markdown_text,
                        structured_payload=structured_payload,
                        branding=branding,
                    )
                )

            result = subprocess.run(
                [m2f_command, md_path, "png", f"outputDirectory={temp_dir}"],
                capture_output=True,
                timeout=60,
                check=False,
            )
            png_path = os.path.join(temp_dir, "report.png")
            if result.returncode == 0 and os.path.isfile(png_path):
                _m2f_healthy = True
                _m2f_unhealthy_since = None
                with open(png_path, "rb") as f:
                    data = f.read()
                shutil.rmtree(temp_dir, ignore_errors=True)
                return data

            if attempt < 2:
                stderr_tail = (result.stderr or b"").decode("utf-8", errors="replace")[:200]
                logger.info(
                    "m2f attempt %d/3 failed (rc=%d, stderr=%s), retrying...",
                    attempt + 1,
                    result.returncode,
                    stderr_tail,
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                time.sleep(2)  # give puppeteer's Chromium a chance to settle
                continue

            # 3 attempts all failed — mark unhealthy
            logger.info(
                "m2f conversion skipped (3 attempts exhausted): "
                "returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            _m2f_healthy = False
            _m2f_unhealthy_since = time.monotonic()
            return None
    except subprocess.TimeoutExpired:
        logger.warning("m2f conversion timed out (60s×3)")
        _m2f_healthy = False
        _m2f_unhealthy_since = time.monotonic()
        return None
    except Exception as e:
        logger.warning("markdown_to_image (m2f) failed: %s", e)
        _m2f_healthy = False
        _m2f_unhealthy_since = time.monotonic()
        return None


def _markdown_to_image_wkhtml(
    markdown_text: str,
    structured_payload: Optional[Mapping[str, Any]] = None,
    branding: Optional[ShareImageBranding] = None,
) -> Optional[bytes]:
    """Convert Markdown to PNG via imgkit/wkhtmltoimage."""
    try:
        import imgkit
    except ImportError:
        logger.debug("imgkit not installed, markdown_to_image unavailable")
        return None

    try:
        html = build_share_image_html(
            markdown_text,
            structured_payload=structured_payload,
            branding=branding,
        )
        options = {
            "format": "png",
            "encoding": "UTF-8",
            "width": 1080,
            "disable-smart-width": "",
            "quality": 95,
            "quiet": "",
        }
        out = imgkit.from_string(html, False, options=options)
        if out and isinstance(out, bytes) and len(out) > 0:
            return out
        logger.warning("imgkit.from_string returned empty or invalid result")
        return None
    except OSError as e:
        if "wkhtmltoimage" in str(e).lower() or "wkhtmltopdf" in str(e).lower():
            logger.debug("wkhtmltopdf/wkhtmltoimage not found: %s", e)
        else:
            logger.warning("imgkit/wkhtmltoimage error: %s", e)
        return None
    except Exception as e:
        logger.warning("markdown_to_image conversion failed: %s", e)
        return None


def _engine_callers() -> Mapping[str, Any]:
    return {
        "wkhtmltoimage": _markdown_to_image_wkhtml,
        "markdown-to-file": _markdown_to_image_m2f,
        "playwright": _markdown_to_image_playwright,
    }


def _html_to_image_m2f(html_content: str) -> Optional[bytes]:
    """Convert pre-built HTML to PNG via m2f CLI.

    Variant of :func:`_markdown_to_image_m2f` for callers (e.g. the batch
    share poster) that already have deterministic HTML and don't need the
    ``build_share_image_html`` step.
    """
    global _m2f_healthy, _m2f_unhealthy_since
    if _m2f_healthy is False and _m2f_unhealthy_since is not None:
        if (time.monotonic() - _m2f_unhealthy_since) < _M2F_HEALTH_TTL_SECONDS:
            return None
        logger.info("html_to_image m2f TTL expired, re-probing")
        _m2f_healthy = None
        _m2f_unhealthy_since = None

    m2f_command = shutil.which("m2f")
    if m2f_command is None:
        logger.warning(
            "m2f (markdown-to-file) not found in PATH. "
            "Install with: npm i -g markdown-to-file. Fallback to text."
        )
        _m2f_healthy = False
        _m2f_unhealthy_since = time.monotonic()
        return None

    temp_dir = None
    try:
        temp_dir = tempfile.mkdtemp()
        # m2f accepts HTML wrapped in a .md file (it preserves raw HTML).
        md_path = os.path.join(temp_dir, "report.md")
        with open(md_path, "w", encoding="utf-8") as f:
            f.write(html_content)

        result = subprocess.run(
            [m2f_command, md_path, "png", f"outputDirectory={temp_dir}"],
            capture_output=True,
            timeout=120,
            check=False,
        )
        png_path = os.path.join(temp_dir, "report.png")
        if result.returncode != 0 or not os.path.isfile(png_path):
            logger.info(
                "html_to_image m2f conversion skipped: returncode=%s, stderr=%s",
                result.returncode,
                (result.stderr or b"").decode("utf-8", errors="replace")[:200],
            )
            _m2f_healthy = False
            _m2f_unhealthy_since = time.monotonic()
            return None

        _m2f_healthy = True
        _m2f_unhealthy_since = None
        with open(png_path, "rb") as f:
            return f.read()
    except subprocess.TimeoutExpired:
        logger.warning("html_to_image m2f conversion timed out (120s)")
        _m2f_healthy = False
        _m2f_unhealthy_since = time.monotonic()
        return None
    except Exception as e:
        logger.warning("html_to_image (m2f) failed: %s", e)
        _m2f_healthy = False
        _m2f_unhealthy_since = time.monotonic()
        return None
    finally:
        if temp_dir:
            shutil.rmtree(temp_dir, ignore_errors=True)


def html_to_image(html_content: str) -> Optional[bytes]:
    """Convert pre-built HTML to PNG using the configured engine chain.

    Falls back through wkhtmltoimage → markdown-to-file → playwright, mirroring
    :func:`markdown_to_image`'s fallback behaviour. Used by the batch share
    poster so callers can reuse the deterministic HTML from
    :func:`build_batch_share_image_html` without going through Markdown.
    """
    if not html_content:
        return None

    try:
        from src.config import get_config

        config = get_config()
        branding = _share_image_branding(config)
    except Exception:
        branding = ShareImageBranding()

    # Engine 1: m2f (markdown-to-file) — the only engine that supports
    # arbitrary pre-built HTML reliably in this codebase.
    result = _html_to_image_m2f(html_content)
    if result is not None:
        return result

    # Fall back to the standard markdown pipeline for engines that need to
    # rebuild HTML from structured data. This is a safety net: it degrades to
    # the original single-poster look instead of failing outright, but the
    # preferred path is the direct HTML→m2f conversion above.
    logger.info("html_to_image falling back to markdown pipeline")
    return markdown_to_image("", structured_payload=None)


def markdown_to_image(
    markdown_text: str,
    max_chars: int = 15000,
    structured_payload: Optional[Mapping[str, Any]] = None,
) -> Optional[bytes]:
    """
    Convert Markdown to PNG image bytes.

    Engine is read from config.md2img_engine: wkhtmltoimage (default),
    markdown-to-file, or playwright. When the configured engine fails, the
    function automatically tries the remaining engines in priority order so
    callers can degrade gracefully without manual configuration changes.

    When conversion fails or dependencies unavailable, returns None so caller
    can fall back to text sending.

    Args:
        markdown_text: Raw Markdown content.
        max_chars: Skip conversion and return None if content exceeds this length
            (avoids huge images). Default 15000.
        structured_payload: Optional stock-analysis or market-review JSON. Exact
            structured fields take precedence over Markdown extraction.

    Returns:
        PNG bytes, or None if conversion fails or dependencies unavailable.
    """
    if len(markdown_text) > max_chars:
        logger.warning(
            "Markdown content (%d chars) exceeds max_chars (%d), skipping image conversion",
            len(markdown_text),
            max_chars,
        )
        return None

    try:
        from src.config import get_config

        config = get_config()
        preferred_engine = getattr(config, "md2img_engine", "wkhtmltoimage")
        branding = _share_image_branding(config)
    except Exception:
        preferred_engine = "wkhtmltoimage"
        branding = ShareImageBranding()

    callers = _engine_callers()
    engine_order = [preferred_engine]
    for engine in callers:
        if engine != preferred_engine and engine in callers:
            engine_order.append(engine)

    last_error: Optional[str] = None
    for engine in engine_order:
        caller = callers.get(engine)
        if caller is None:
            continue
        try:
            logger.debug("markdown_to_image trying engine: %s", engine)
            result = caller(markdown_text, structured_payload, branding)
            if result:
                logger.info("markdown_to_image succeeded with engine: %s", engine)
                return result
            last_error = f"engine {engine} returned empty result"
        except Exception as exc:
            last_error = f"engine {engine} raised {type(exc).__name__}: {exc}"
            logger.warning("markdown_to_image engine %s failed: %s", engine, exc, exc_info=True)

    logger.warning(
        "markdown_to_image failed for all engines (%s): %s",
        ", ".join(engine_order),
        last_error or "no engine available",
    )
    return None
