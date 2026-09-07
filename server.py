# -*- coding: utf-8 -*-
"""
===================================
Daily Stock Analysis - FastAPI 后端服务入口
===================================

职责：
1. 提供 RESTful API 服务
2. 配置 CORS 跨域支持
3. 健康检查接口
4. 托管前端静态文件（生产模式）

启动方式：
    uvicorn server:app --reload --host 0.0.0.0 --port 8000
    
    或使用 main.py:
    python main.py --serve-only      # 仅启动 API 服务
    python main.py --serve           # API 服务 + 执行分析
"""

import logging

from src.config import setup_env, get_config
from src.logging_config import setup_logging

# 初始化环境变量与日志
setup_env()

config = get_config()
level_name = (config.log_level or "INFO").upper()
level = getattr(logging, level_name, logging.INFO)

setup_logging(
    log_prefix="api_server",
    console_level=level,
    extra_quiet_loggers=['uvicorn', 'fastapi'],
)

# 启动检查：验证 alphasift.daily 已打单元格序列化补丁（避免 pandas 3.x .at 写入 iterable 报错）
# 运行时 pipeline 调用的是 alphasift 安装包内的 daily.py（非项目派生版本），
# 若安装包被重新安装导致补丁丢失，需手动重新执行修补脚本。
try:
    import alphasift.daily as _alphasift_daily
    if hasattr(_alphasift_daily, "_serialize_cell_value"):
        logging.getLogger("server").info(
            "[check] alphasift.daily cell serialization patch present (v3)"
        )
    else:
        logging.getLogger("server").warning(
            "[check] alphasift.daily MISSING cell serialization patch — "
            "run `python scripts/patch_alphasift_daily.py` to re-apply"
        )
except Exception:
    logging.getLogger("server").warning(
        "failed to check alphasift.daily serialization patch, continuing",
        exc_info=True,
    )

# 启动检查：验证 alphasift.ranker 已打「重排 token 预算」补丁。
# 推理型模型（如 anthropic/MiniMax-M2.7）会把输出预算大量花在 thinking token 上，
# 未打补丁时表现为 finish_reason=length + 空正文 -> empty_response -> 回退本地评分。
try:
    import alphasift.ranker as _alphasift_ranker
    with open(getattr(_alphasift_ranker, "__file__", ""), encoding="utf-8") as _fh:
        _ranker_src = _fh.read()
    if "_DSA_RANKER_TOKEN_BUDGET_PATCH_V2" in _ranker_src:
        logging.getLogger("server").info(
            "[check] alphasift.ranker token budget patch present"
        )
    else:
        logging.getLogger("server").warning(
            "[check] alphasift.ranker MISSING token budget patch — "
            "run `python scripts/patch_alphasift_ranker.py` to re-apply"
        )
except Exception:
    logging.getLogger("server").warning(
        "failed to check alphasift.ranker token budget patch, continuing",
        exc_info=True,
    )

# 从 api.app 导入应用实例
from api.app import app  # noqa: E402

# 导出 app 供 uvicorn 使用
__all__ = ['app']


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=8000,
        reload=True,
    )
