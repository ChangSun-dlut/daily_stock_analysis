#!/usr/bin/env python3
"""
重新安装 alphasift 包后，重新应用单元格序列化补丁。

运行时 pipeline 调用的是安装包 alphasift.daily 中的 enrich_daily_features，
而非项目 src/services/screening/daily.py 的派生版本。该补丁在
``alphasift/daily.py`` 的 ``result.at`` 写入处添加 ``_serialize_cell_value``
包装，避免 pandas 3.x 在写入非标量值时抛出 "Must have equal len keys and
value when setting with an iterable"。

执行方式：
    python scripts/patch_alphasift_daily.py

启动时（server.py）会自动检测补丁是否存在，若缺失会提示运行此脚本。
"""

import os
import sys


def _find_alphasift_daily() -> str | None:
    """查找 alphasift.daily 在 site-packages 中的路径。"""
    try:
        import alphasift.daily as mod
    except ImportError:
        print("[patch] ERROR: alphasift package is not installed")
        return None
    path = getattr(mod, "__file__", None)
    if path and os.path.isfile(path):
        return path
    return None


def _patch_file(path: str) -> bool:
    """对 alphasift/daily.py 的 .at 写入处添加序列化包装。"""
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # 检查是否已打补丁
    if "_serialize_cell_value" in content:
        print(f"[patch] alphasift.daily already patched at {path}")
        return True

    # 添加 numpy 导入
    content = content.replace(
        "import json\nimport os",
        "import json\nimport numpy as np\nimport os",
    )

    # 添加序列化函数
    serialization_helpers = """

def _to_jsonable(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, np.ndarray):
        return [_to_jsonable(v) for v in value.tolist()]
    if isinstance(value, np.ma.MaskedArray):
        return [_to_jsonable(v) for v in value.tolist()]
    if isinstance(value, (pd.Timestamp, pd.Timedelta)):
        return str(value)
    if isinstance(value, pd.Series):
        return _to_jsonable(value.tolist())
    if isinstance(value, pd.DataFrame):
        return _to_jsonable(value.to_dict(orient="records"))
    if isinstance(value, pd.Index):
        return [_to_jsonable(v) for v in value.tolist()]
    if isinstance(value, pd.Categorical):
        return [_to_jsonable(v) for v in value.categories.tolist()]
    return value


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    return str(value)


def _serialize_cell_value(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (str, bytes, bool)):
        return value
    if isinstance(value, (int, float)):
        return value
    try:
        return json.dumps(
            _to_jsonable(value), ensure_ascii=False, default=_json_default
        )
    except (TypeError, ValueError):
        return str(value)


"""

    content = content.replace(
        "from alphasift.source_guard import call_with_timeout, parse_source_timeout_seconds\n\n",
        "from alphasift.source_guard import call_with_timeout, parse_source_timeout_seconds\n"
        + serialization_helpers,
    )

    # 包装 .at 写入
    content = content.replace(
        "for key, value in features.items():\n            result.at[idx, key] = value",
        "for key, value in features.items():\n"
        "            try:\n"
        "                result.at[idx, key] = _serialize_cell_value(value)\n"
        "            except (ValueError, TypeError) as exc:\n"
        "                _msg = str(exc)\n"
        '                if "equal len keys" in _msg or "setting with an iterable" in _msg:\n'
        "                    result.at[idx, key] = json.dumps(\n"
        "                        _to_jsonable(value), ensure_ascii=False, default=_json_default\n"
        "                    )\n"
        "                else:\n"
        "                    raise",
    )

    with open(path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"[patch] SUCCESS: alphasift.daily patched at {path}")
    return True


def main():
    path = _find_alphasift_daily()
    if path is None:
        sys.exit(1)
    _patch_file(path)


if __name__ == "__main__":
    main()