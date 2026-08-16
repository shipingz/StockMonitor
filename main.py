#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
A股 15分钟K线 MA60 上穿监控
=====================================
在 GitHub Actions 上定时运行（收盘后 3 分钟精确对齐，交易日 16 轮/天），
用 AKShare 抓取自选股 15 分钟K线，检测「收盘价上穿 MA60」信号，
通过企业微信机器人 Webhook 推送（信号 / 心跳 / 告警 三态消息）。

设计依据：CONTEXT.md 与 docs/adr/ADR-0001 ~ ADR-0012。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# ---------------------------------------------------------------------------
# 配置（全部从环境变量读取，默认值兜底；workflow 中由 vars/secrets 注入）
# ---------------------------------------------------------------------------

TZ_CN = ZoneInfo("Asia/Shanghai")

# A股 15 分钟K线收盘时刻（北京时间），每天 16 根
KLINE_CLOSE_TIMES: List[Tuple[int, int]] = [
    (9, 45), (10, 0), (10, 15), (10, 30), (10, 45),
    (11, 0), (11, 15), (11, 30),
    (13, 15), (13, 30), (13, 45), (14, 0), (14, 15), (14, 30), (14, 45),
    (15, 0),
]

# 护栏：最新已收盘K线距其收盘时刻超过该分钟数视为严重延迟（ADR-0003/0006）
STALE_THRESHOLD_MINUTES = int(os.environ.get("STALE_THRESHOLD_MINUTES", "15"))

# MA 周期（ADR-0002 口径1：MA 含当前K线；默认 60）
MA_PERIOD = int(os.environ.get("MA_PERIOD", "60"))

# 判定上穿所需最少K线数：MA_PERIOD 根算当前 MA + 1 根作上一根对比
MIN_BARS = MA_PERIOD + 1

# 自选股上限（软约束，ADR-0001：超过告警而非静默截断）
MAX_STOCKS = int(os.environ.get("MAX_STOCKS", "50"))

# 抓取限速与重试（ADR-0011：照抄 daily_stock_analysis——串行 + 随机限速 + 指数退避）
SLEEP_MIN = float(os.environ.get("FETCH_SLEEP_MIN", "2.0"))
SLEEP_MAX = float(os.environ.get("FETCH_SLEEP_MAX", "5.0"))
FETCH_ATTEMPTS = int(os.environ.get("FETCH_ATTEMPTS", "3"))

# 抓取并发线程数（ADR-0011 修订）：串行对 50 只需 3~5 分钟，实时价模式（ADR-0016）下不够。
# 安全优先：每线程独立限速（threading.local），总 QPS ≈ workers/3.5s；默认 3（总 QPS ~1.5，
# 新浪源实测远高于此承受力；东财仅兜底路径出现）。设 1 即回到纯串行。范围 1~8。
FETCH_WORKERS = max(1, min(8, int(os.environ.get("FETCH_WORKERS", "3"))))

# K线数据源优先级（ADR-0015 修订）：GitHub runner 上东财 IP 被封是确定性事实（nid 403 + 直连断连），
# 故默认新浪优先（实测稳定、200 根/次、无风控），东财降级为兜底。可设 KLINE_SOURCE_PRIORITY=sina,em 调整。
KLINE_SOURCE_PRIORITY = os.environ.get("KLINE_SOURCE_PRIORITY", "sina,em").strip().lower()

# 企业微信 markdown 消息安全阈值（上限 4096 字节，留余量，ADR-0007）
MSG_SAFE_BYTES = int(os.environ.get("MSG_SAFE_BYTES", "3800"))

WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "").strip()
STOCK_LIST_RAW = os.environ.get("STOCK_LIST", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
FORCE_RUN = os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes", "on")
# 飞书自定义机器人 Webhook（ADR-0014：多渠道推送，有哪个推哪个）
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
FEISHU_WEBHOOK_SECRET = os.environ.get("FEISHU_WEBHOOK_SECRET", "").strip()
# 复盘模式（ADR-0013）：计算最近一个完整交易日当天全部信号，非交易日验证用
REPLAY = os.environ.get("REPLAY", "").strip().lower() in ("1", "true", "yes", "on")
REPLAY_DATE = os.environ.get("REPLAY_DATE", "").strip()

# 中文星期
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

logger = logging.getLogger("stock-monitor")

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------


def setup_logging() -> None:
    """日志输出到 stdout（Actions 日志）+ logs/ 文件（上传 artifact）。

    Windows 控制台默认 GBK 无法编码 emoji，重配置 stdout 为 UTF-8 防崩溃；
    GitHub Actions 的 ubuntu runner 本身为 UTF-8，不受影响。
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"monitor_{datetime.now(TZ_CN):%Y%m%d}.log")
    handlers: List[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except OSError:  # 文件日志失败不阻断
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


# ---------------------------------------------------------------------------
# AKShare 数据抓取（ADR-0011：并发 + 每线程随机限速 + 指数退避重试）
# ---------------------------------------------------------------------------

# 每线程独立的限速状态：并发下各线程各自保持请求节奏（threading.local），
# 总 QPS ≈ FETCH_WORKERS / 平均限速间隔，由线程数控制（ADR-0011 修订）。
_thread_local = threading.local()

# 东方财富反爬补丁（ADR-0015）：云服务器 IP（GitHub Actions runner）直连东财会被风控断连
# （RemoteDisconnected），需先向 anonflow2.eastmoney.com 换取 nid 令牌，抓取时携带
# Cookie: nid18=<nid> + 随机浏览器 UA + 随机休眠。移植自参考项目 daily_stock_analysis
# 的 eastmoney_patch.py，UA 用内置列表替代 fake_useragent（避免额外网络依赖）。
EASTMONEY_PATCH = os.environ.get("EASTMONEY_PATCH", "1").strip().lower() in ("1", "true", "yes", "on")
EASTMONEY_TARGET_DOMAINS = (
    "fund.eastmoney.com",
    "push2.eastmoney.com",
    "push2his.eastmoney.com",
)
USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
)


def is_eastmoney_target(url: str) -> bool:
    return any(d in (url or "") for d in EASTMONEY_TARGET_DOMAINS)


def random_user_agent() -> str:
    return random.choice(USER_AGENTS)


_nid_cache: Dict[str, object] = {"data": None, "expire_at": 0.0}
_nid_lock = threading.Lock()


def _get_eastmoney_nid(user_agent: str) -> Optional[str]:
    """向 anonflow2.eastmoney.com 换取 nid 授权令牌（带 20s 成功缓存 / 5min 失败缓存）。

    失败时也缓存（data=None），避免 403 后每只股票每次尝试都重复请求被风控的接口。
    加锁：并发抓取（ADR-0011 修订）下仅首个线程请求令牌，其余线程等锁后命中缓存。
    """
    now = time.time()
    with _nid_lock:
        if now < _nid_cache["expire_at"]:
            return _nid_cache["data"]  # type: ignore[return-value]
        try:
            import hashlib
            import secrets as secrets_mod
            import uuid as uuid_mod

            def _uuid_md5() -> str:
                return hashlib.md5(str(uuid_mod.uuid4()).encode("utf-8")).hexdigest()

            def _st_nvi() -> str:
                charset = "useandom-26T198340PX75pxJACKVERYMINDBUSHWOLF_GQZbfghjklqvwyzrict"
                random_str = "".join(secrets_mod.choice(charset) for _ in range(21))
                return random_str + hashlib.sha256(random_str.encode("utf-8")).hexdigest()[:4]

            url = "https://anonflow2.eastmoney.com/backend/api/webreport"
            payload = json.dumps(
                {
                    "osPlatform": "Windows",
                    "sourceType": "WEB",
                    "osversion": "Windows 10.0",
                    "language": "zh-CN",
                    "timezone": "Asia/Shanghai",
                    "webDeviceInfo": {
                        "screenResolution": random.choice(("1920X1080", "2560X1440", "3840X2160")),
                        "userAgent": user_agent,
                        "canvasKey": _uuid_md5(),
                        "webglKey": _uuid_md5(),
                        "fontKey": _uuid_md5(),
                        "audioKey": _uuid_md5(),
                    },
                }
            )
            headers = {
                "Cookie": f"st_nvi={_st_nvi()}",
                "Content-Type": "application/json",
                # 浏览器特征头，降低 anonflow2 对无特征请求的 403 概率（ADR-0015）
                "Referer": "https://www.eastmoney.com/",
                "Origin": "https://www.eastmoney.com",
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
            resp = requests.post(url, headers=headers, data=payload, timeout=30)
            resp.raise_for_status()
            nid = resp.json()["data"]["nid"]
            _nid_cache["data"] = nid
            _nid_cache["expire_at"] = now + 20
            logger.info("东方财富 nid 令牌获取成功")
            return nid
        except Exception as exc:
            logger.warning("东方财富 nid 令牌获取失败（将无令牌直连，可能仍被风控）: %s", exc)
            _nid_cache["data"] = None
            _nid_cache["expire_at"] = now + 5 * 60
            return None


_original_session_request = requests.Session.request
_patch_applied = False


def apply_eastmoney_patch() -> None:
    """全局 monkeypatch requests.Session.request：仅对东财域名附加 UA + nid cookie + 随机休眠。"""
    global _patch_applied
    if _patch_applied:
        return
    if not EASTMONEY_PATCH:
        logger.info("EASTMONEY_PATCH=off，跳过东财反爬补丁")
        _patch_applied = True
        return

    def patched_request(self, method, url, **kwargs):
        if not is_eastmoney_target(url):
            return _original_session_request(self, method, url, **kwargs)
        user_agent = random_user_agent()
        headers = dict(kwargs.get("headers") or {})
        headers["User-Agent"] = user_agent
        nid = _get_eastmoney_nid(user_agent)
        if nid:
            headers["Cookie"] = f"nid18={nid}"
        kwargs["headers"] = headers
        time.sleep(random.uniform(1.0, 4.0))  # 随机休眠降低风控概率
        return _original_session_request(self, method, url, **kwargs)

    requests.Session.request = patched_request  # type: ignore[method-assign]
    _patch_applied = True
    logger.info("东方财富反爬补丁已启用（UA + nid18 cookie + 随机休眠）")


def enforce_rate_limit() -> None:
    """每线程独立限速：距该线程上次请求不足 SLEEP_MIN 则补足，再随机 jitter SLEEP_MIN~SLEEP_MAX。"""
    last = getattr(_thread_local, "last_request_time", None)
    now_ts = time.time()
    if last is not None:
        elapsed = now_ts - last
        if elapsed < SLEEP_MIN:
            time.sleep(SLEEP_MIN - elapsed)
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
    _thread_local.last_request_time = time.time()


def sina_symbol(code: str) -> str:
    """6 位代码 → 新浪符号：6 开头为沪市（sh），其余为深市（sz）。北交所不在支持范围（ADR-0001）。"""
    return f"sh{code}" if code.startswith("6") else f"sz{code}"


def parse_sina_name_response(text: str) -> Dict[str, str]:
    """解析 hq.sinajs.cn 响应（已按 GBK 解码）：'var hq_str_sh600519=\"贵州茅台,...\";' → {code: name}。"""
    result: Dict[str, str] = {}
    for line in (text or "").splitlines():
        m = re.match(r'var hq_str_(?:sh|sz)(\d{6})="([^,]*),', line)
        if m and m.group(2).strip():
            result[m.group(1)] = m.group(2).strip()
    return result


def parse_sina_kline_jsonp(text: str) -> Optional[pd.DataFrame]:
    """解析新浪分钟K线 JSONP：'var _data=[{\"day\":\"2026-08-14 10:00:00\",\"close\":\"1700.00\",...},...];'
    返回 时间/收盘 两列（时间升序）。解析失败返回 None。
    """
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    if not data:
        return None
    rows = [{"时间": item.get("day"), "收盘": item.get("close")} for item in data]
    df = pd.DataFrame(rows)
    if df.empty:
        return None
    df["时间"] = pd.to_datetime(df["时间"], errors="coerce")
    df["收盘"] = pd.to_numeric(df["收盘"], errors="coerce")
    df = df.dropna(subset=["时间", "收盘"]).sort_values("时间")
    return df if not df.empty else None


def fetch_trade_calendar() -> Optional[set]:
    """全年交易日集合（date）。失败返回 None → 调用方按 ADR-0008 处理。"""
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        return set(pd.to_datetime(df["trade_date"]).dt.date)
    except Exception as exc:
        logger.warning("交易日历获取失败: %s", exc)
        return None


def fetch_name_map(codes: List[str]) -> Dict[str, str]:
    """自选股 代码→名称 映射（ADR-0001/0015）。

    主用新浪批量接口（hq.sinajs.cn，一次请求查全部自选股，稳定且无全市场分页）；
    失败时 fallback 东财 stock_info_a_code_name（重试 FETCH_ATTEMPTS 次）。
    全部失败返回空 dict，名称退化为代码。
    """
    # 1) 新浪批量（只查自选股，一次请求）
    try:
        symbols = ",".join(sina_symbol(c) for c in codes)
        resp = requests.get(
            f"https://hq.sinajs.cn/list={symbols}",
            headers={"Referer": "https://finance.sina.com.cn"},
            timeout=15,
        )
        resp.encoding = "gbk"
        name_map = parse_sina_name_response(resp.text)
        if name_map:
            logger.info("名称映射：新浪批量接口成功（%d/%d 只）", len(name_map), len(codes))
            return name_map
        logger.warning("新浪名称接口返回为空（HTTP %s），尝试东财 fallback", resp.status_code)
    except Exception as exc:
        logger.warning("新浪名称接口异常: %s，尝试东财 fallback", exc)

    # 2) 东财全市场（带重试；akshare 内部 tqdm 分页，仅 fallback 时触发）
    import akshare as ak

    last_exc: Optional[Exception] = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            enforce_rate_limit()
            df = ak.stock_info_a_code_name()
            if df is None or df.empty:
                raise RuntimeError("返回空数据")
            full = {str(row["code"]): str(row["name"]) for _, row in df.iterrows()}
            return {c: full[c] for c in codes if c in full}
        except Exception as exc:
            last_exc = exc
            wait = min(2**attempt, 30)
            logger.warning(
                "东财代码名称表获取失败（尝试 %d/%d）: %s，%d 秒后重试",
                attempt, FETCH_ATTEMPTS, exc, wait,
            )
            if attempt < FETCH_ATTEMPTS:
                time.sleep(wait)
    logger.error("代码名称表获取最终失败: %s", last_exc)
    return {}


def _fetch_15min_kline_em(symbol: str) -> Optional[pd.DataFrame]:
    """东财源 15 分钟K线（ADR-0011：最多 FETCH_ATTEMPTS 次，指数退避 min(2**attempt, 30)）。"""
    import akshare as ak

    last_exc: Optional[Exception] = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            enforce_rate_limit()
            df = ak.stock_zh_a_hist_min_em(
                symbol=symbol,
                start_date="2020-01-01 09:30:00",
                end_date="2222-01-01 15:00:00",
                period="15",
                adjust="",
            )
            if df is None or df.empty:
                raise RuntimeError("返回空数据")
            return df
        except Exception as exc:
            last_exc = exc
            wait = min(2**attempt, 30)
            logger.warning(
                "抓取 %s 15分钟K线失败（尝试 %d/%d）: %s，%d 秒后重试",
                symbol, attempt, FETCH_ATTEMPTS, exc, wait,
            )
            if attempt < FETCH_ATTEMPTS:
                time.sleep(wait)
    logger.error("抓取 %s 15分钟K线最终失败: %s", symbol, last_exc)
    return None


def fetch_15min_kline_sina(symbol: str) -> Optional[pd.DataFrame]:
    """新浪源 15 分钟K线直连（ADR-0015 fallback）：quotes.sina.cn getKLineData，datalen=200。
    返回 时间/收盘 两列。失败返回 None。
    """
    url = (
        "https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_data=/CN_MarketDataService.getKLineData"
        f"?symbol={sina_symbol(symbol)}&scale=15&ma=no&datalen=200"
    )
    last_exc: Optional[Exception] = None
    for attempt in range(1, FETCH_ATTEMPTS + 1):
        try:
            enforce_rate_limit()
            resp = requests.get(
                url,
                headers={"Referer": "https://finance.sina.com.cn"},
                timeout=15,
            )
            resp.raise_for_status()
            df = parse_sina_kline_jsonp(resp.text)
            if df is None:
                raise RuntimeError("解析为空")
            return df
        except Exception as exc:
            last_exc = exc
            wait = min(2**attempt, 30)
            logger.warning(
                "新浪 %s 15分钟K线失败（尝试 %d/%d）: %s，%d 秒后重试",
                symbol, attempt, FETCH_ATTEMPTS, exc, wait,
            )
            if attempt < FETCH_ATTEMPTS:
                time.sleep(wait)
    logger.error("新浪 %s 15分钟K线最终失败: %s", symbol, last_exc)
    return None


def fetch_15min_kline(symbol: str) -> Optional[pd.DataFrame]:
    """抓取单只股票 15 分钟K线，按 KLINE_SOURCE_PRIORITY 顺序尝试数据源（ADR-0015）。

    默认 'sina,em'：新浪优先（GitHub runner 上东财被封是确定性事实），东财兜底。
    """
    sources = {
        "sina": ("新浪", fetch_15min_kline_sina),
        "em": ("东财", _fetch_15min_kline_em),
    }
    order = [s.strip() for s in KLINE_SOURCE_PRIORITY.split(",") if s.strip() in sources]
    if not order:  # 非法配置兜底
        order = ["sina", "em"]
    for idx, name in enumerate(order):
        source_name, fetcher = sources[name]
        logger.info("尝试 %s 源抓取 %s（优先级 %d/%d）", source_name, symbol, idx + 1, len(order))
        df = fetcher(symbol)
        if df is not None:
            return df
        if idx < len(order) - 1:
            logger.warning("%s 源抓取 %s 失败，切换下一数据源", source_name, symbol)
    return None


# ---------------------------------------------------------------------------
# 纯计算逻辑（可单元测试，不依赖网络）
# ---------------------------------------------------------------------------


def parse_stock_list(raw: str) -> List[str]:
    """解析逗号分隔的股票代码列表（ADR-0009：纯 6 位数字）。非法项忽略并告警日志。"""
    codes: List[str] = []
    for part in (raw or "").split(","):
        code = part.strip()
        if not code:
            continue
        if not (code.isdigit() and len(code) == 6):
            logger.warning("忽略非法股票代码: %r（需 6 位数字）", code)
            continue
        codes.append(code)
    return codes


def compute_ma_signal(closes: Sequence[float], period: int = MA_PERIOD) -> Optional[dict]:
    """
    ADR-0002 触发语义（A型上穿 + MA 口径1）：
      cur_ma  = 最近 period 根收盘价均值（含当前K线）
      prev_ma = 上一时刻的 MA（前 period 根，含上一根、不含当前）
      上穿    = cur_close > cur_ma 且 prev_close <= prev_ma
    返回 dict；历史不足 period+1 根返回 None（调用方跳过该股，ADR-0004）。
    """
    closes = [float(c) for c in closes]
    if len(closes) < period + 1:
        return None
    cur_close = closes[-1]
    prev_close = closes[-2]
    cur_ma = sum(closes[-period:]) / period
    prev_ma = sum(closes[-(period + 1):-1]) / period
    cross_above = cur_close > cur_ma and prev_close <= prev_ma
    deviation_pct = (cur_close - cur_ma) / cur_ma * 100 if cur_ma else 0.0
    return {
        "cur_close": cur_close,
        "prev_close": prev_close,
        "cur_ma": cur_ma,
        "prev_ma": prev_ma,
        "cross_above": cross_above,
        "deviation_pct": deviation_pct,
    }


def format_price(value: float) -> str:
    return f"{value:,.2f}"


def build_signal_block(symbol: str, name: str, kline_time: datetime, sig: dict) -> str:
    """单条信号的消息块（ADR-0007 字段清单）。"""
    return "\n".join(
        [
            f"**{name}（{symbol}）**｜K线 {kline_time:%H:%M}",
            (
                f"收盘价：{format_price(sig['cur_close'])}"
                f" ｜ MA{MA_PERIOD}：{format_price(sig['cur_ma'])}"
                f" ｜ 偏离：{sig['deviation_pct']:+.2f}%"
            ),
            "**上穿信号**：收盘价上穿 MA%d" % MA_PERIOD,
        ]
    )


def pack_signal_messages(
    blocks: List[str],
    limit: int = MSG_SAFE_BYTES,
    header: Optional[str] = None,
) -> List[str]:
    """ADR-0007 超限拆分：贪心打包，条数尽量少；按 UTF-8 字节计算。header 可定制（复盘复用）。"""
    if header is None:
        header = f"## 📈 MA{MA_PERIOD} 上穿信号（15分钟K线）"
    messages: List[str] = []
    current: List[str] = []

    def text_of(bs: List[str]) -> str:
        return header + "\n\n" + "\n\n".join(bs)

    for block in blocks:
        trial = current + [block]
        if len(text_of(trial).encode("utf-8")) > limit and current:
            messages.append(text_of(current))
            current = [block]
        else:
            current = trial
    if current:
        messages.append(text_of(current))
    return messages


def heartbeat_message(kline_time: datetime, checked: int, signal_count: int) -> str:
    """ADR-0010 心跳：无信号时推送「系统正常」确认。"""
    return f"✅ 系统正常 | K线 {kline_time:%H:%M} | 检查 {checked} 只 | {signal_count} 信号"


def alarm_message(alarms: List[str]) -> str:
    """ADR-0008 告警：合并为一条。"""
    return "## ⚠️ 监控异常\n\n" + "\n".join(f"- {a}" for a in alarms)


def send_wechat_message(content: str) -> bool:
    """推送单条消息到企业微信机器人（ADR-0007：markdown）。失败记日志，返回是否成功。"""
    try:
        resp = requests.post(
            WEBHOOK_URL,
            json={"msgtype": "markdown", "markdown": {"content": content}},
            timeout=15,
        )
        data = resp.json()
        if data.get("errcode") != 0:
            logger.error("企业微信推送失败: HTTP %s %s", resp.status_code, data)
            return False
        logger.info("企业微信推送成功（%d 字节）", len(content.encode("utf-8")))
        return True
    except Exception as exc:
        logger.error("企业微信推送异常: %s", exc)
        return False


def extract_feishu_card_title(content: str) -> Tuple[str, str]:
    """提取首行 '## xxx' 作为飞书卡片标题，返回 (title, body)；无标题时 title 为空。"""
    lines = content.splitlines()
    if lines and re.match(r"^##\s+", lines[0]):
        title = re.sub(r"^##\s+", "", lines[0]).strip()
        return title, "\n".join(lines[1:]).strip()
    return "", content.strip()


def format_feishu_markdown(content: str) -> str:
    """把企业微信风格 markdown 转成飞书 lark_md 兼容格式（参考 daily_stock_analysis 简化版）：
    - '#/##/### 标题' → '**标题**'（lark_md 不支持 # 标题语法）
    - '- 列表项' → '• 列表项'
    其余（**加粗**、emoji、换行）lark_md 原生支持。
    """
    lines = []
    for raw_line in content.splitlines():
        line = raw_line.rstrip()
        if re.match(r"^#{1,6}\s+", line):
            title = re.sub(r"^#{1,6}\s+", "", line).strip()
            line = f"**{title}**" if title else ""
        elif line.startswith("- "):
            line = f"• {line[2:].strip()}"
        lines.append(line)
    return "\n".join(lines).strip()


def build_feishu_sign(timestamp: str) -> str:
    """飞书机器人加签（FEISHU_WEBHOOK_SECRET）：HMAC-SHA256(timestamp\\nsecret) → base64。"""
    string_to_sign = f"{timestamp}\n{FEISHU_WEBHOOK_SECRET}"
    return base64.b64encode(
        hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
    ).decode("utf-8")


def _feishu_post(payload: dict) -> bool:
    """POST 飞书 webhook，附加加签字段（若配置了 FEISHU_WEBHOOK_SECRET）。"""
    body = dict(payload)
    if FEISHU_WEBHOOK_SECRET:
        timestamp = str(int(time.time()))
        body["timestamp"] = timestamp
        body["sign"] = build_feishu_sign(timestamp)
    resp = requests.post(FEISHU_WEBHOOK_URL, json=body, timeout=15)
    data = resp.json()
    code = data.get("code", data.get("StatusCode"))
    if code == 0:
        logger.info("飞书推送成功（%d 字节）", len(str(payload).encode("utf-8")))
        return True
    logger.error(
        "飞书推送失败: HTTP %s code=%s msg=%s",
        resp.status_code,
        code,
        data.get("msg") or data.get("StatusMessage") or "未知错误",
    )
    return False


def send_feishu_message(content: str) -> bool:
    """推送单条消息到飞书机器人（ADR-0014）：interactive 卡片（lark_md）优先，text 降级。"""
    title, body = extract_feishu_card_title(content)
    card: dict = {
        "config": {"wide_screen_mode": True},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": format_feishu_markdown(body)}}
        ],
    }
    if title:
        card["header"] = {"title": {"tag": "plain_text", "content": title}}
    try:
        if _feishu_post({"msg_type": "interactive", "card": card}):
            return True
        # 降级：纯文本
        return _feishu_post({"msg_type": "text", "content": {"text": content}})
    except Exception as exc:
        logger.error("飞书推送异常: %s", exc)
        return False


def enabled_channels() -> List[Tuple[str, Callable[[str], bool]]]:
    """已配置的推送渠道列表（ADR-0014：有哪个推哪个，都有都推）。"""
    channels: List[Tuple[str, Callable[[str], bool]]] = []
    if WEBHOOK_URL:
        channels.append(("企业微信", send_wechat_message))
    if FEISHU_WEBHOOK_URL:
        channels.append(("飞书", send_feishu_message))
    return channels


def send_messages(contents: List[str]) -> None:
    """推送消息列表到全部已配置渠道。dry_run 只打印不推送；推送失败记日志（ADR-0005 不重试）。"""
    for content in contents:
        channels = enabled_channels()
        if DRY_RUN:
            names = "、".join(name for name, _ in channels) if channels else "无（未配置任何 webhook）"
            logger.info("[dry-run] 将推送（%d 字节，渠道: %s）：\n%s", len(content.encode("utf-8")), names, content)
            continue
        if not channels:
            logger.error("未配置任何推送渠道（WECHAT_WEBHOOK_URL / FEISHU_WEBHOOK_URL），跳过")
            continue
        for name, sender in channels:
            sender(content)


# ---------------------------------------------------------------------------
# 复盘模式（ADR-0013）：非交易日用 flag 触发，回放最近一个完整交易日当天全部信号
# ---------------------------------------------------------------------------


def weekday_cn(d: date) -> str:
    return WEEKDAY_CN[d.weekday()]


def parse_replay_date(raw: str) -> Optional[date]:
    """解析显式复盘日期 YYYY-MM-DD；空/非法返回 None。"""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def resolve_replay_date(calendar: set, now: datetime) -> Optional[date]:
    """ADR-0013 决策点①A：今天是交易日且已收盘（北京时间 >=15:00）→ 回放今天；
    否则回放今天之前最近的交易日。找不到返回 None。"""
    today = now.date()
    if today in calendar and (now.hour, now.minute) >= (15, 0):
        return today
    d = today - timedelta(days=1)
    for _ in range(60):  # 最多往前找 60 天，防死循环
        if d in calendar:
            return d
        d -= timedelta(days=1)
    return None


def fetch_all_klines(codes: List[str]) -> Tuple[Dict[str, pd.DataFrame], List[str]]:
    """抓取全部自选股 15 分钟K线（ADR-0011 修订：FETCH_WORKERS 线程并发，默认 3）。

    每线程独立限速（enforce_rate_limit 用 threading.local），总 QPS 由线程数控制；
    FETCH_WORKERS=1 时退化为纯串行。返回 (成功 dict, 失败列表)。
    """
    klines: Dict[str, pd.DataFrame] = {}
    failed_codes: List[str] = []
    if FETCH_WORKERS <= 1 or len(codes) <= 1:
        for code in codes:
            df = fetch_15min_kline(code)
            if df is None:
                failed_codes.append(code)
            else:
                klines[code] = df
    else:
        with ThreadPoolExecutor(max_workers=FETCH_WORKERS) as executor:
            future_map = {executor.submit(fetch_15min_kline, code): code for code in codes}
            for future in as_completed(future_map):
                code = future_map[future]
                try:
                    df = future.result()
                except Exception as exc:
                    logger.error("%s 抓取线程异常: %s", code, exc)
                    df = None
                if df is None:
                    failed_codes.append(code)
                else:
                    klines[code] = df
    if failed_codes:
        logger.warning("单股抓取失败（ADR-0008 静默跳过）: %s", ", ".join(failed_codes))
    return klines, failed_codes


def replay_signals_for_stock(
    df: pd.DataFrame, target: date, code: str, name: str
) -> Tuple[List[dict], List[str]]:
    """对目标日当天每个收盘时刻逐根判定上穿（只用 t 之前的K线，不用未来数据）。

    返回 (signals, detail_lines)：signals 为命中列表（含 code/name/kline_time/sig），
    detail_lines 为全天 16 根明细日志（HH:MM close=.. ma=.. cross=Y/N）。
    目标日无数据 → ([], [])。
    """
    df = df.copy()
    df["时间"] = pd.to_datetime(df["时间"])
    df = df.sort_values("时间")
    day_df = df[df["时间"].dt.date == target]
    if day_df.empty:
        return [], []

    closes = pd.to_numeric(df["收盘"], errors="coerce")
    signals: List[dict] = []
    details: List[str] = []
    for ts in day_df["时间"]:
        sub_closes = closes[df["时间"] <= ts].dropna().tolist()
        sig = compute_ma_signal(sub_closes)
        if sig is None:
            details.append(f"{ts:%H:%M} close=--- ma=--- cross=N（历史不足）")
            continue
        cross = sig["cross_above"]
        details.append(
            f"{ts:%H:%M} close={sig['cur_close']:.2f} ma={sig['cur_ma']:.2f} cross={'Y' if cross else 'N'}"
        )
        if cross:
            signals.append(
                {
                    "code": code,
                    "name": name,
                    "kline_time": ts.to_pydatetime(),
                    "sig": sig,
                }
            )
    return signals, details


def build_replay_signal_block(symbol: str, name: str, kline_time: datetime, sig: dict) -> str:
    """复盘消息的单条信号块（ADR-0013：去掉偏离字段）。"""
    return "\n".join(
        [
            f"**{name}（{symbol}）**｜K线 {kline_time:%H:%M}",
            f"收盘价：{format_price(sig['cur_close'])} ｜ MA{MA_PERIOD}：{format_price(sig['cur_ma'])}",
            "**上穿信号**：收盘价上穿 MA%d" % MA_PERIOD,
        ]
    )


def build_replay_message(target: date, checked: int, signals: List[dict]) -> List[str]:
    """复盘消息：无信号给汇总行；有信号给信号列表（复用贪心拆分）。"""
    header = f"## 📊 信号复盘：{target:%Y-%m-%d}（{weekday_cn(target)}）"
    if not signals:
        return [f"{header}\n\n当日无上穿信号（检查 {checked} 只 × 16 根K线）"]
    signals = sorted(signals, key=lambda s: s["kline_time"])
    blocks = [
        build_replay_signal_block(s["code"], s["name"], s["kline_time"], s["sig"])
        for s in signals
    ]
    return pack_signal_messages(blocks, header=header)


def run_replay() -> int:
    """复盘模式主流程：确定目标日 → 抓取 → 逐根回放 → 推送复盘消息。"""
    logger.info("=" * 60)
    logger.info("信号复盘模式启动 | 北京时间 %s", datetime.now(TZ_CN))
    logger.info("dry_run=%s 显式日期=%s", DRY_RUN, REPLAY_DATE or "(自动)")
    if not WEBHOOK_URL and not FEISHU_WEBHOOK_URL and not DRY_RUN:
        logger.error("未配置 WECHAT_WEBHOOK_URL / FEISHU_WEBHOOK_URL（GitHub Secrets），且非 dry-run，无法推送")
        return 1

    alarms: List[str] = []

    # 1. 确定目标日（ADR-0013 决策点①）
    target: Optional[date] = None
    if REPLAY_DATE:
        target = parse_replay_date(REPLAY_DATE)
        if target is None:
            logger.error("REPLAY_DATE 格式非法（需 YYYY-MM-DD）: %r", REPLAY_DATE)
            return 1
        logger.info("使用显式复盘日期: %s（%s）", target, weekday_cn(target))
    else:
        calendar = fetch_trade_calendar()
        if calendar is None:
            logger.error("交易日历获取失败，无法自动确定复盘目标日，退出（可显式指定 REPLAY_DATE）")
            return 1
        now = datetime.now(TZ_CN)
        target = resolve_replay_date(calendar, now)
        if target is None:
            logger.error("无法确定复盘目标日（交易日历中无可用日期），退出")
            return 1
        logger.info("自动确定复盘日期: %s（%s）", target, weekday_cn(target))

    # 2. 自选股解析与名称映射（同实时路径）
    codes = parse_stock_list(STOCK_LIST_RAW)
    if not codes:
        logger.error("STOCK_LIST 为空或全部非法，退出")
        return 1
    if len(codes) > MAX_STOCKS:
        alarms.append(f"自选股数量 {len(codes)} 超过上限 {MAX_STOCKS}，继续全量处理")
    name_map = fetch_name_map(codes)
    missing_codes = [c for c in codes if c not in name_map]
    if missing_codes:
        alarms.append(f"代码表中未找到：{', '.join(missing_codes)}（名称将显示为代码）")

    # 3. 抓取（ADR-0011）
    klines, failed_codes = fetch_all_klines(codes)
    if not klines:
        alarms.append(f"本轮全部股票抓取失败（{len(codes)} 只），数据源可能故障")
        send_messages([alarm_message(alarms)])
        return 0

    # 4. 逐根回放目标日（ADR-0013：只用目标日及之前的数据，不用未来数据）
    now_naive = datetime.now(TZ_CN).replace(tzinfo=None)
    all_signals: List[dict] = []
    checked = 0
    missing_day_codes: List[str] = []
    for code, df in klines.items():
        df = df.copy()
        df["时间"] = pd.to_datetime(df["时间"])
        df = df[df["时间"] <= now_naive].sort_values("时间")
        if df.empty:
            continue
        signals, details = replay_signals_for_stock(df, target, code, name_map.get(code, code))
        if not details:
            missing_day_codes.append(code)
            continue
        checked += 1
        for line in details:
            logger.info("[复盘] %s %s | %s", code, name_map.get(code, code), line)
        all_signals.extend(signals)

    if checked == 0:
        alarms.append(
            f"目标日 {target} 无任何股票的K线数据（可能超出近 5 个交易日保留范围，ADR-0004）"
        )
        send_messages([alarm_message(alarms)])
        return 0
    if missing_day_codes:
        logger.warning("以下股票目标日无数据，跳过（ADR-0008 静默）: %s", ", ".join(missing_day_codes))
    logger.info("复盘完成：%d 只股票，%d 个上穿信号", checked, len(all_signals))

    # 5. 消息（ADR-0013 决策点②：推送复盘消息；dry_run 时只打印）
    contents = build_replay_message(target, checked, all_signals)
    if alarms:
        alarm_text = alarm_message(alarms)
        merged = contents[-1] + "\n\n" + alarm_text
        if len(merged.encode("utf-8")) <= MSG_SAFE_BYTES:
            contents[-1] = merged
        else:
            contents.append(alarm_text)
    send_messages(contents)
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    setup_logging()
    apply_eastmoney_patch()  # ADR-0015：东财反爬补丁（UA + nid18 cookie），GitHub runner 必开

    # 复盘模式（ADR-0013）：非交易日用 workflow_dispatch 的 replay flag 触发
    if REPLAY:
        return run_replay()

    now = datetime.now(TZ_CN)
    today = now.date()
    now_naive = now.replace(tzinfo=None)

    logger.info("=" * 60)
    logger.info("A股 15分钟K线 MA%d 上穿监控启动 | 北京时间 %s", MA_PERIOD, now)
    logger.info("dry_run=%s force_run=%s 自选股=%s", DRY_RUN, FORCE_RUN, STOCK_LIST_RAW or "(未配置)")
    if not WEBHOOK_URL and not FEISHU_WEBHOOK_URL and not DRY_RUN:
        logger.error("未配置 WECHAT_WEBHOOK_URL / FEISHU_WEBHOOK_URL（GitHub Secrets），且非 dry-run，无法推送")
        return 1

    alarms: List[str] = []

    # 1. 交易日历（ADR-0006：非交易日直接退出；日历失败按交易日继续 + 告警）
    if not FORCE_RUN:
        calendar = fetch_trade_calendar()
        if calendar is None:
            alarms.append("交易日历获取失败，按交易日继续运行")
        elif today not in calendar:
            logger.info("今天 %s 非交易日，退出（0 推送）", today)
            return 0
    else:
        logger.info("force_run 模式，跳过交易日历检查")

    # 2. 自选股解析（ADR-0001/0009）
    codes = parse_stock_list(STOCK_LIST_RAW)
    if not codes:
        logger.error("STOCK_LIST 为空或全部非法，退出")
        return 1
    if len(codes) > MAX_STOCKS:
        alarms.append(f"自选股数量 {len(codes)} 超过上限 {MAX_STOCKS}，继续全量处理")

    # 3. 名称映射（ADR-0001：查不到的代码告警，名称退化为代码）
    name_map = fetch_name_map(codes)
    missing_codes = [c for c in codes if c not in name_map]
    if missing_codes:
        alarms.append(f"代码表中未找到：{', '.join(missing_codes)}（名称将显示为代码）")

    # 4. 抓取 15 分钟K线（串行 + 限速 + 重试，ADR-0011）
    klines, failed_codes = fetch_all_klines(codes)
    if not klines:
        alarms.append(f"本轮全部股票抓取失败（{len(codes)} 只），数据源可能故障")
        send_messages([alarm_message(alarms)])
        return 0

    # 5. 护栏：最新已收盘K线日期与新鲜度（ADR-0006 修订版，保证 15:03 轮不被误杀）
    cleaned: Dict[str, pd.DataFrame] = {}
    latest_times: List[pd.Timestamp] = []
    for code, df in klines.items():
        df = df.copy()
        df["时间"] = pd.to_datetime(df["时间"])
        df = df[df["时间"] <= now_naive].sort_values("时间")
        if df.empty:
            continue
        cleaned[code] = df
        latest_times.append(df["时间"].iloc[-1])
    if not cleaned:
        logger.info("所有股票均无已收盘K线（今天首根K线 9:45 尚未收盘或数据未更新），退出")
        return 0
    latest = max(latest_times)
    if latest.date() != today:
        logger.info("最新已收盘K线 %s 非今天，退出（今天首根K线尚未收盘）", latest)
        return 0
    age_minutes = (now_naive - latest).total_seconds() / 60.0
    if age_minutes > STALE_THRESHOLD_MINUTES:
        alarms.append(
            f"严重延迟：最新已收盘K线 {latest:%H:%M} 已收盘 {age_minutes:.0f} 分钟"
            f"（> {STALE_THRESHOLD_MINUTES} 分钟），本轮跳过"
        )
        send_messages([alarm_message(alarms)])
        return 0
    logger.info("最新已收盘K线: %s（距今 %.0f 分钟），正常处理", latest, age_minutes)

    # 6. 信号判定（ADR-0002：A型上穿 + MA 口径1）
    signals: List[Tuple[str, str, datetime, dict]] = []  # (code, name, kline_time, sig)
    skipped_codes: List[str] = []
    for code, df in cleaned.items():
        try:
            closes = pd.to_numeric(df["收盘"], errors="coerce").dropna().tolist()
        except Exception as exc:
            logger.warning("%s 收盘价列解析失败，跳过: %s", code, exc)
            skipped_codes.append(code)
            continue
        sig = compute_ma_signal(closes)
        if sig is None:
            logger.info("%s 历史K线不足 %d 根（实际 %d 根），跳过（ADR-0004）", code, MIN_BARS, len(closes))
            skipped_codes.append(code)
            continue
        if sig["cross_above"]:
            kline_time = df["时间"].iloc[-1].to_pydatetime()
            signals.append((code, name_map.get(code, code), kline_time, sig))
    logger.info(
        "检查 %d 只，上穿信号 %d 个，跳过 %d 只（数据不足/解析失败）",
        len(cleaned), len(signals), len(skipped_codes),
    )

    # 7. 消息（ADR-0010 三态互斥 + ADR-0007 拆分；ADR-0008 告警并入）
    contents: List[str] = []
    if signals:
        blocks = [build_signal_block(code, name, kt, sig) for code, name, kt, sig in signals]
        contents = pack_signal_messages(blocks)
    else:
        contents = [heartbeat_message(latest.to_pydatetime(), len(cleaned), 0)]

    if alarms:
        alarm_text = alarm_message(alarms)
        merged = contents[-1] + "\n\n" + alarm_text
        if len(merged.encode("utf-8")) <= MSG_SAFE_BYTES:
            contents[-1] = merged
        else:
            contents.append(alarm_text)

    send_messages(contents)
    return 0


if __name__ == "__main__":
    sys.exit(main())
