# -*- coding: utf-8 -*-
"""纯逻辑单元测试（不依赖网络 / akshare）：MA60 计算、上穿判定、消息构建与拆分、复盘模式。

运行：python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pandas as pd

sys.path.insert(0, ".")
from main import (  # noqa: E402
    KLINE_CLOSE_TIMES,
    MA_PERIOD,
    build_replay_message,
    build_signal_block,
    compute_ma_signal,
    extract_feishu_card_title,
    format_feishu_markdown,
    heartbeat_message,
    is_eastmoney_target,
    pack_signal_messages,
    parse_replay_date,
    parse_sina_kline_jsonp,
    parse_sina_name_response,
    parse_stock_list,
    random_user_agent,
    replay_signals_for_stock,
    resolve_replay_date,
    sina_symbol,
)

TZ_CN = ZoneInfo("Asia/Shanghai")


def make_closes(last: float, prev: float, n: int = 80, base: float = 10.0, drift: float = 0.01) -> list:
    """构造 n 根K线收盘价序列：前 n-2 根从 base 缓慢上行，最后两根为 prev、last。"""
    closes = [base + i * drift for i in range(n - 2)]
    closes.append(prev)
    closes.append(last)
    return closes


def make_replay_df(target: date, jump_at: str = "11:00", prev_days: int = 4) -> pd.DataFrame:
    """构造含目标日在内的连续 prev_days+1 天 × 16 根K线 DataFrame。

    价格缓慢上行；目标日 jump_at 前一根压低（位于 MA 下方），jump_at 根跳升（上穿），
    之后维持高位 → A型触发应只报 jump_at 这一次。
    """
    rows: list = []
    base = 10.0
    jump_h, jump_m = (int(x) for x in jump_at.split(":"))
    for day_offset in range(prev_days, -1, -1):
        d = target - timedelta(days=day_offset)
        for h, m in KLINE_CLOSE_TIMES:
            ts = datetime.combine(d, time(h, m))
            if d == target and (h, m) == (jump_h, jump_m):
                rows.append((ts, 12.5))  # 跳升
            elif d == target and (h, m) == KLINE_CLOSE_TIMES[KLINE_CLOSE_TIMES.index((jump_h, jump_m)) - 1]:
                rows.append((ts, 9.5))  # 前一根压低
            else:
                rows.append((ts, base))
            base += 0.02
    return pd.DataFrame(rows, columns=["时间", "收盘"])


class TestComputeMaSignal(unittest.TestCase):
    def test_cross_above_detected(self):
        # 上一根在 MA 下方、当前在 MA 上方 → 上穿
        closes = make_closes(last=11.5, prev=9.5)
        sig = compute_ma_signal(closes)
        self.assertIsNotNone(sig)
        self.assertTrue(sig["cross_above"])
        self.assertGreater(sig["cur_close"], sig["cur_ma"])
        self.assertLessEqual(sig["prev_close"], sig["prev_ma"])

    def test_no_cross_when_above(self):
        # 持续在 MA 上方（上一根也在上方）→ 不上穿（A型触发，ADR-0002）
        closes = make_closes(last=12.0, prev=11.8)
        sig = compute_ma_signal(closes)
        self.assertIsNotNone(sig)
        self.assertFalse(sig["cross_above"])

    def test_no_cross_when_below(self):
        # 持续在 MA 下方 → 不上穿
        closes = make_closes(last=9.0, prev=8.8)
        sig = compute_ma_signal(closes)
        self.assertIsNotNone(sig)
        self.assertFalse(sig["cross_above"])

    def test_insufficient_data_returns_none(self):
        # 历史不足 MA_PERIOD+1 根 → None（ADR-0004 跳过该股）
        closes = [10.0] * MA_PERIOD  # 恰好 60 根，不够算上一根 MA
        self.assertIsNone(compute_ma_signal(closes))
        closes = [10.0] * (MA_PERIOD - 5)
        self.assertIsNone(compute_ma_signal(closes))

    def test_ma_uses_period_including_current(self):
        # 口径1：cur_ma 含当前K线（ADR-0002）
        closes = make_closes(last=100.0, prev=10.0, n=70, base=10.0)
        sig = compute_ma_signal(closes)
        self.assertIsNotNone(sig)
        self.assertAlmostEqual(sig["cur_ma"], sum(closes[-MA_PERIOD:]) / MA_PERIOD)
        self.assertAlmostEqual(sig["prev_ma"], sum(closes[-(MA_PERIOD + 1):-1]) / MA_PERIOD)


class TestParseStockList(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(parse_stock_list("600519,000001, 300750 ,"), ["600519", "000001", "300750"])

    def test_invalid_ignored(self):
        self.assertEqual(parse_stock_list("600519,abc,12345,1234567"), ["600519"])

    def test_empty(self):
        self.assertEqual(parse_stock_list(""), [])
        self.assertEqual(parse_stock_list(" , ,"), [])


class TestMessagePacking(unittest.TestCase):
    def test_single_message_within_limit(self):
        blocks = [f"**股票{i}（00000{i}）**｜K线 10:00\n收盘价：10.00 ｜ MA60：9.90 ｜ 偏离：+1.01%" for i in range(3)]
        msgs = pack_signal_messages(blocks)
        self.assertEqual(len(msgs), 1)

    def test_split_when_over_limit(self):
        blocks = [f"**股票{i:04d}（00000{i}）**｜K线 10:00\n收盘价：10.00 ｜ MA60：9.90 ｜ 偏离：+1.01%\n**上穿信号**" for i in range(60)]
        msgs = pack_signal_messages(blocks, limit=1500)
        self.assertGreater(len(msgs), 1)
        for m in msgs:
            self.assertLessEqual(len(m.encode("utf-8")), 1500)

    def test_greedy_minimal_count(self):
        # 每块 ~100 字节，limit 1500：60 块应拆成 ceil(60*100/1500)=4~5 条（贪心接近最优）
        blocks = [f"**股票{i:04d}**｜K线 10:00\n收盘价：10.00 ｜ MA60：9.90 ｜ 偏离：+1.01%" for i in range(60)]
        msgs = pack_signal_messages(blocks, limit=1500)
        self.assertLessEqual(len(msgs), 6)


class TestHeartbeatAndSignalBlock(unittest.TestCase):
    def test_heartbeat_format(self):
        kt = datetime(2026, 8, 17, 10, 0, tzinfo=TZ_CN)
        msg = heartbeat_message(kt, 50, 0)
        self.assertIn("✅ 系统正常", msg)
        self.assertIn("K线 10:00", msg)
        self.assertIn("检查 50 只", msg)
        self.assertIn("0 信号", msg)

    def test_signal_block_fields(self):
        kt = datetime(2026, 8, 17, 10, 0, tzinfo=TZ_CN)
        sig = {
            "cur_close": 1680.0,
            "cur_ma": 1672.35,
            "prev_close": 1660.0,
            "prev_ma": 1665.0,
            "cross_above": True,
            "deviation_pct": 0.4573,
        }
        block = build_signal_block("600519", "贵州茅台", kt, sig)
        self.assertIn("贵州茅台（600519）", block)
        self.assertIn("K线 10:00", block)
        self.assertIn("1,680.00", block)
        self.assertIn("1,672.35", block)
        self.assertIn("+0.46%", block)


class TestReplayDateResolution(unittest.TestCase):
    """ADR-0013 决策点①A：今天交易日且已收盘 → 今天；否则往前找最近交易日。"""

    def setUp(self):
        # 2026-08-14 周五、08-17 周一 为交易日；08-15/16 周末非交易日
        self.calendar = {date(2026, 8, 14), date(2026, 8, 17), date(2026, 8, 18)}

    def test_trading_day_after_close_replays_today(self):
        now = datetime(2026, 8, 17, 15, 30, tzinfo=TZ_CN)  # 周一 15:30 已收盘
        self.assertEqual(resolve_replay_date(self.calendar, now), date(2026, 8, 17))

    def test_trading_day_at_close_boundary(self):
        now = datetime(2026, 8, 17, 15, 0, tzinfo=TZ_CN)  # 15:00 整视为已收盘
        self.assertEqual(resolve_replay_date(self.calendar, now), date(2026, 8, 17))

    def test_trading_day_before_close_replays_previous(self):
        now = datetime(2026, 8, 17, 10, 30, tzinfo=TZ_CN)  # 周一未收盘 → 回放周五
        self.assertEqual(resolve_replay_date(self.calendar, now), date(2026, 8, 14))

    def test_weekend_replays_previous_trading_day(self):
        now = datetime(2026, 8, 16, 12, 0, tzinfo=TZ_CN)  # 周六 → 回放周五
        self.assertEqual(resolve_replay_date(self.calendar, now), date(2026, 8, 14))

    def test_empty_calendar_returns_none(self):
        now = datetime(2026, 8, 16, 12, 0, tzinfo=TZ_CN)
        self.assertIsNone(resolve_replay_date(set(), now))


class TestParseReplayDate(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(parse_replay_date("2026-08-14"), date(2026, 8, 14))

    def test_invalid(self):
        self.assertIsNone(parse_replay_date("2026/08/14"))
        self.assertIsNone(parse_replay_date("not-a-date"))
        self.assertIsNone(parse_replay_date(""))

    def test_strips_whitespace(self):
        self.assertEqual(parse_replay_date(" 2026-08-14 "), date(2026, 8, 14))


class TestReplaySignals(unittest.TestCase):
    """ADR-0013 逐根回放：只用 t 之前数据；A型触发只报一次。"""

    def test_detects_single_cross_at_jump(self):
        target = date(2026, 8, 14)
        df = make_replay_df(target, jump_at="11:00")
        signals, details = replay_signals_for_stock(df, target, "600519", "贵州茅台")
        self.assertEqual(len(details), 16)  # 目标日 16 根K线全有明细
        self.assertEqual(len(signals), 1)  # A型：只报上穿那一根
        self.assertEqual(signals[0]["kline_time"].strftime("%H:%M"), "11:00")
        self.assertEqual(signals[0]["code"], "600519")
        self.assertEqual(signals[0]["name"], "贵州茅台")

    def test_no_signals_when_flat(self):
        target = date(2026, 8, 14)
        df = make_replay_df(target, jump_at="11:00")
        # 把跳升值改平：直接构造全平序列
        df["收盘"] = 10.0
        signals, details = replay_signals_for_stock(df, target, "600519", "贵州茅台")
        self.assertEqual(len(details), 16)
        self.assertEqual(signals, [])

    def test_target_day_missing_returns_empty(self):
        target = date(2026, 8, 14)
        df = make_replay_df(target, jump_at="11:00")
        other_day = date(2026, 8, 21)  # 不在数据中
        signals, details = replay_signals_for_stock(df, other_day, "600519", "贵州茅台")
        self.assertEqual(signals, [])
        self.assertEqual(details, [])

    def test_no_future_data_leak(self):
        """验证逐根判定只用到 t 及之前的数据：跳升发生在 11:00，10:45 及之前不应报。"""
        target = date(2026, 8, 14)
        df = make_replay_df(target, jump_at="11:00")
        signals, details = replay_signals_for_stock(df, target, "600519", "贵州茅台")
        reported_times = [s["kline_time"].strftime("%H:%M") for s in signals]
        self.assertEqual(reported_times, ["11:00"])


class TestReplayMessage(unittest.TestCase):
    def test_no_signal_message(self):
        target = date(2026, 8, 14)
        msgs = build_replay_message(target, checked=50, signals=[])
        self.assertEqual(len(msgs), 1)
        self.assertIn("## 📊 信号复盘：2026-08-14（周五）", msgs[0])
        self.assertIn("当日无上穿信号（检查 50 只 × 16 根K线）", msgs[0])

    def test_signal_message_without_deviation(self):
        target = date(2026, 8, 14)
        kt = datetime(2026, 8, 14, 11, 0)
        sig = {
            "cur_close": 12.5,
            "cur_ma": 11.0,
            "prev_close": 9.5,
            "prev_ma": 10.5,
            "cross_above": True,
            "deviation_pct": 13.64,
        }
        signals = [{"code": "600519", "name": "贵州茅台", "kline_time": kt, "sig": sig}]
        msgs = build_replay_message(target, checked=50, signals=signals)
        self.assertEqual(len(msgs), 1)
        self.assertIn("贵州茅台（600519）", msgs[0])
        self.assertIn("K线 11:00", msgs[0])
        self.assertNotIn("偏离", msgs[0])  # ADR-0013：复盘消息去掉偏离字段

    def test_multiple_signals_sorted_by_time(self):
        target = date(2026, 8, 14)
        sig = {
            "cur_close": 12.5,
            "cur_ma": 11.0,
            "prev_close": 9.5,
            "prev_ma": 10.5,
            "cross_above": True,
            "deviation_pct": 13.64,
        }
        signals = [
            {"code": "300750", "name": "宁德时代", "kline_time": datetime(2026, 8, 14, 14, 30), "sig": sig},
            {"code": "600519", "name": "贵州茅台", "kline_time": datetime(2026, 8, 14, 10, 0), "sig": sig},
        ]
        msgs = build_replay_message(target, checked=50, signals=signals)
        self.assertLess(msgs[0].index("K线 10:00"), msgs[0].index("K线 14:30"))  # 时间升序


class TestFeishuFormatting(unittest.TestCase):
    """ADR-0014 飞书渠道：标题提取与 lark_md 方言转换。"""

    def test_extract_title_from_signal_message(self):
        content = "## 📈 MA60 上穿信号（15分钟K线）\n\n**贵州茅台（600519）**｜K线 10:00"
        title, body = extract_feishu_card_title(content)
        self.assertEqual(title, "📈 MA60 上穿信号（15分钟K线）")
        self.assertNotIn("##", body)
        self.assertIn("贵州茅台", body)

    def test_extract_title_absent(self):
        content = "✅ 系统正常 | K线 10:00 | 检查 50 只 | 0 信号"
        title, body = extract_feishu_card_title(content)
        self.assertEqual(title, "")
        self.assertEqual(body, content)

    def test_format_feishu_markdown_headings_and_bullets(self):
        content = "## 标题\n- 条目一\n- 条目二\n**加粗**"
        out = format_feishu_markdown(content)
        self.assertIn("**标题**", out)  # # 标题 → 加粗（lark_md 不支持 #）
        self.assertIn("• 条目一", out)  # - 列表 → • 列表
        self.assertIn("**加粗**", out)  # 原生支持保留

    def test_format_feishu_markdown_no_heading_marker_left(self):
        out = format_feishu_markdown("## ⚠️ 监控异常\n- 严重延迟")
        self.assertNotIn("##", out)
        self.assertIn("**⚠️ 监控异常**", out)


class TestEastmoneyPatch(unittest.TestCase):
    """ADR-0015 东财反爬补丁：域名匹配与 UA 选择（纯函数）。"""

    def test_target_domains_matched(self):
        self.assertTrue(is_eastmoney_target("https://push2his.eastmoney.com/api/qt/stock/kline/get?x=1"))
        self.assertTrue(is_eastmoney_target("https://push2.eastmoney.com/api/qt/clist/get"))
        self.assertTrue(is_eastmoney_target("https://fund.eastmoney.com/data/"))
        self.assertFalse(is_eastmoney_target("https://finance.sina.com.cn/xxx"))
        self.assertFalse(is_eastmoney_target("https://open.feishu.cn/open-apis/bot/v2/hook/x"))
        self.assertFalse(is_eastmoney_target("https://qyapi.weixin.qq.com/cgi-bin/webhook/send"))
        self.assertFalse(is_eastmoney_target(""))

    def test_random_user_agent_from_pool(self):
        uas = {random_user_agent() for _ in range(50)}
        self.assertGreater(len(uas), 1)  # 从内置池随机
        self.assertTrue(all(ua.startswith("Mozilla/5.0") for ua in uas))


class TestSinaParsers(unittest.TestCase):
    """ADR-0015 新浪源：符号转换、名称响应、分钟K线 JSONP 解析。"""

    def test_sina_symbol(self):
        self.assertEqual(sina_symbol("600519"), "sh600519")
        self.assertEqual(sina_symbol("688981"), "sh688981")
        self.assertEqual(sina_symbol("000001"), "sz000001")
        self.assertEqual(sina_symbol("300750"), "sz300750")

    def test_parse_sina_name_response(self):
        text = (
            'var hq_str_sh600519="贵州茅台,1700.00,1699.00,1710.00";\n'
            'var hq_str_sz000001="平安银行,11.00,10.90,11.10";\n'
            'var hq_str_sh600999="";\n'
        )
        result = parse_sina_name_response(text)
        self.assertEqual(result, {"600519": "贵州茅台", "000001": "平安银行"})  # 空名称行跳过

    def test_parse_sina_kline_jsonp(self):
        text = (
            'var _data=[{"day":"2026-08-14 09:45:00","open":"10.0","high":"10.2",'
            '"low":"9.9","close":"10.1","volume":"1234"},'
            '{"day":"2026-08-14 10:00:00","open":"10.1","high":"10.5",'
            '"low":"10.0","close":"10.4","volume":"5678"}];'
        )
        df = parse_sina_kline_jsonp(text)
        self.assertIsNotNone(df)
        self.assertEqual(len(df), 2)
        self.assertEqual(df["收盘"].tolist(), [10.1, 10.4])
        self.assertEqual(df["时间"].iloc[0].strftime("%Y-%m-%d %H:%M"), "2026-08-14 09:45")

    def test_parse_sina_kline_jsonp_invalid(self):
        self.assertIsNone(parse_sina_kline_jsonp(""))
        self.assertIsNone(parse_sina_kline_jsonp("var _data=null;"))
        self.assertIsNone(parse_sina_kline_jsonp("not json at all"))


if __name__ == "__main__":
    unittest.main()



