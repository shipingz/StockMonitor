# -*- coding: utf-8 -*-
"""纯逻辑单元测试（不依赖网络 / akshare）：MA60 计算、上穿判定、消息构建与拆分。

运行：python -m unittest discover -s tests -v
"""

import sys
import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, ".")
from main import (  # noqa: E402
    MA_PERIOD,
    build_signal_block,
    compute_ma_signal,
    heartbeat_message,
    pack_signal_messages,
    parse_stock_list,
)

TZ_CN = ZoneInfo("Asia/Shanghai")


def make_closes(last: float, prev: float, n: int = 80, base: float = 10.0, drift: float = 0.01) -> list:
    """构造 n 根K线收盘价序列：前 n-2 根从 base 缓慢上行，最后两根为 prev、last。"""
    closes = [base + i * drift for i in range(n - 2)]
    closes.append(prev)
    closes.append(last)
    return closes


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


if __name__ == "__main__":
    unittest.main()
