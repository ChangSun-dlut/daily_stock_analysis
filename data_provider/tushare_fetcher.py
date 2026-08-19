# -*- coding: utf-8 -*-
"""
===================================
TushareFetcher - 备用数据源 1 (Priority 2)
===================================

数据来源：Tushare Pro API（挖地兔）
特点：需要 Token、有请求配额限制
优点：数据质量高、接口稳定

流控策略：
1. 实现"每分钟调用计数器"
2. 超过免费配额（80次/分）时，强制休眠到下一分钟
3. 使用 tenacity 实现指数退避重试
"""

import json as _json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any

import pandas as pd
import requests
from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential,
    retry_if_exception_type,
    before_sleep_log,
)

from .base import BaseFetcher, DataFetchError, RateLimitError, STANDARD_COLUMNS,is_bse_code, is_st_stock, is_kc_cy_stock, normalize_stock_code, _is_hk_market
from .realtime_types import UnifiedRealtimeQuote, ChipDistribution
from src.config import get_config
import os
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


# ETF code prefixes by exchange
# Shanghai: 51xxxx, 52xxxx, 56xxxx, 58xxxx
# Shenzhen: 15xxxx, 16xxxx, 18xxxx
_ETF_SH_PREFIXES = ('51', '52', '56', '58')
_ETF_SZ_PREFIXES = ('15', '16', '18')
_ETF_ALL_PREFIXES = _ETF_SH_PREFIXES + _ETF_SZ_PREFIXES


def _is_etf_code(stock_code: str) -> bool:
    """
    Check if the code is an ETF fund code.

    ETF code ranges:
    - Shanghai ETF: 51xxxx, 52xxxx, 56xxxx, 58xxxx
    - Shenzhen ETF: 15xxxx, 16xxxx, 18xxxx
    """
    code = normalize_stock_code(stock_code)
    return code.startswith(_ETF_ALL_PREFIXES) and len(code) == 6


def _is_us_code(stock_code: str) -> bool:
    """
    判断代码是否为美股
    
    美股代码规则：
    - 1-5个大写字母，如 'AAPL', 'TSLA'
    - 可能包含 '.'，如 'BRK.B'
    """
    code = stock_code.strip().upper()
    return bool(re.match(r'^[A-Z]{1,5}(\.[A-Z])?$', code))


def _resolve_tushare_http_url() -> Optional[str]:
    """读取 ``TUSHARE_HTTP_URL`` 环境变量并做基本校验。

    - 留空 / 仅空白 / 未设置 → 返回 ``None``，调用方继续走官方默认地址。
    - 设置则去掉首尾空白后返回，并校验必须是 ``http://`` 或 ``https://`` 前缀，
      避免有人误填成纯主机名（如 ``api.tushare.pro``）导致 ``requests`` 把它
      当成相对路径请求失败。
    """
    raw = os.getenv("TUSHARE_HTTP_URL")
    if not raw:
        return None
    url = raw.strip()
    if not url:
        return None
    if not (url.startswith("http://") or url.startswith("https://")):
        raise ValueError(
            "TUSHARE_HTTP_URL 必须以 http:// 或 https:// 开头，"
            f"当前值为 {url!r}"
        )
    return url


class _TushareHttpClient:
    """Lightweight Tushare Pro client that does not require the tushare SDK."""

    def __init__(self, token: str, timeout: int = 30, api_url: str = "http://api.tushare.pro") -> None:
        self._token = token
        self._timeout = timeout
        self._api_url = api_url

    def query(self, api_name: str, fields: str = "", **kwargs) -> pd.DataFrame:
        req_params = {
            "api_name": api_name,
            "token": self._token,
            "params": kwargs,
            "fields": fields,
        }
        res = requests.post(self._api_url, json=req_params, timeout=self._timeout)
        if res.status_code != 200:
            raise Exception(f"Tushare API HTTP {res.status_code}")

        result = _json.loads(res.text)
        if result.get("code") != 0:
            raise Exception(result.get("msg") or f"Tushare API error code {result.get('code')}")

        data = result.get("data") or {}
        columns = data.get("fields") or []
        items = data.get("items") or []
        return pd.DataFrame(items, columns=columns)

    def __getattr__(self, api_name: str):
        if api_name.startswith("_"):
            raise AttributeError(api_name)

        def caller(**kwargs) -> pd.DataFrame:
            return self.query(api_name, **kwargs)

        return caller


def _safe_pct_change(value: Any) -> float:
    """Safe float coercion for Tushare pct_change (returns 0.0 on failure)."""
    try:
        if value is None:
            return 0.0
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class TushareFetcher(BaseFetcher):
    """
    Tushare Pro 数据源实现
    
    优先级：2
    数据来源：Tushare Pro API
    
    关键策略：
    - 每分钟调用计数器，防止超出配额
    - 超过 80 次/分钟时强制等待
    - 失败后指数退避重试
    
    配额说明（Tushare 免费用户）：
    - 每分钟最多 80 次请求
    - 每天最多 500 次请求
    """
    
    name = "TushareFetcher"
    priority = int(os.getenv("TUSHARE_PRIORITY", "2"))  # 默认优先级，会在 __init__ 中根据配置动态调整

    def __init__(self, rate_limit_per_minute: int = 80):
        """
        初始化 TushareFetcher

        Args:
            rate_limit_per_minute: 每分钟最大请求数（默认80，Tushare免费配额）
        """
        self.rate_limit_per_minute = rate_limit_per_minute
        self._call_count = 0  # 当前分钟内的调用次数
        self._minute_start: Optional[float] = None  # 当前计数周期开始时间
        self._api: Optional[object] = None  # Tushare API 实例
        # A 股日 K 复权模式: none / qfq（getattr 兼容被 mock 的 config 对象）
        self._kline_adjust: str = getattr(get_config(), "tushare_kline_adjust", "qfq")
        self.date_list: Optional[List[str]] = None  # 交易日列表缓存（倒序，最新日期在前）
        self._date_list_end: Optional[str] = None  # 缓存对应的截止日期，用于跨日刷新
        self._pro_dead: bool = False      # Pro rt_k 接口不可用时置 True，跳过后续尝试
        self._legacy_dead: bool = False   # 旧版接口失败后置 True，跳过后续尝试

        # 尝试初始化 API
        self._init_api()

        # 根据 API 初始化结果动态调整优先级
        self.priority = self._determine_priority()
    
    def _init_api(self) -> None:
        """
        初始化 Tushare API

        如果 Token 未配置，此数据源将不可用。
        这里直接使用内置 HTTP client，避免运行时强依赖 tushare SDK，
        从而减少 Docker / PyInstaller / 多虚拟环境场景下因缺包导致的初始化失败。
        """
        config = get_config()

        if not config.tushare_token:
            logger.warning("Tushare Token 未配置，此数据源不可用")
            return

        try:
            self._api = self._build_api_client(config.tushare_token)
            logger.info("Tushare API 初始化成功")
        except Exception as e:
            logger.error(f"Tushare API 初始化失败: {e}")
            self._api = None

    def _build_api_client(self, token: str) -> _TushareHttpClient:
        """
        Build a lightweight Tushare Pro client over direct HTTP requests.

        The project already normalizes all Pro calls through the same request
        contract, so we do not need the official tushare SDK during runtime.

        支持通过 ``TUSHARE_HTTP_URL`` 环境变量将请求指向自建或第三方兼容
        端点，便于在网络无法直达 ``api.tushare.pro`` 时切换镜像/网关。
        留空或不设置则保持官方默认地址，行为与历史版本完全一致。
        """
        api_url = _resolve_tushare_http_url()
        if api_url:
            logger.info("Tushare 使用自定义接入地址: %s", api_url)
            client = _TushareHttpClient(token=token, api_url=api_url)
        else:
            client = _TushareHttpClient(token=token)
        logger.debug("Tushare API client configured for direct HTTP calls")
        return client

    def _determine_priority(self) -> int:
        """
        根据 Token 配置和 API 初始化状态确定优先级

        策略：
        - Token 配置且 API 初始化成功：优先级 -1（绝对最高，优于 efinance）
        - 其他情况：优先级 2（默认）

        Returns:
            优先级数字（0=最高，数字越大优先级越低）
        """
        config = get_config()

        if config.tushare_token and self._api is not None:
            # Token 配置且 API 初始化成功，提升为最高优先级
            logger.info("✅ 检测到 TUSHARE_TOKEN 且 API 初始化成功，Tushare 数据源优先级提升为最高 (Priority -1)")
            return -1

        # Token 未配置或 API 初始化失败，保持默认优先级
        return 2

    def is_available(self) -> bool:
        """
        检查数据源是否可用

        Returns:
            True 表示可用，False 表示不可用
        """
        return self._api is not None

    def _check_rate_limit(self) -> None:
        """
        检查并执行速率限制
        
        流控策略：
        1. 检查是否进入新的一分钟
        2. 如果是，重置计数器
        3. 如果当前分钟调用次数超过限制，强制休眠
        """
        current_time = time.time()
        
        # 检查是否需要重置计数器（新的一分钟）
        if self._minute_start is None:
            self._minute_start = current_time
            self._call_count = 0
        elif current_time - self._minute_start >= 60:
            # 已经过了一分钟，重置计数器
            self._minute_start = current_time
            self._call_count = 0
            logger.debug("速率限制计数器已重置")
        
        # 检查是否超过配额
        if self._call_count >= self.rate_limit_per_minute:
            # 计算需要等待的时间（到下一分钟）
            elapsed = current_time - self._minute_start
            sleep_time = max(0, 60 - elapsed) + 1  # +1 秒缓冲
            
            logger.warning(
                f"Tushare 达到速率限制 ({self._call_count}/{self.rate_limit_per_minute} 次/分钟)，"
                f"等待 {sleep_time:.1f} 秒..."
            )
            
            time.sleep(sleep_time)
            
            # 重置计数器
            self._minute_start = time.time()
            self._call_count = 0
        
        # 增加调用计数
        self._call_count += 1
        logger.debug(f"Tushare 当前分钟调用次数: {self._call_count}/{self.rate_limit_per_minute}")

    def _call_api_with_rate_limit(self, method_name: str, **kwargs) -> pd.DataFrame:
        """统一通过速率限制包装 Tushare API 调用。"""
        if self._api is None:
            raise DataFetchError("Tushare API 未初始化，请检查 Token 配置")

        self._check_rate_limit()
        method = getattr(self._api, method_name)
        return method(**kwargs)

    def _get_china_now(self) -> datetime:
        """返回上海时区当前时间，方便测试覆盖跨日刷新逻辑。"""
        return datetime.now(ZoneInfo("Asia/Shanghai"))

    def _get_trade_dates(self, end_date: Optional[str] = None) -> List[str]:
        """按自然日刷新交易日历缓存，避免服务跨日后继续复用旧日历。"""
        if self._api is None:
            return []

        china_now = self._get_china_now()
        requested_end_date = end_date or china_now.strftime("%Y%m%d")

        if self.date_list is not None and self._date_list_end == requested_end_date:
            return self.date_list

        start_date = (china_now - timedelta(days=20)).strftime("%Y%m%d")
        df_cal = self._call_api_with_rate_limit(
            "trade_cal",
            exchange="SSE",
            start_date=start_date,
            end_date=requested_end_date,
        )

        if df_cal is None or df_cal.empty or "cal_date" not in df_cal.columns:
            logger.warning("[Tushare] trade_cal 返回为空，无法更新交易日历缓存")
            self.date_list = []
            self._date_list_end = requested_end_date
            return self.date_list

        trade_dates = sorted(
            df_cal[df_cal["is_open"] == 1]["cal_date"].astype(str).tolist(),
            reverse=True,
        )
        self.date_list = trade_dates
        self._date_list_end = requested_end_date
        return trade_dates

    @staticmethod
    def _pick_trade_date(trade_dates: List[str], use_today: bool) -> Optional[str]:
        """根据可用交易日列表选择当天或前一交易日。"""
        if not trade_dates:
            return None
        if use_today or len(trade_dates) == 1:
            return trade_dates[0]
        return trade_dates[1]

    @staticmethod
    def _detect_exchange_hint(stock_code: str) -> Optional[str]:
        """Return SH/SZ/BJ when the raw user input carries an explicit exchange hint."""
        upper = (stock_code or "").strip().upper()
        if upper.startswith(("SH", "SS")) or upper.endswith((".SH", ".SS")):
            return "SH"
        if upper.startswith("SZ") or upper.endswith(".SZ"):
            return "SZ"
        if upper.startswith("BJ") or upper.endswith(".BJ"):
            return "BJ"
        return None

    @classmethod
    def _get_legacy_realtime_symbol(cls, stock_code: str) -> str:
        """Build the legacy tushare symbol while preserving explicit SH/SZ hints."""
        code = normalize_stock_code(stock_code)
        exchange_hint = cls._detect_exchange_hint(stock_code)

        if code == '000001' and exchange_hint == 'SH':
            return 'sh000001'
        if code == '399001':
            return 'sz399001'
        if code == '399006':
            return 'sz399006'
        if code == '000300':
            return 'sh000300'
        if is_bse_code(code):
            return f"bj{code}"
        return code
    
    def _convert_stock_code(self, stock_code: str) -> str:
        """
        转换 A 股 / ETF / 北交所等为 Tushare ts_code（不含港股逻辑）。

        Tushare 要求的格式示例：
        - 沪市股票：600519.SH
        - 深市股票：000001.SZ
        - 沪市 ETF：510050.SH
        - 深市 ETF：159919.SZ

        Args:
            stock_code: 原始代码，如 '600519', '000001', '563230'

        Returns:
            Tushare 格式代码，如 '600519.SH', '000001.SZ'
        """
        raw_code = stock_code.strip()
        
        # Already has suffix.
        if '.' in raw_code:
            upper = raw_code.upper()
            code = normalize_stock_code(raw_code)
            exchange_hint = self._detect_exchange_hint(raw_code)
            if exchange_hint in ("SH", "SZ", "BJ") and code.isdigit():
                return f"{code}.{exchange_hint}"

            ts_code = upper
            if ts_code.endswith('.SS'):
                return f"{ts_code[:-3]}.SH"
            return ts_code

        if _is_us_code(raw_code):
            raise DataFetchError(f"TushareFetcher 不支持美股 {raw_code}，请使用 AkshareFetcher 或 YfinanceFetcher")

        if _is_hk_market(raw_code):
            #raise DataFetchError(f"TushareFetcher 不支持港股 {raw_code}，请使用 AkshareFetcher")
            return normalize_stock_code(raw_code)

        code = normalize_stock_code(raw_code)
        exchange_hint = self._detect_exchange_hint(raw_code)

        if exchange_hint == "SH":
            return f"{code}.SH"
        if exchange_hint == "SZ":
            return f"{code}.SZ"
        if exchange_hint == "BJ":
            return f"{code}.BJ"

        # ETF: determine exchange by prefix
        if code.startswith(_ETF_SH_PREFIXES) and len(code) == 6:
            return f"{code}.SH"
        if code.startswith(_ETF_SZ_PREFIXES) and len(code) == 6:
            return f"{code}.SZ"
        
        # BSE (Beijing Stock Exchange): 8xxxxx, 4xxxxx, 920xxx
        if is_bse_code(code):
            return f"{code}.BJ"
        
        # Regular stocks
        # Shanghai: 600xxx, 601xxx, 603xxx, 605xxx, 688xxx (STAR Market)
        # Shenzhen: 000xxx, 001xxx, 002xxx, 003xxx, 300xxx, 301xxx (ChiNext)
        if code.startswith(('600', '601', '603', '605', '688')):
            return f"{code}.SH"
        elif code.startswith(('000', '001', '002', '003', '300', '301')):
            return f"{code}.SZ"
        else:
            logger.warning(f"无法确定股票 {code} 的市场，默认使用深市")
            return f"{code}.SZ"

    def _convert_hk_stock_code_for_tushare(self, stock_code: str) -> str:
        """
        将用户输入转为 Tushare Pro 接口所需的 ts_code（含港股 nnnnn.HK）。

        - 非港股：委托 _convert_stock_code（A 股 / ETF / 北交所等）。
        - 港股：从 HK00700、00700、00700.HK 等形式归一为 5 位数字 + .HK。
        """
        raw_code = stock_code.strip()
        if _is_hk_market(raw_code):
            if "." in raw_code:
                ts_code = raw_code.upper()
                if ts_code.endswith(".SS"):
                    return f"{ts_code[:-3]}.SH"
                if ts_code.endswith(".HK"):
                    return ts_code
            digits = re.sub(r"\D", "", raw_code)
            if not digits:
                raise DataFetchError(f"无法识别港股代码 {raw_code}")
            code = digits[-5:].rjust(5, "0")
            return f"{code}.HK"
        return self._convert_stock_code(stock_code)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((ConnectionError, TimeoutError)),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    def _fetch_raw_data(self, stock_code: str, start_date: str, end_date: str) -> pd.DataFrame:
        """
        从 Tushare 获取原始数据
        
        根据代码类型选择不同接口：
        - 普通股票：daily()
        - ETF 基金：fund_daily()
        
        流程：
        1. 检查 API 是否可用
        2. 检查是否为美股（不支持）
        3. 执行速率限制检查
        4. 转换股票代码格式
        5. 根据代码类型选择接口并调用
        """
        if self._api is None:
            raise DataFetchError("Tushare API 未初始化，请检查 Token 配置")
        
        # US stocks not supported
        if _is_us_code(stock_code):
            raise DataFetchError(f"TushareFetcher 不支持美股 {stock_code}，请使用 AkshareFetcher 或 YfinanceFetcher")
        
        # Rate-limit check
        self._check_rate_limit()
        
        is_hk = _is_hk_market(stock_code)
         # 判断是否为 ETF / 港股，以选择不同接口
        is_etf = _is_etf_code(stock_code)
        if is_hk:
            ts_code = self._convert_hk_stock_code_for_tushare(stock_code)
            api_name = "hk_daily"
        else:
            ts_code = self._convert_stock_code(stock_code)
            api_name = "fund_daily" if is_etf else "daily"
        
        # Convert date format (Tushare requires YYYYMMDD)
        ts_start = start_date.replace('-', '')
        ts_end = end_date.replace('-', '')
        
       

        logger.debug(f"调用 Tushare {api_name}({ts_code}, {ts_start}, {ts_end})")
        
        try:
            if is_hk:
                # 港股使用 hk_daily 接口
                df = self._api.hk_daily(
                    ts_code=ts_code,
                    start_date=ts_start,
                    end_date=ts_end,
                )
            elif is_etf:
                # ETF uses fund_daily interface
                df = self._api.fund_daily(
                    ts_code=ts_code,
                    start_date=ts_start,
                    end_date=ts_end,
                )
            else:
                # Regular A-share stocks use daily interface
                df = self._api.daily(
                    ts_code=ts_code,
                    start_date=ts_start,
                    end_date=ts_end,
                )
                if self._kline_adjust == "qfq" and df is not None and not df.empty:
                    df = self._apply_forward_adjust(df, ts_code, ts_start, ts_end)
            
            return df
            
        except Exception as e:
            error_msg = str(e).lower()
            
            # 检测配额超限
            if any(keyword in error_msg for keyword in ['quota', '配额', 'limit', '权限']):
                logger.warning(f"Tushare 配额可能超限: {e}")
                raise RateLimitError(f"Tushare 配额超限: {e}") from e
            
            raise DataFetchError(f"Tushare 获取数据失败: {e}") from e

    def _apply_forward_adjust(
        self,
        df: pd.DataFrame,
        ts_code: str,
        ts_start: str,
        ts_end: str,
    ) -> pd.DataFrame:
        """
        将 Tushare daily 的不复权数据转换为前复权（qfq）。

        Tushare 的 ``daily`` 接口返回未复权价格，除权除息会造成跳空，
        导致跨区间的涨跌幅、均线、RSI 等指标失真（且与 AkShare/Tencent
        等 fallback 源的 qfq 口径不一致）。前复权公式：

            qfq_price = raw_price * adj_factor / adj_factor[最新交易日]

        复权后价格序列在除权日连续，``pct_chg`` / ``pre_close`` 仍沿用
        Tushare 原始字段（其本身已是除权口径的正确涨跌幅）。

        任何异常（接口失败、因子缺失、非正因子）都降级返回原数据，
        保证数据源可用性优先于复权精度。
        """
        try:
            adj_df = self._api.adj_factor(
                ts_code=ts_code,
                start_date=ts_start,
                end_date=ts_end,
            )
            if (
                adj_df is None
                or not isinstance(adj_df, pd.DataFrame)
                or adj_df.empty
                or "adj_factor" not in adj_df.columns
                or "trade_date" not in adj_df.columns
            ):
                logger.debug("Tushare adj_factor 返回为空，跳过前复权")
                return df

            merged = df.merge(
                adj_df[["trade_date", "adj_factor"]],
                on="trade_date",
                how="left",
            )
            if merged["adj_factor"].isna().any():
                logger.warning("Tushare adj_factor 存在缺失交易日，跳过前复权")
                return df

            # daily 返回倒序（最新交易日在前），必须按 trade_date 取最新一行，
            # 不能依赖行位置（iloc[-1] 会取到最早交易日）。
            latest_idx = merged["trade_date"].idxmax()
            latest_adj = merged.loc[latest_idx, "adj_factor"]
            if latest_adj is None or latest_adj <= 0:
                logger.warning("Tushare adj_factor 最新值异常（%r），跳过前复权", latest_adj)
                return df

            for col in ("open", "high", "low", "close"):
                if col in merged.columns:
                    merged[col] = merged[col] * merged["adj_factor"] / latest_adj

            logger.debug(
                "Tushare 前复权完成: %s, latest_adj=%s, rows=%s",
                ts_code,
                latest_adj,
                len(merged),
            )
            return merged
        except Exception as e:
            logger.warning("Tushare 前复权失败（降级为不复权）: %s", e)
            return df
    
    def _normalize_data(self, df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
        """
        标准化 Tushare 数据
        
        Tushare daily / fund_daily 返回的列名：
        ts_code, trade_date, open, high, low, close, pre_close, change, pct_chg, vol, amount
        
        需要映射到标准列名：
        date, open, high, low, close, volume, amount, pct_chg

        单位缩放仅适用于 A 股（及 ETF 等使用同一套单位的接口）：
        - vol 按「手」计，乘以 100 转为「股」
        - amount 按「千元」计，乘以 1000 转为「元」

        港股 hk_daily 返回的 vol / amount 已是可直接使用的量级，不做上述缩放。
        """
        df = df.copy()
        is_hk = _is_hk_market(stock_code)

        # 列名映射
        column_mapping = {
            'trade_date': 'date',
            'vol': 'volume',
            # open, high, low, close, amount, pct_chg 列名相同
        }
        
        df = df.rename(columns=column_mapping)
        
        # 转换日期格式（YYYYMMDD -> YYYY-MM-DD）
        if 'date' in df.columns:
            df['date'] = pd.to_datetime(df['date'], format='%Y%m%d')
        
        # 成交量 / 成交额：仅 A 股类接口做单位换算（港股 hk_daily 不换算）
        if 'volume' in df.columns and not is_hk:
            df['volume'] = df['volume'] * 100
        
        if 'amount' in df.columns and not is_hk:
            df['amount'] = df['amount'] * 1000
        
        # 添加股票代码列
        df['code'] = stock_code
        
        # 只保留需要的列
        keep_cols = ['code'] + STANDARD_COLUMNS
        existing_cols = [col for col in keep_cols if col in df.columns]
        df = df[existing_cols]
        
        return df

    def get_stock_name(self, stock_code: str) -> Optional[str]:
        """
        获取股票名称
        
        使用 Tushare 的 stock_basic 接口获取股票基本信息
        
        Args:
            stock_code: 股票代码
            
        Returns:
            股票名称，失败返回 None
        """
        if self._api is None:
            logger.warning("Tushare API 未初始化，无法获取股票名称")
            return None

        # 检查缓存
        if hasattr(self, '_stock_name_cache') and stock_code in self._stock_name_cache:
            return self._stock_name_cache[stock_code]
        
        # 初始化缓存
        if not hasattr(self, '_stock_name_cache'):
            self._stock_name_cache = {}
        
        try:
            # 速率限制检查
            self._check_rate_limit()
            

            # 根据市场/类型选择基础信息接口
            if _is_hk_market(stock_code):
                ts_code = self._convert_hk_stock_code_for_tushare(stock_code)
                # 港股：使用 hk_basic
                df = self._api.hk_basic(
                    ts_code=ts_code,
                    fields='ts_code,name'
                )
            elif _is_etf_code(stock_code):
                ts_code = self._convert_stock_code(stock_code)
                # ETF：使用 fund_basic
                df = self._api.fund_basic(
                    ts_code=ts_code,
                    fields='ts_code,name'
                )
            else:
                ts_code = self._convert_stock_code(stock_code)
                # A 股股票：使用 stock_basic
                df = self._api.stock_basic(
                    ts_code=ts_code,
                    fields='ts_code,name'
                )
            
            if df is not None and not df.empty:
                name = df.iloc[0]['name']
                self._stock_name_cache[stock_code] = name
                logger.debug(f"Tushare 获取股票名称成功: {stock_code} -> {name}")
                return name
            
        except Exception as e:
            logger.warning(f"Tushare 获取股票名称失败 {stock_code}: {e}")
        
        return None
    
    def get_stock_list(self) -> Optional[pd.DataFrame]:
        """
        获取股票列表
        
        使用 Tushare 的 stock_basic 接口获取 A 股列表（不含港股）。
        
        Returns:
            包含 code, name, industry, area, market 列的 DataFrame，失败返回 None
        """
        if self._api is None:
            logger.warning("Tushare API 未初始化，无法获取股票列表")
            return None
        
        try:
            self._check_rate_limit()

            df = self._api.stock_basic(
                exchange='',
                list_status='L',
                fields='ts_code,name,industry,area,market'
            )

            if df is None or df.empty:
                return None

            df = df.copy()
            df['code'] = df['ts_code'].astype(str).str.split('.').str[0]

            if not hasattr(self, '_stock_name_cache'):
                self._stock_name_cache = {}
            for _, row in df.iterrows():
                self._stock_name_cache[row['code']] = row['name']

            logger.info(f"Tushare 获取股票列表成功: {len(df)} 条")
            return df[['code', 'name', 'industry', 'area', 'market']]

        except Exception as e:
            logger.warning(f"Tushare 获取股票列表失败: {e}")

        return None
    
    def get_realtime_quote(self, stock_code: str) -> Optional[UnifiedRealtimeQuote]:
        """
        获取实时行情

        策略：
        1. 优先尝试 Pro 接口（需要2000积分）：数据全，稳定性高
        2. 失败降级到旧版接口：门槛低，数据较少

        Args:
            stock_code: 股票代码

        Returns:
            UnifiedRealtimeQuote 对象，失败返回 None
        """
        if self._api is None:
            return None

        # HK stocks not supported by Tushare
        if _is_hk_market(stock_code):
            logger.debug(f"TushareFetcher 跳过港股实时行情 {stock_code}")
            return None

        normalized_code = normalize_stock_code(stock_code)

        from .realtime_types import (
            RealtimeSource,
            safe_float, safe_int
        )

        # 速率限制检查
        self._check_rate_limit()

        # ---- 尝试 Pro 接口 rt_k (需要2000积分) ----
        if not self._pro_dead:
            try:
                ts_code = self._convert_stock_code(stock_code)
                df = self._call_api_with_rate_limit("rt_k", ts_code=ts_code)

                if df is not None and not df.empty:
                    row = df.iloc[0]
                    logger.debug(f"Tushare Pro rt_k 实时行情获取成功: {stock_code}")

                    # rt_k 字段说明 (doc_id=372):
                    #   close = 最新价, vol = 成交量(股), amount = 成交额(元)
                    #   涨跌幅/涨跌额需手动计算
                    close = safe_float(row.get('close'))
                    pre_close = safe_float(row.get('pre_close'))
                    if close is not None and pre_close is not None and pre_close != 0:
                        change_pct = round((close - pre_close) / pre_close * 100, 2)
                        change_amount = round(close - pre_close, 2)
                    else:
                        change_pct = None
                        change_amount = None

                    return UnifiedRealtimeQuote(
                        code=normalized_code,
                        name=str(row.get('name', '')),
                        source=RealtimeSource.TUSHARE,
                        price=close,
                        change_pct=change_pct,
                        change_amount=change_amount,
                        volume=safe_int(row.get('vol')),
                        amount=safe_float(row.get('amount')),
                        high=safe_float(row.get('high')),
                        low=safe_float(row.get('low')),
                        open_price=safe_float(row.get('open')),
                        pre_close=pre_close,
                        # rt_k 不返回 PE/PB/换手率/总市值，
                        # 这些字段由其他数据源在 get_realtime_quote_multisource 层补齐
                        turnover_rate=None,
                        pe_ratio=None,
                        pb_ratio=None,
                        total_mv=None,
                    )
                # 空返回：休市 / 代码无效
                logger.debug(
                    f"Tushare Pro rt_k 实时行情返回空 {stock_code}，可能原因：休市或代码无效"
                )
            except Exception as e:
                _err = str(e)
                if "接口名" in _err:
                    self._pro_dead = True
                    logger.info(f"Tushare Pro rt_k 接口不可用: {_err.strip()}，已跳过后续尝试")
                else:
                    logger.debug(f"Tushare Pro rt_k 实时行情异常 {stock_code}: {_err.strip()}")

        # ---- 降级：旧版接口 (ts.get_realtime_quotes) ----
        if not self._legacy_dead:
            try:
                import tushare as ts

                symbol = self._get_legacy_realtime_symbol(stock_code)

                # 旧版接口无超时控制，线程池兜底
                _timeout_s = 10.0
                with ThreadPoolExecutor(max_workers=1) as executor:
                    fut = executor.submit(ts.get_realtime_quotes, symbol)
                    try:
                        df = fut.result(timeout=_timeout_s)
                    except FuturesTimeoutError:
                        logger.info(
                            f"Tushare 旧版实时行情超时 {stock_code} "
                            f"(>{_timeout_s}s)，旧版接口已基本不可用"
                        )
                        return None

                if df is None or df.empty:
                    return None

                row = df.iloc[0]

                # 计算涨跌幅
                price = safe_float(row['price'])
                pre_close = safe_float(row['pre_close'])
                change_pct = 0.0
                change_amount = 0.0

                if price and pre_close and pre_close > 0:
                    change_amount = price - pre_close
                    change_pct = (change_amount / pre_close) * 100

                return UnifiedRealtimeQuote(
                    code=normalized_code,
                    name=str(row['name']),
                    source=RealtimeSource.TUSHARE,
                    price=price,
                    change_pct=round(change_pct, 2),
                    change_amount=round(change_amount, 2),
                    volume=safe_int(row['volume']) // 100,
                    amount=safe_float(row['amount']),
                    high=safe_float(row['high']),
                    low=safe_float(row['low']),
                    open_price=safe_float(row['open']),
                    pre_close=pre_close,
                )

            except Exception as e:
                _err = str(e)
                self._legacy_dead = True
                logger.info(
                    f"Tushare 旧版实时行情失败 {stock_code}: {_err.strip()}，"
                    f"已跳过后续旧版接口尝试"
                )

        return None

    def get_main_indices(self, region: str = "cn") -> Optional[List[dict]]:
        """
        获取主要指数实时行情 (Tushare Pro)，仅支持 A 股
        """
        if region != "cn":
            return None
        if self._api is None:
            return None

        from .realtime_types import safe_float

        # 指数映射：Tushare代码 -> 名称
        indices_map = {
            '000001.SH': '上证指数',
            '399001.SZ': '深证成指',
            '399006.SZ': '创业板指',
            '000688.SH': '科创50',
            '000016.SH': '上证50',
            '000300.SH': '沪深300',
        }

        try:
            self._check_rate_limit()

            # Tushare index_daily 获取历史数据，实时数据需用其他接口或估算
            # 由于 Tushare 免费用户可能无法获取指数实时行情，这里作为备选
            # 使用 index_daily 获取最近交易日数据

            end_date = datetime.now().strftime('%Y%m%d')
            start_date = (datetime.now() - pd.Timedelta(days=5)).strftime('%Y%m%d')

            results = []

            # 批量获取所有指数数据
            for ts_code, name in indices_map.items():
                try:
                    df = self._api.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                    if df is not None and not df.empty:
                        row = df.iloc[0] # 最新一天

                        current = safe_float(row['close'])
                        prev_close = safe_float(row['pre_close'])

                        results.append({
                            'code': ts_code.split('.')[0], # 兼容 sh000001 格式需转换，这里保持纯数字
                            'name': name,
                            'current': current,
                            'change': safe_float(row['change']),
                            'change_pct': safe_float(row['pct_chg']),
                            'open': safe_float(row['open']),
                            'high': safe_float(row['high']),
                            'low': safe_float(row['low']),
                            'prev_close': prev_close,
                            'volume': safe_float(row['vol']),
                            'amount': safe_float(row['amount']) * 1000, # 千元转元
                            'amplitude': 0.0 # Tushare index_daily 不直接返回振幅
                        })
                except Exception as e:
                    logger.debug(f"Tushare 获取指数 {name} 失败: {e}")
                    continue

            if results:
                return results
            else:
                logger.warning("[Tushare] 未获取到指数行情数据")

        except Exception as e:
            logger.error(f"[Tushare] 获取指数行情失败: {e}")

        return None

    def get_market_stats(self) -> Optional[dict]:
        """
        获取市场涨跌统计 (Tushare Pro)
        2000积分 每天访问该接口 ts.pro_api().rt_k 两次
        接口限制见：https://tushare.pro/document/1?doc_id=108
        """
        if self._api is None:
            return None

        try:
            logger.info("[Tushare] ts.pro_api() 获取市场统计...")
            
            # 获取当前中国时间，判断是否在交易时间内
            china_now = self._get_china_now()
            current_clock = china_now.strftime("%H:%M")
            current_date = china_now.strftime("%Y%m%d")

            trade_dates = self._get_trade_dates(current_date)
            if not trade_dates:
                return None

            if current_date in trade_dates:
                if current_clock < '09:30' or current_clock > '16:30':
                    use_realtime = False
                else:
                    use_realtime = True
            else:
                use_realtime = False

            # 若实盘的时候使用 则使用其他可以实盘获取的数据源 akshare、efinance
            if use_realtime:
                try:
                    df = self._call_api_with_rate_limit("rt_k", ts_code='3*.SZ,6*.SH,0*.SZ,92*.BJ')
                    if df is not None and not df.empty:
                        return self._calc_market_stats(df)
                    
                except Exception as e:
                    logger.error(f"[Tushare] ts.pro_api().rt_k 尝试获取实时数据失败: {e}")
                    return None
            else:

                if current_date not in trade_dates:
                    last_date = self._pick_trade_date(trade_dates, use_today=True)  # 拿最近的日期
                else:
                    if current_clock < '09:30': 
                        last_date = self._pick_trade_date(trade_dates, use_today=False)  # 拿取前一天的数据
                    else:  # 即 '> 16:30'                  
                        last_date = self._pick_trade_date(trade_dates, use_today=True)  # 拿取当天的数据

                if last_date is None:
                    return None

                try:
                    df = self._call_api_with_rate_limit(
                        "daily",
                        ts_code='3*.SZ,6*.SH,0*.SZ,92*.BJ',
                        start_date=last_date,
                        end_date=last_date,
                    )
                    # 为防止不同接口返回的列名大小写不一致（例如 rt_k 返回小写，daily 返回大写），统一将列名转为小写
                    df.columns = [col.lower() for col in df.columns]

                    # 获取股票基础信息（包含代码和名称）
                    df_basic = self._call_api_with_rate_limit("stock_basic", fields='ts_code,name')
                    df = pd.merge(df, df_basic, on='ts_code', how='left')
                    # 将 daily的 amount 列的值乘以 1000 来和其他数据源保持一致
                    if 'amount' in df.columns:
                        df['amount'] = df['amount'] * 1000

                    if df is not None and not df.empty:
                        return self._calc_market_stats(df)
                except Exception as e:
                    logger.error(f"[Tushare] ts.pro_api().daily 获取数据失败: {e}")
                    

            
        except Exception as e:
            logger.error(f"[Tushare] 获取市场统计失败: {e}")

        return None
    
    def _calc_market_stats(
            self,
            df: pd.DataFrame,
            ) -> Optional[Dict[str, Any]]:
            """从行情 DataFrame 计算涨跌统计。"""
            import numpy as np

            df = df.copy()
            
            # 1. 提取基础比对数据：最新价、昨收
            # 兼容不同接口返回的列名 sina/em efinance tushare xtdata
            code_col = next((c for c in ['代码', '股票代码', 'ts_code','stock_code'] if c in df.columns), None)
            name_col = next((c for c in ['名称', '股票名称','name','name'] if c in df.columns), None)
            close_col = next((c for c in ['最新价', '最新价', 'close','lastPrice'] if c in df.columns), None)
            pre_close_col = next((c for c in ['昨收', '昨日收盘', 'pre_close','lastClose'] if c in df.columns), None)
            amount_col = next((c for c in ['成交额', '成交额', 'amount','amount'] if c in df.columns), None) 
            
            limit_up_count = 0
            limit_down_count = 0
            up_count = 0
            down_count = 0
            flat_count = 0

            for code, name, current_price, pre_close, amount in zip(
                df[code_col], df[name_col], df[close_col], df[pre_close_col], df[amount_col]
            ):
                
                # 停牌过滤 efinance 的停牌数据有时候会缺失价格显示为 '-'，em 显示为none
                if pd.isna(current_price) or pd.isna(pre_close) or current_price in ['-'] or pre_close in ['-'] or amount == 0:
                    continue
                
                # em、efinance 为str 需要转换为float
                current_price = float(current_price)
                pre_close = float(pre_close)
                
                # 获取去除前缀的纯数字代码
                pure_code = normalize_stock_code(str(code)) 

                # A. 确定每只股票的涨跌幅比例 (使用纯数字代码判断)
                if is_bse_code(pure_code): 
                    ratio = 0.30
                elif is_kc_cy_stock(pure_code): #pure_code.startswith(('688', '30')):
                    ratio = 0.20
                elif is_st_stock(name): #'ST' in str_name:
                    ratio = 0.05
                else:
                    ratio = 0.10

                # B. 严格按照 A 股规则计算涨跌停价：昨收 * (1 ± 比例) -> 四舍五入保留2位小数
                limit_up_price = np.floor(pre_close * (1 + ratio) * 100 + 0.5) / 100.0
                limit_down_price = np.floor(pre_close * (1 - ratio) * 100 + 0.5) / 100.0

                limit_up_price_Tolerance = round(abs(pre_close * (1 + ratio) - limit_up_price), 10)
                limit_down_price_Tolerance = round(abs(pre_close * (1 - ratio) - limit_down_price), 10)

                # C. 精确比对
                if current_price > 0 :
                    is_limit_up = (current_price > 0) and (abs(current_price - limit_up_price) <= limit_up_price_Tolerance)
                    is_limit_down = (current_price > 0) and (abs(current_price - limit_down_price) <= limit_down_price_Tolerance)

                    if is_limit_up:
                        limit_up_count += 1
                    if is_limit_down:
                        limit_down_count += 1

                    if current_price > pre_close:
                        up_count += 1
                    elif current_price < pre_close:
                        down_count += 1
                    else:
                        flat_count += 1
                    
            # 统计数量
            stats = {
                'up_count': up_count,
                'down_count': down_count,
                'flat_count': flat_count,
                'limit_up_count': limit_up_count,
                'limit_down_count': limit_down_count,
                'total_amount': 0.0,
            }
            
            # 成交额统计
            if amount_col and amount_col in df.columns:
                df[amount_col] = pd.to_numeric(df[amount_col], errors='coerce')
                stats['total_amount'] = (df[amount_col].sum() / 1e8)
                
            return stats

    def get_trade_time(self,early_time='09:30',late_time='16:30') -> Optional[str]:
        '''
        获取当前时间可以获得数据的开始时间日期

        Args:
                early_time: 默认 '09:30'
                late_time: 默认 '16:30'
                early_time-late_time 之间为使用上一个交易日数据的时间段，其他时间为使用当天数据的时间段
        Returns:
                start_date: 可以获得数据的开始日期
        '''
        china_now = self._get_china_now()
        china_date = china_now.strftime("%Y%m%d")
        china_clock = china_now.strftime("%H:%M")

        trade_dates = self._get_trade_dates(china_date)
        if not trade_dates:
            return None

        if china_date in trade_dates:
            if  early_time < china_clock < late_time: # 使用上一个交易日数据的时间段
                use_today = False
            else:
                use_today = True
        else:
            # 非交易日（周末 / 节假日）：trade_dates[0] 就是最近的交易日，
            # 直接用上一个交易日的数据，不要传 use_today=True 取当天。
            # 此前传 True 导致非交易日 block_trade / moneyflow_ind_dc 全部拉空。
            start_date = trade_dates[0]
            logger.info(
                f"[Tushare] 非交易日 ({china_date})，回退到最近交易日 {start_date}。"
                "板块资金流 / 大宗交易通常 T+1 才有完整数据。"
            )
            return start_date

        start_date = self._pick_trade_date(trade_dates, use_today=use_today)
        if start_date is None:
            return None

        if not use_today:
            logger.info(f"[Tushare] 当前时间 {china_clock} 可能无法获取当天筹码分布，尝试获取前一个交易日的数据 {start_date}")

        return start_date
    
    def get_sector_rankings(self, n: int = 5) -> Optional[Tuple[list, list]]:
        """
        获取行业板块涨跌榜 (Tushare Pro)
        
        数据源优先级：
        1. 同花顺接口 (ts.pro_api().moneyflow_ind_ths)
        2. 东财接口 (ts.pro_api().moneyflow_ind_dc)
        注意：每个接口的行业分类和板块定义不同，会导致结果两者不一致
        """
        def _get_rank_top_n(df: pd.DataFrame, change_col: str, industry_name: str, n: int) -> Tuple[list, list]:
            df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
            df = df.dropna(subset=[change_col])

            # 涨幅前n
            top = df.nlargest(n, change_col)
            top_sectors = [
                {'name': row[industry_name], 'change_pct': row[change_col]}
                for _, row in top.iterrows()
            ]

            bottom = df.nsmallest(n, change_col)
            bottom_sectors = [
                {'name': row[industry_name], 'change_pct': row[change_col]}
                for _, row in bottom.iterrows()
            ]
            return top_sectors, bottom_sectors

        # 15:30之后才有当天数据
        start_date = self.get_trade_time(early_time='00:00', late_time='15:30')
        if not start_date:
            return None

        # 优先同花顺接口
        logger.info("[Tushare] ts.pro_api().moneyflow_ind_ths 获取板块排行(同花顺)...")
        try:
            df = self._call_api_with_rate_limit("moneyflow_ind_ths", trade_date=start_date)
            if df is not None and not df.empty:
                change_col = 'pct_change'
                name = 'industry'
                if change_col in df.columns:
                    return _get_rank_top_n(df, change_col, name, n)
        except Exception as e:
            logger.warning(f"[Tushare] 获取同花顺行业板块涨跌榜失败: {e} 尝试东财接口")

        # 同花顺接口失败，降级尝试东财接口
        logger.info("[Tushare] ts.pro_api().moneyflow_ind_dc 获取板块排行(东财)...")
        try:
            df = self._call_api_with_rate_limit("moneyflow_ind_dc", trade_date=start_date)
            if df is not None and not df.empty:
                df = df[df['content_type'] == '行业']  # 过滤出行业板块
                change_col = 'pct_change'
                name = 'name'
                if change_col in df.columns:
                    return _get_rank_top_n(df, change_col, name, n)
        except Exception as e:
            logger.warning(f"[Tushare] 获取东财行业板块涨跌榜失败: {e}")
            return None
        
        # 获取为空或者接口调用失败，返回 None
        return None

    def get_sector_money_flow(
        self,
        top_n: int = 10,
        trade_date: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """板块资金流（主力/中户/散户/暗盘）分析.

        数据源：
        - ``moneyflow_ind_dc``（东方财富，含 buy_elg/lg/md/sm_amount 拆分）
        - ``block_trade``（大宗交易，按行业聚合，并按 lead_stock 补全）

        返回按 ``main_net`` 绝对值排序的前 ``top_n`` 个板块。
        缺失暗盘/大宗交易数据时，``block_net`` 设为 ``None``，前端会显示 "—"。

        自适应回退：当 ``moneyflow_ind_dc`` 在 ``trade_date`` 当天返回空时（常见于
        tushare 数据延迟 / 周末 / 节假日），自动按 trade_cal 回退到最近 1-3 个交易日
        重试。最多尝试 5 个交易日，全部失败才返回 None。
        """
        try:
            start_date = trade_date or self.get_trade_time(early_time="00:00", late_time="15:30")
            if not start_date:
                logger.warning("[Tushare] 无法确定板块资金流日期")
                return None

            # 自适应回退：仅针对 moneyflow_ind_dc 本身为空的情况
            # （典型场景：tushare 数据延迟 / 周末 / 节假日当天数据未写入）。
            # block_trade（暗盘）延迟不应阻塞当天板块资金流的展示，缺失时 block_net
            # 保持为 None 即可（下游已处理为空展示）。
            trade_dates_all = self._get_trade_dates(start_date)
            if not trade_dates_all:
                return None
            if trade_dates_all[0] != start_date and start_date in trade_dates_all:
                trade_dates_all = [start_date] + [d for d in trade_dates_all if d != start_date]

            df: Optional[pd.DataFrame] = None
            used_date = start_date
            for candidate in trade_dates_all[:5]:
                logger.info("[Tushare] 获取板块资金流（%s）...", candidate)
                df = self._call_api_with_rate_limit("moneyflow_ind_dc", trade_date=candidate)
                if df is None or df.empty:
                    continue
                used_date = candidate
                if candidate != start_date:
                    logger.info(
                        "[Tushare] %s 资金流回退到 %s 拿到数据",
                        start_date, candidate,
                    )
                break
            if df is None or df.empty:
                logger.info("[Tushare] 连续 %d 天资金流均为空，跳过板块资金流", 5)
                return None

            # 仅看"行业"content_type，排除"概念/其他"
            if "content_type" in df.columns:
                df = df[df["content_type"].astype(str) == "行业"].copy()
                if df.empty:
                    logger.info("[Tushare] moneyflow_ind_dc 行业分类为空")
                    return None

            # 把 buy_*_amount 的字符串列归一化（单位：元，Tushare 默认单位）
            money_cols = [
                "net_amount", "buy_elg_amount", "buy_lg_amount",
                "buy_md_amount", "buy_sm_amount",
            ]
            for col in money_cols:
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce").fillna(0.0)
                else:
                    df[col] = 0.0

            rows: List[Dict[str, Any]] = []
            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                if not name:
                    continue
                # 主力净流入：特大单 + 大单，moneyflow_ind_dc 的 net_amount 已经
                # 等于特大单 + 大单的净流入，但为防御起见仍按字段名直接读取。
                main_net = float(row.get("net_amount", 0.0))
                rows.append(
                    {
                        "name": name,
                        "ts_code": str(row.get("ts_code", "")).strip(),
                        "content_type": "行业",
                        "pct_change": _safe_pct_change(row.get("pct_change")),
                        "main_net": main_net,
                        "mid_net": float(row.get("buy_md_amount", 0.0)),
                        "retail_net": float(row.get("buy_sm_amount", 0.0)),
                        "block_net": None,  # 第二步由 block_trade 填充
                        "lead_stock": str(row.get("buy_sm_amount_stock", "")).strip() or None,
                        "source": "tushare_dc",
                        # 实际数据日期（用于上层判断 Tushare 盘后快照是否已更新）
                        "trade_date": used_date,
                    }
                )

            # 注入暗盘（大宗交易）按行业聚合
            # 自适应回退后用 used_date，让 block_trade / 五日主力都基于真实可用的日期
            self._merge_block_trade_into_sector_money_flow(rows, trade_date=used_date)

            # 第二轮：按 lead_stock 名称补全（覆盖 stock_basic.industry 严格匹配
            # 漏掉的"同一 lead_stock 跨多板块"场景）
            self._supplement_block_net_by_lead_stock(rows, trade_date=used_date)

            # 五日主力净流入
            self._merge_5d_main_net(rows, trade_date=used_date)

            # 行业板块展示策略：涨幅 top_n/2 + 跌幅 top_n/2，覆盖涨跌两极
            # top_n >= 999 表示请求全量（用于 block_trade_heat 等下游）
            if int(top_n) >= 999:
                return rows
            return self._select_top_gainers_and_losers(rows, top_n=max(1, int(top_n)))
        except Exception as exc:
            logger.warning("[Tushare] 获取板块资金流失败: %s", exc)
            return None

    def _merge_block_trade_into_sector_money_flow(
        self,
        rows: List[Dict[str, Any]],
        trade_date: str,
    ) -> None:
        """把 Tushare 大宗交易按行业聚合后回写到 ``rows`` 的 ``block_net`` 字段.

        依赖 ``stock_basic`` 拉取每只股票所属 industry（行业板块名）；
        单只股票缺失 industry 时该笔大宗交易会被跳过。
        """
        if not rows:
            return
        try:
            block_df = self._call_api_with_rate_limit("block_trade", trade_date=trade_date)
            if block_df is None or block_df.empty:
                logger.info("[Tushare] block_trade 返回空，无暗盘数据")
                return
            if "amount" not in block_df.columns:
                return
            block_df["amount"] = pd.to_numeric(block_df["amount"], errors="coerce").fillna(0.0)
            ts_codes = sorted({str(c) for c in block_df.get("ts_code", []) if c})
            if not ts_codes:
                return

            industry_map = self._load_industry_map(ts_codes)
            if not industry_map:
                logger.info("[Tushare] 未能建立 stock_code->industry 映射，跳过暗盘聚合")
                return

            agg: Dict[str, float] = {}
            for _, b in block_df.iterrows():
                ind = industry_map.get(str(b["ts_code"]))
                if not ind:
                    continue
                agg[ind] = agg.get(ind, 0.0) + float(b["amount"])

            for row in rows:
                ind = row.get("name")
                if ind in agg:
                    # Tushare block_trade.amount 单位：万元 → 元
                    row["block_net"] = agg[ind] * 1e4
        except Exception as exc:
            logger.debug("[Tushare] 暗盘聚合失败，跳过: %s", exc)

    def _supplement_block_net_by_lead_stock(
        self,
        rows: List[Dict[str, Any]],
        trade_date: str,
    ) -> None:
        """按 lead_stock 所在的申万行业补全暗盘数据。

        ``_merge_block_trade_into_sector_money_flow`` 按 stock_basic.industry
        严格匹配板块名，但 moneyflow_ind_dc（东财 ~496 个细分行业）与
        stock_basic.industry（申万 ~110 个中类）名称体系不兼容，仅约 7% 能
        直接匹配。该方法退而求其次：以 lead_stock 为锚，找出它所属的**申万行业
        全量** block_trade 总额，作为该东财板块的暗盘近似值。

        lead_stock 通常是板块内主力资金活跃度最高的股票，其申万行业可以作为
        "该板块最近的标的池"的代理。
        """
        if not rows:
            return
        try:
            bt_df = self._call_api_with_rate_limit("block_trade", trade_date=trade_date)
            if bt_df is None or bt_df.empty:
                return
            bt_df = bt_df.copy()
            bt_df["amount"] = pd.to_numeric(bt_df["amount"], errors="coerce").fillna(0.0)
            ts_codes = sorted({str(c) for c in bt_df["ts_code"] if c})

            # 申万行业暗盘聚合（一次性算全部行业）
            bs_map = self._load_basic_industry_map(ts_codes)
            if not bs_map:
                return
            ind_blocks: Dict[str, float] = {}
            for _, b in bt_df.iterrows():
                ind = bs_map.get(str(b["ts_code"]))
                if ind:
                    ind_blocks[ind] = ind_blocks.get(ind, 0.0) + float(b["amount"]) * 1e4

            # ts_code → 申万行业（仅用于 lead_stock 反查）
            name_to_ind: Dict[str, str] = {}
            try:
                basic = self._call_api_with_rate_limit("stock_basic", list_status="L")
                if basic is not None and not basic.empty:
                    basic["ts_code"] = basic["ts_code"].astype(str)
                    for _, r in basic.iterrows():
                        nm = str(r.get("name", "")).strip()
                        ind = str(r.get("industry", "")).strip()
                        if nm and ind:
                            name_to_ind[nm] = ind
            except Exception:
                pass

            for row in rows:
                lead = (row.get("lead_stock") or "").strip()
                if not lead:
                    continue
                existing = row.get("block_net")
                if isinstance(existing, (int, float)) and existing > 0:
                    continue
                sw_ind = name_to_ind.get(lead)
                if not sw_ind:
                    continue
                # FIX: 直接取默认值 0，避免上一轮未赋值导致 NameError
                val = ind_blocks.get(sw_ind, 0.0)
                if val > 0:
                    row["block_net"] = val
                    row["block_net_source"] = "lead_industry"
        except Exception as exc:
            logger.debug("[Tushare] lead_stock 申万行业暗盘补全失败: %s", exc)

    def _load_basic_name_map(self, ts_codes: List[str]) -> Dict[str, str]:
        """ts_code → 股票名称（仅名称），供 lead_stock 反查用。"""
        cache = getattr(self, "_basic_name_cache", None)
        now = time.time()
        if cache and cache.get("ts_codes") == set(ts_codes) and now - cache.get("fetched_at", 0) < 300:
            return cache.get("map", {})

        mapping: Dict[str, str] = {}
        try:
            df = self._call_api_with_rate_limit("stock_basic", list_status="L")
            if df is not None and not df.empty:
                df["ts_code"] = df["ts_code"].astype(str)
                target = set(ts_codes)
                for _, row in df.iterrows():
                    ts = str(row["ts_code"])
                    nm = str(row.get("name", "")).strip()
                    if ts in target and nm:
                        mapping[ts] = nm
        except Exception as exc:
            logger.debug("[Tushare] 加载 stock_basic 名称失败: %s", exc)

        self._basic_name_cache = {
            "ts_codes": set(ts_codes),
            "fetched_at": now,
            "map": mapping,
        }
        return mapping

    def _load_basic_industry_map(
        self, ts_codes: List[str]
    ) -> Dict[str, str]:
        """ts_code → stock_basic.industry（申万行业名），用于暗盘聚合."""
        cache = getattr(self, "_basic_ind_cache", None)
        now = time.time()
        if cache and cache.get("ts_codes") == set(ts_codes) and now - cache.get("fetched_at", 0) < 300:
            return cache.get("map", {})

        mapping: Dict[str, str] = {}
        try:
            df = self._call_api_with_rate_limit("stock_basic", list_status="L")
            if df is not None and not df.empty:
                df["ts_code"] = df["ts_code"].astype(str)
                target = set(ts_codes)
                for _, row in df.iterrows():
                    ts = str(row["ts_code"])
                    if ts in target:
                        ind = str(row.get("industry", "")).strip()
                        if ind:
                            mapping[ts] = ind
        except Exception as exc:
            logger.debug("[Tushare] 加载 stock_basic industry 失败: %s", exc)

        self._basic_ind_cache = {
            "ts_codes": set(ts_codes),
            "fetched_at": now,
            "map": mapping,
        }
        return mapping

    @staticmethod
    def _select_top_gainers_and_losers(rows: list, top_n: int = 10) -> list:
        """行业板块展示策略：涨幅 top_n/2 + 跌幅 top_n/2.

        无论大盘涨还是跌，板块资金流表格都会同时覆盖最强和最弱的两极，
        与行业板块"领涨/领跌"展示保持一致。
        """
        valid = [r for r in rows if isinstance(r.get("pct_change"), (int, float))]
        top_up = sorted(valid, key=lambda r: r["pct_change"], reverse=True)[: top_n // 2]
        top_down = sorted(valid, key=lambda r: r["pct_change"])[: top_n - top_n // 2]
        return top_up + top_down

    def _merge_5d_main_net(
        self,
        rows: list,
        trade_date: str,
    ) -> None:
        """为每行注入 ``main_net_5d``（最近 5 交易日主力净流入之和，元）。

        从 ``moneyflow_ind_dc`` 拉取 60 天数据，按板块名聚合最近 5 日。
        """
        if not rows:
            return
        try:
            import datetime

            end_dt = datetime.datetime.strptime(trade_date, "%Y%m%d")
            start_dt = end_dt - datetime.timedelta(days=60)
            df_all = self._call_api_with_rate_limit(
                "moneyflow_ind_dc",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=end_dt.strftime("%Y%m%d"),
            )
            if df_all is None or df_all.empty:
                return
            if "content_type" in df_all.columns:
                df_all = df_all[df_all["content_type"].astype(str) == "行业"].copy()
            if df_all.empty:
                return

            df_all["net_amount"] = pd.to_numeric(df_all["net_amount"], errors="coerce").fillna(0.0)
            dates = sorted({str(d) for d in df_all["trade_date"].unique()})
            last5 = dates[-5:]
            df5 = df_all[df_all["trade_date"].astype(str).isin(last5)]

            agg = df5.groupby("name")["net_amount"].sum().to_dict()
            for row in rows:
                name = row.get("name", "")
                if name and name in agg:
                    row["main_net_5d"] = agg[name]
        except Exception as exc:
            logger.debug("[Tushare] 5日主力净流入聚合失败: %s", exc)

    def get_sector_money_flow_history(self, days: int = 10) -> List[Dict[str, Any]]:
        """获取行业板块最近 N 个交易日的资金流历史（板块轮动用）。

        从 ``moneyflow_ind_dc`` 拉取约 2 倍自然日的数据，过滤 ``content_type=行业``，
        按板块分组后每板块返回最近 ``days`` 个交易日记录（按日期升序）：

        返回：
            [{"name": 板块名, "ts_code": str, "history": [
                {"trade_date": "YYYYMMDD", "pct_change": float, "net_amount": float},
                ...
            ]}, ...]
        """
        try:
            import datetime

            end_dt = datetime.datetime.now()
            # 拉取约 2.5 倍自然日，确保覆盖 N 个交易日（含周末/节假日）
            start_dt = end_dt - datetime.timedelta(days=max(30, int(days * 2.5)))
            df_all = self._call_api_with_rate_limit(
                "moneyflow_ind_dc",
                start_date=start_dt.strftime("%Y%m%d"),
                end_date=end_dt.strftime("%Y%m%d"),
            )
            if df_all is None or df_all.empty:
                return []
            if "content_type" in df_all.columns:
                df_all = df_all[df_all["content_type"].astype(str) == "行业"].copy()
            if df_all.empty:
                return []

            df_all["net_amount"] = pd.to_numeric(df_all["net_amount"], errors="coerce").fillna(0.0)
            df_all["pct_change"] = pd.to_numeric(df_all["pct_change"], errors="coerce").fillna(0.0)
            df_all["trade_date"] = df_all["trade_date"].astype(str)

            result: List[Dict[str, Any]] = []
            for (name, ts_code), grp in df_all.groupby(["name", "ts_code"]):
                grp = grp.sort_values("trade_date")
                history = [
                    {
                        "trade_date": str(r["trade_date"]),
                        "pct_change": float(r["pct_change"]),
                        "net_amount": float(r["net_amount"]),
                    }
                    for _, r in grp.iterrows()
                ]
                result.append(
                    {
                        "name": str(name).strip(),
                        "ts_code": str(ts_code).strip(),
                        "history": history[-days:],
                    }
                )
            return result
        except Exception as exc:
            logger.warning("[Tushare] 获取板块资金流历史失败: %s", exc)
            return []

    def _load_industry_map(self, ts_codes: List[str]) -> Dict[str, str]:
        """拉取 ``ts_code -> industry`` 映射（行业板块名）。

        使用 5 分钟缓存，避免重复拉 ``stock_basic``。
        """
        cache = getattr(self, "_industry_map_cache", None)
        now = time.time()
        if cache and cache.get("ts_codes") == set(ts_codes) and now - cache.get("fetched_at", 0) < 300:
            return cache.get("map", {})

        mapping: Dict[str, str] = {}
        try:
            df = self._call_api_with_rate_limit("stock_basic", list_status="L")
            if df is not None and not df.empty and "industry" in df.columns:
                df["ts_code"] = df["ts_code"].astype(str)
                mapping = {
                    str(row["ts_code"]): str(row["industry"]).strip()
                    for _, row in df.iterrows()
                    if str(row.get("industry", "")).strip()
                }
        except Exception as exc:
            logger.debug("[Tushare] 加载 stock_basic 失败: %s", exc)

        self._industry_map_cache = {
            "ts_codes": set(ts_codes),
            "fetched_at": now,
            "map": mapping,
        }
        return mapping

    def get_chip_distribution(self, stock_code: str) -> Optional[ChipDistribution]:
        """
        获取筹码分布数据
        
        数据来源：ts.pro_api().cyq_chips()
        包含：获利比例、平均成本、筹码集中度
        
        注意：ETF/指数没有筹码分布数据，会直接返回 None；港股不支持，直接返回 None。
        5000积分以下每天访问15次,每小时访问5次
        
        Args:
            stock_code: 股票代码
            
        Returns:
            ChipDistribution 对象（最新交易日的数据），获取失败返回 None

        """
        if _is_us_code(stock_code):
            logger.warning(f"[Tushare] TushareFetcher 不支持美股 {stock_code} 的筹码分布")
            return None
        
        if _is_etf_code(stock_code):
            logger.warning(f"[Tushare] TushareFetcher 不支持 ETF {stock_code} 的筹码分布")
            return None

        if _is_hk_market(stock_code):
            logger.warning(f"[Tushare] TushareFetcher 不支持港股 {stock_code} 的筹码分布")
            return None
        
        try:
            # 19点之后才有当天数据
            start_date = self.get_trade_time(early_time='00:00', late_time='19:00') 
            if not start_date:
                return None

            ts_code = self._convert_stock_code(stock_code)

            df = self._call_api_with_rate_limit(
                "cyq_chips",
                ts_code=ts_code,
                start_date=start_date,
                end_date=start_date,
            )
            if df is not None and not df.empty:
                daily_df = self._call_api_with_rate_limit(
                    "daily",
                    ts_code=ts_code,
                    start_date=start_date,
                    end_date=start_date,
                )
                if daily_df is None or daily_df.empty:
                    return None
                current_price = daily_df.iloc[0]['close']
                metrics = self.compute_cyq_metrics(df, current_price)

                chip = ChipDistribution(
                    code=stock_code,
                    date=datetime.strptime(start_date, '%Y%m%d').strftime('%Y-%m-%d'),
                    profit_ratio=metrics['获利比例'],
                    avg_cost=metrics['平均成本'],
                    cost_90_low=metrics['90成本-低'],
                    cost_90_high=metrics['90成本-高'],
                    concentration_90=metrics['90集中度'],
                    cost_70_low=metrics['70成本-低'],
                    cost_70_high=metrics['70成本-高'],
                    concentration_70=metrics['70集中度'],
                )
                
                logger.info(f"[筹码分布] {stock_code} 日期={chip.date}: 获利比例={chip.profit_ratio:.1%}, "
                        f"平均成本={chip.avg_cost}, 90%集中度={chip.concentration_90:.2%}, "
                        f"70%集中度={chip.concentration_70:.2%}")
                return chip

        except Exception as e:
            logger.warning(f"[Tushare] 获取筹码分布失败 {stock_code}: {e}")
            return None

    def compute_cyq_metrics(self, df: pd.DataFrame, current_price: float) -> dict:
        """
        基于 Tushare 的筹码分布明细表 (cyq_chips) 计算常用筹码指标  
        :param df: 包含 'price' 和 'percent' 列的 DataFrame  
        :param current_price: 股票当天的当前价/收盘价 (用于计算获利比例)  
        :return: 包含各项筹码指标的字典  
        """
        import numpy as np
        # 1. 确保按价格从小到大排序 (Tushare 返回的数据往往是纯倒序的)
        df_sorted = df.sort_values(by='price', ascending=True).reset_index(drop=True)

        # 2. 防止原始数据 percent 总和产生浮点数误差，归一化到 100%
        total_percent = df_sorted['percent'].sum()

        df_sorted['norm_percent'] = df_sorted['percent'] / total_percent * 100

        # 3. 计算筹码的累积分布
        df_sorted['cumsum'] = df_sorted['norm_percent'].cumsum()

        # --- 获利比例 ---
        # 所有价格 <= 当前价的筹码之和
        winner_rate = df_sorted[df_sorted['price'] <= current_price]['norm_percent'].sum()

        # --- 平均成本 ---
        # 价格的加权平均值
        avg_cost = np.average(df_sorted['price'], weights=df_sorted['norm_percent'])

        # --- 辅助函数：求指定累积比例处的价格 ---
        def get_percentile_price(target_pct):
            # 寻找累积求和第一次大于等于目标百分比的行索引
            idx = df_sorted['cumsum'].searchsorted(target_pct)
            idx = min(idx, len(df_sorted) - 1) # 防止越界
            return df_sorted.loc[idx, 'price']

        # --- 90% 成本区与集中度 ---
        # 去头去尾各 5%
        cost_90_low = get_percentile_price(5)
        cost_90_high = get_percentile_price(95)
        if (cost_90_high + cost_90_low) != 0:
            concentration_90 = (cost_90_high - cost_90_low) / (cost_90_high + cost_90_low) * 100
        else:
            concentration_90 = 0.0
            
        # --- 70% 成本区与集中度 ---
        # 去头去尾各 15%
        cost_70_low = get_percentile_price(15)
        cost_70_high = get_percentile_price(85)
        if (cost_70_high + cost_70_low) != 0:
            concentration_70 = (cost_70_high - cost_70_low) / (cost_70_high + cost_70_low) * 100
        else:
            concentration_70 = 0.0

        # 返回格式化结果
        return {
            "获利比例": round(winner_rate/100, 4), # /100 与akshare保持一致，返回小数格式
            "平均成本": round(avg_cost, 4),
            "90成本-低": round(cost_90_low, 4),
            "90成本-高": round(cost_90_high, 4),
            "90集中度": round(concentration_90/100, 4),
            "70成本-低": round(cost_70_low, 4),
            "70成本-高": round(cost_70_high, 4),
            "70集中度": round(concentration_70/100, 4)
        }



if __name__ == "__main__":
    # 测试代码
    logging.basicConfig(level=logging.DEBUG)
    
    fetcher = TushareFetcher()
    
    try:
        # 测试历史数据
        df = fetcher.get_daily_data('600519')  # 茅台
        print(f"获取成功，共 {len(df)} 条数据")
        print(df.tail())
        
        # 测试股票名称
        name = fetcher.get_stock_name('600519')
        print(f"股票名称: {name}")
        
    except Exception as e:
        print(f"获取失败: {e}")

    # 测试市场统计
    print("\n" + "=" * 50)
    print("Testing get_market_stats (tushare)")
    print("=" * 50)
    try:
        stats = fetcher.get_market_stats()
        if stats:
            print(f"Market Stats successfully computed:")
            print(f"Up: {stats['up_count']} (Limit Up: {stats['limit_up_count']})")
            print(f"Down: {stats['down_count']} (Limit Down: {stats['limit_down_count']})")
            print(f"Flat: {stats['flat_count']}")
            print(f"Total Amount: {stats['total_amount']:.2f} 亿 (Yi)")
        else:
            print("Failed to compute market stats.")
    except Exception as e:
        print(f"Failed to compute market stats: {e}")


    # 测试筹码分布数据
    print("\n" + "=" * 50)
    print("测试筹码分布数据获取")
    print("=" * 50)
    try:
        chip = fetcher.get_chip_distribution('600519')  # 茅台
    except Exception as e:
        print(f"[筹码分布] 获取失败: {e}")

    # 测试行业板块排名
    print("\n" + "=" * 50)
    print("测试行业板块排名获取")
    print("=" * 50)
    try:
        rankings = fetcher.get_sector_rankings(n=5)
        if rankings:
            top, bottom = rankings
            print("涨幅榜 Top 5:")
            for sector in top:
                print(f"{sector['name']}: {sector['change_pct']}%")
            print("\n跌幅榜 Top 5:")
            for sector in bottom:
                print(f"{sector['name']}: {sector['change_pct']}%")
        else:
            print("未获取到行业板块排名数据")
    except Exception as e:
        print(f"[行业板块排名] 获取失败: {e}")
