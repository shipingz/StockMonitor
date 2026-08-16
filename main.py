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

import logging
import os
import random
import sys
import time
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Sequence, Tuple
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

# 企业微信 markdown 消息安全阈值（上限 4096 字节，留余量，ADR-0007）
MSG_SAFE_BYTES = int(os.environ.get("MSG_SAFE_BYTES", "3800"))

WEBHOOK_URL = os.environ.get("WECHAT_WEBHOOK_URL", "").strip()
STOCK_LIST_RAW = os.environ.get("STOCK_LIST", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").strip().lower() in ("1", "true", "yes", "on")
FORCE_RUN = os.environ.get("FORCE_RUN", "").strip().lower() in ("1", "true", "yes", "on")
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
# AKShare 数据抓取（ADR-0011：串行 + 随机限速 + 指数退避重试）
# ---------------------------------------------------------------------------

_last_request_time: Optional[float] = None


def enforce_rate_limit() -> None:
    """每次请求前强制限速：距上次请求不足 SLEEP_MIN 则补足，再随机 jitter SLEEP_MIN~SLEEP_MAX。"""
    global _last_request_time
    now_ts = time.time()
    if _last_request_time is not None:
        elapsed = now_ts - _last_request_time
        if elapsed < SLEEP_MIN:
            time.sleep(SLEEP_MIN - elapsed)
    time.sleep(random.uniform(SLEEP_MIN, SLEEP_MAX))
    _last_request_time = time.time()


def fetch_trade_calendar() -> Optional[set]:
    """全年交易日集合（date）。失败返回 None → 调用方按 ADR-0008 处理。"""
    try:
        import akshare as ak

        df = ak.tool_trade_date_hist_sina()
        return set(pd.to_datetime(df["trade_date"]).dt.date)
    except Exception as exc:
        logger.warning("交易日历获取失败: %s", exc)
        return None


def fetch_name_map() -> Dict[str, str]:
    """全市场 代码→名称 映射（1 次请求，ADR-0001）。失败返回空 dict，名称退化为代码。"""
    try:
        import akshare as ak

        df = ak.stock_info_a_code_name()
        return {str(row["code"]): str(row["name"]) for _, row in df.iterrows()}
    except Exception as exc:
        logger.warning("代码名称表获取失败，本轮名称将以代码显示: %s", exc)
        return {}


def fetch_15min_kline(symbol: str) -> Optional[pd.DataFrame]:
    """抓取单只股票 15 分钟K线（ADR-0011：最多 FETCH_ATTEMPTS 次，指数退避 min(2**attempt, 30)）。"""
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


def send_wechat_messages(contents: List[str]) -> None:
    """推送消息列表。dry_run 模式只打印不推送；推送失败记日志（ADR-0005 不重试不补偿）。"""
    for content in contents:
        if DRY_RUN:
            logger.info("[dry-run] 将推送（%d 字节）：\n%s", len(content.encode("utf-8")), content)
            continue
        if not WEBHOOK_URL:
            logger.error("未配置 WECHAT_WEBHOOK_URL，跳过推送")
            continue
        try:
            resp = requests.post(
                WEBHOOK_URL,
                json={"msgtype": "markdown", "markdown": {"content": content}},
                timeout=15,
            )
            data = resp.json()
            if data.get("errcode") != 0:
                logger.error("企业微信推送失败: HTTP %s %s", resp.status_code, data)
            else:
                logger.info("企业微信推送成功（%d 字节）", len(content.encode("utf-8")))
        except Exception as exc:
            logger.error("企业微信推送异常: %s", exc)


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
    """串行抓取全部自选股 15 分钟K线（ADR-0011）。返回 (成功 dict, 失败列表)。"""
    klines: Dict[str, pd.DataFrame] = {}
    failed_codes: List[str] = []
    for code in codes:
        df = fetch_15min_kline(code)
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
    if not WEBHOOK_URL and not DRY_RUN:
        logger.error("未配置 WECHAT_WEBHOOK_URL（GitHub Secrets），且非 dry-run，无法推送")
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
    name_map = fetch_name_map()
    missing_codes = [c for c in codes if c not in name_map]
    if missing_codes:
        alarms.append(f"代码表中未找到：{', '.join(missing_codes)}（名称将显示为代码）")

    # 3. 抓取（ADR-0011）
    klines, failed_codes = fetch_all_klines(codes)
    if not klines:
        alarms.append(f"本轮全部股票抓取失败（{len(codes)} 只），数据源可能故障")
        send_wechat_messages([alarm_message(alarms)])
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
        send_wechat_messages([alarm_message(alarms)])
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
    send_wechat_messages(contents)
    return 0


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main() -> int:
    setup_logging()

    # 复盘模式（ADR-0013）：非交易日用 workflow_dispatch 的 replay flag 触发
    if REPLAY:
        return run_replay()

    now = datetime.now(TZ_CN)
    today = now.date()
    now_naive = now.replace(tzinfo=None)

    logger.info("=" * 60)
    logger.info("A股 15分钟K线 MA%d 上穿监控启动 | 北京时间 %s", MA_PERIOD, now)
    logger.info("dry_run=%s force_run=%s 自选股=%s", DRY_RUN, FORCE_RUN, STOCK_LIST_RAW or "(未配置)")
    if not WEBHOOK_URL and not DRY_RUN:
        logger.error("未配置 WECHAT_WEBHOOK_URL（GitHub Secrets），且非 dry-run，本轮无法推送")
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
    name_map = fetch_name_map()
    missing_codes = [c for c in codes if c not in name_map]
    if missing_codes:
        alarms.append(f"代码表中未找到：{', '.join(missing_codes)}（名称将显示为代码）")

    # 4. 抓取 15 分钟K线（串行 + 限速 + 重试，ADR-0011）
    klines, failed_codes = fetch_all_klines(codes)
    if not klines:
        alarms.append(f"本轮全部股票抓取失败（{len(codes)} 只），数据源可能故障")
        send_wechat_messages([alarm_message(alarms)])
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
        send_wechat_messages([alarm_message(alarms)])
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

    send_wechat_messages(contents)
    return 0


if __name__ == "__main__":
    sys.exit(main())
