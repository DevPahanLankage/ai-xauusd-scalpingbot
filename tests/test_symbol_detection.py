import unittest

from xauusd_bot.collector import select_gold_symbol
from xauusd_bot.mcp_client import READ_ONLY_TOOL_ALLOWLIST


class GoldSymbolDetectionTests(unittest.TestCase):
    def test_prefers_exact_xauusd(self) -> None:
        symbols = [
            {
                "symbol": "XAUUSD.a",
                "currency_base": "XAU",
                "currency_profit": "USD",
                "selected": True,
            },
            {
                "symbol": "XAUUSD",
                "currency_base": "XAU",
                "currency_profit": "USD",
                "selected": True,
            },
        ]
        self.assertEqual(select_gold_symbol(symbols)["symbol"], "XAUUSD")

    def test_rejects_gold_quoted_in_eur(self) -> None:
        symbols = [
            {
                "symbol": "XAUEUR",
                "description": "Gold vs Euro",
                "currency_base": "XAU",
                "currency_profit": "EUR",
                "selected": True,
            }
        ]
        self.assertIsNone(select_gold_symbol(symbols))

    def test_allowlist_contains_no_trading_tools(self) -> None:
        self.assertFalse(any(name.startswith("trade_") for name in READ_ONLY_TOOL_ALLOWLIST))


if __name__ == "__main__":
    unittest.main()
