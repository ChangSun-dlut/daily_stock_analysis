#!/usr/bin/env python3
"""
重新安装 alphasift 包后，重新应用「重排 token 预算」补丁。

背景
----
运行时 pipeline 调用的是安装包 ``alphasift.ranker``（非项目派生版本
``src/services/screening/ranker.py``）。当主模型是推理型模型（如
``anthropic/MiniMax-M2.7``）时，输出预算绝大部分被 thinking token 占用：

    实测 15 只候选的重排请求，max_tokens=2048：
      completion_tokens=1677, reasoning_tokens=1577 -> 正文只剩约 100 token

prompt 再大一点就会 ``finish_reason=length`` 且正文为空，ranker 判定
``empty_response``、覆盖率 0.00，最终回退到本地因子评分（前端提示
「LLM 重排失败：fell back to screen_score」）。

litellm 对该模型不支持 ``thinking={"type": "disabled"}``
（``UnsupportedParamsError``），因此无法关闭思考，只能放大输出预算：
实测同一请求 ``max_tokens=8192`` 可正常返回完整 JSON（正文 1042 字符）。

补丁做的事
----------
在 ``_call_llm`` 中：
1. 抬高输出预算下限到 8192（``max_tokens`` 只是上限，不增加实际消耗）；
2. 若仍 ``finish_reason == "length"`` 或正文为空，按 3 倍递增重试，
   最多 3 次、上限 16384，而不是把空内容直接交给解析器。

执行方式：
    python scripts/patch_alphasift_ranker.py

启动检查见 ``server.py``（与 alphasift.daily 序列化补丁同款检测）。
"""

from __future__ import annotations

import os
import sys

MARKER = "_DSA_RANKER_TOKEN_BUDGET_PATCH"
MARKER_V2 = "_DSA_RANKER_TOKEN_BUDGET_PATCH_V2"

OLD_BLOCK = '''            try:
                response = litellm.completion(**kwargs)
                content = response.choices[0].message.content or ""
                finish = getattr(response.choices[0], "finish_reason", None) or ""
                if finish == "length":
                    _log_truncated_response(
                        candidate_model=candidate_model, content=content,
                        max_tokens=max_tokens, response=response,
                    )
                return content
'''

# v1 补丁：只做 2.5x 重试，未抬高起始预算（从 2250 起步可能仍不够）
OLD_BLOCK_V1 = '''            # {MARKER} (scripts/patch_alphasift_ranker.py)
            # 推理型模型（anthropic/MiniMax-M2.7 等）会把输出预算大量消耗在
            # thinking token 上，导致 finish_reason=length 且正文为空，
            # 最终被判定 empty_response 并回退 screen_score。
            # 这里在截断或空正文时放大 max_tokens 重试，最多 2 次。
            _dsa_attempts = [int(max_tokens) if max_tokens else 2048]
            _dsa_budget = _dsa_attempts[0]
            while _dsa_budget < 16384 and len(_dsa_attempts) < 3:
                _dsa_budget = int(_dsa_budget * 2.5)
                _dsa_attempts.append(min(_dsa_budget, 16384))
            try:
                content = ""
                finish = ""
                for _dsa_tokens in _dsa_attempts:
                    kwargs["max_tokens"] = _dsa_tokens
                    response = litellm.completion(**kwargs)
                    content = response.choices[0].message.content or ""
                    finish = getattr(response.choices[0], "finish_reason", None) or ""
                    if finish == "length":
                        _log_truncated_response(
                            candidate_model=candidate_model, content=content,
                            max_tokens=_dsa_tokens, response=response,
                        )
                    if finish != "length" and content.strip():
                        if _dsa_tokens != _dsa_attempts[0]:
                            logger.info(
                                "LLM ranking recovered with max_tokens=%d "
                                "(model=%s, content_len=%d)",
                                _dsa_tokens, candidate_model, len(content),
                            )
                        return content
                    logger.warning(
                        "LLM ranking response unusable (model=%s, finish=%s, "
                        "content_len=%d) with max_tokens=%d",
                        candidate_model, finish, len(content), _dsa_tokens,
                    )
                return content
'''

NEW_BLOCK = '''            # {MARKER} {MARKER_V2} (scripts/patch_alphasift_ranker.py)
            # 推理型模型（anthropic/MiniMax-M2.7 等）会把输出预算大量消耗在
            # thinking token 上，导致 finish_reason=length 且正文为空，
            # 最终被判定 empty_response 并回退 screen_score。
            # max_tokens 只是输出上限（不增加实际消耗），因此抬高下限是安全的；
            # 若仍截断或空正文，再按 3 倍递增重试，最多 3 次、上限 16384。
            _dsa_base = int(max_tokens) if max_tokens else 2048
            _dsa_floor = 8192
            _dsa_attempts = [max(_dsa_base, _dsa_floor)]
            while _dsa_attempts[-1] < 16384 and len(_dsa_attempts) < 3:
                _dsa_attempts.append(min(int(_dsa_attempts[-1] * 3), 16384))
            try:
                content = ""
                finish = ""
                for _dsa_tokens in _dsa_attempts:
                    kwargs["max_tokens"] = _dsa_tokens
                    response = litellm.completion(**kwargs)
                    content = response.choices[0].message.content or ""
                    finish = getattr(response.choices[0], "finish_reason", None) or ""
                    if finish == "length":
                        _log_truncated_response(
                            candidate_model=candidate_model, content=content,
                            max_tokens=_dsa_tokens, response=response,
                        )
                    if finish != "length" and content.strip():
                        if _dsa_tokens != _dsa_attempts[0]:
                            logger.info(
                                "LLM ranking recovered with max_tokens=%d "
                                "(model=%s, content_len=%d)",
                                _dsa_tokens, candidate_model, len(content),
                            )
                        return content
                    logger.warning(
                        "LLM ranking response unusable (model=%s, finish=%s, "
                        "content_len=%d) with max_tokens=%d",
                        candidate_model, finish, len(content), _dsa_tokens,
                    )
                return content'''


# 预算抬高后，推理型模型生成耗时变长（实测 8192 预算约 60~90s），
# 默认 60s 会超时。这里给重排请求设置超时下限 150s。
TIMEOUT_MARKER = "_DSA_RANKER_TIMEOUT_PATCH"
OLD_TIMEOUT = '            kwargs["timeout"] = timeout_sec\n'
NEW_TIMEOUT = (
    '            # {MARKER}\n'
    "            kwargs[\"timeout\"] = max(float(timeout_sec or 0.0), 150.0)\n"
)


def _find_alphasift_ranker() -> str | None:
    try:
        import alphasift.ranker as mod
    except ImportError:
        print("[patch] ERROR: alphasift package is not installed")
        return None
    path = getattr(mod, "__file__", None)
    if path and os.path.isfile(path):
        return path
    return None


def _patch_file(path: str) -> bool:
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    if MARKER_V2 in content:
        print("[patch] token-budget patch (v2) already present")
    else:
        new_block = (
            NEW_BLOCK.replace("{MARKER}", MARKER).replace("{MARKER_V2}", MARKER_V2)
        )
        if OLD_BLOCK in content:
            content = content.replace(OLD_BLOCK, new_block + "\n", 1)
            print("[patch] applied token-budget patch (v2)")
        elif OLD_BLOCK_V1.replace("{MARKER}", MARKER) in content:
            # v1 -> v2 升级：整段替换
            content = content.replace(
                OLD_BLOCK_V1.replace("{MARKER}", MARKER), new_block + "\n", 1
            )
            print("[patch] upgraded token-budget patch v1 -> v2")
        else:
            print(
                "[patch] ERROR: target block not found — alphasift version may have "
                f"changed; inspect _call_llm() manually at {path}"
            )
            return False

    # 超时下限补丁（独立、幂等）
    if TIMEOUT_MARKER not in content:
        if OLD_TIMEOUT not in content:
            print("[patch] WARN: timeout assignment not found; skipping timeout patch")
        else:
            content = content.replace(
                OLD_TIMEOUT, NEW_TIMEOUT.replace("{MARKER}", TIMEOUT_MARKER), 1
            )
            print("[patch] applied ranking timeout floor patch (150s)")

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    print(f"[patch] applied token-budget patch (v2) to {path}")
    return True


def main() -> int:
    path = _find_alphasift_ranker()
    if not path:
        return 1
    return 0 if _patch_file(path) else 1


if __name__ == "__main__":
    sys.exit(main())
