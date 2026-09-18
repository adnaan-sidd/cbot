from datetime import datetime, timedelta

import pytest

from core.models import Candle
from market_data.historical_feed import HistoricalFeed


def _candle(i: int) -> Candle:
    ts = datetime(2026, 1, 1) + timedelta(minutes=i)
    return Candle(timestamp=ts, open=100 + i, high=101 + i, low=99 + i, close=100.5 + i, volume=10)


class TestHistoricalFeed:
    def test_yields_candles_in_order(self):
        candles = [_candle(i) for i in range(5)]
        feed = HistoricalFeed(candles)
        result = list(feed.candles())
        assert result == candles

    def test_can_be_consumed_multiple_times(self):
        candles = [_candle(i) for i in range(3)]
        feed = HistoricalFeed(candles)
        first_pass = list(feed.candles())
        second_pass = list(feed.candles())
        assert first_pass == second_pass == candles

    def test_out_of_order_candles_rejected(self):
        candles = [_candle(1), _candle(0)]
        with pytest.raises(ValueError):
            HistoricalFeed(candles)

    def test_len(self):
        candles = [_candle(i) for i in range(4)]
        feed = HistoricalFeed(candles)
        assert len(feed) == 4

    def test_from_csv(self, tmp_path):
        csv_content = (
            "timestamp,open,high,low,close,volume,spread_points\n"
            "2026-01-01T00:00:00,3600.0,3601.0,3599.0,3600.5,10,2.0\n"
            "2026-01-01T00:01:00,3600.5,3602.0,3600.0,3601.5,12,\n"
        )
        csv_path = tmp_path / "candles.csv"
        csv_path.write_text(csv_content)

        feed = HistoricalFeed.from_csv(csv_path)
        candles = list(feed.candles())
        assert len(candles) == 2
        assert candles[0].spread_points == pytest.approx(2.0)
        assert candles[1].spread_points is None
        assert candles[0].timestamp == datetime(2026, 1, 1, 0, 0, 0)

    def test_from_csv_missing_column_raises(self, tmp_path):
        csv_path = tmp_path / "bad.csv"
        csv_path.write_text("timestamp,open,high,low,close\n2026-01-01T00:00:00,1,2,0.5,1.5\n")
        with pytest.raises(ValueError):
            HistoricalFeed.from_csv(csv_path)
