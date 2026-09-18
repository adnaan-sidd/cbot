from datetime import datetime, timedelta

import pytest

from core_models import Candle
from resample import resample_to_higher_timeframe


def _m15(hour: int, minute: int, o: float, h: float, l: float, c: float) -> Candle:
    ts = datetime(2026, 1, 5, hour, minute)  # a Monday
    return Candle(timestamp=ts, open=o, high=h, low=l, close=c, volume=10)


class TestResampleBasics:
    def test_empty_input_returns_empty(self):
        assert resample_to_higher_timeframe([], 60) == []

    def test_invalid_timeframe_rejected(self):
        with pytest.raises(ValueError):
            resample_to_higher_timeframe([_m15(0, 0, 1, 2, 0.5, 1.5)], 0)

    def test_single_bucket_never_emitted_as_complete(self):
        """All candles fall in the same (still-forming) hour -> nothing
        should be emitted, since we can never know it's 'done' without
        seeing a candle from the next bucket."""
        candles = [
            _m15(10, 0, 100, 101, 99, 100.5),
            _m15(10, 15, 100.5, 102, 100, 101),
            _m15(10, 30, 101, 103, 100.5, 102),
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert result == []


class TestResampleAggregation:
    def test_four_m15_bars_aggregate_into_one_h1_bar(self):
        """4 M15 candles spanning 10:00-10:45, plus one candle in the
        NEXT hour to close the first bucket. Expect exactly one H1
        candle: open=first bar's open, close=last bar's close (10:45),
        high=max of all four, low=min of all four."""
        candles = [
            _m15(10, 0, 100.0, 102.0, 99.0, 101.0),
            _m15(10, 15, 101.0, 103.0, 100.5, 102.0),
            _m15(10, 30, 102.0, 104.0, 99.5, 100.0),
            _m15(10, 45, 100.0, 101.0, 98.0, 99.0),
            _m15(11, 0, 99.0, 100.0, 98.5, 99.5),  # next hour -> closes the 10:00 bucket
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert len(result) == 1
        h1 = result[0]
        assert h1.timestamp == datetime(2026, 1, 5, 10, 0)
        assert h1.open == pytest.approx(100.0)
        assert h1.close == pytest.approx(99.0)
        assert h1.high == pytest.approx(104.0)
        assert h1.low == pytest.approx(98.0)

    def test_volume_sums_across_the_bucket(self):
        candles = [
            _m15(10, 0, 100, 101, 99, 100.5),
            _m15(10, 15, 100.5, 102, 100, 101),
            _m15(11, 0, 101, 102, 100, 101.5),  # closes the bucket
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert result[0].volume == pytest.approx(20.0)  # 10 + 10

    def test_multiple_complete_buckets(self):
        candles = [
            _m15(10, 0, 100, 101, 99, 100),
            _m15(10, 45, 100, 102, 99, 101),
            _m15(11, 0, 101, 103, 100, 102),
            _m15(11, 45, 102, 104, 101, 103),
            _m15(12, 0, 103, 105, 102, 104),  # closes the 11:00 bucket
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert len(result) == 2
        assert result[0].timestamp == datetime(2026, 1, 5, 10, 0)
        assert result[1].timestamp == datetime(2026, 1, 5, 11, 0)

    def test_partial_final_bucket_always_excluded_even_with_many_candles(self):
        """Even if the final (in-progress) bucket has almost a full
        hour's worth of M15 candles, it must never be emitted — only a
        candle from the NEXT hour proves it's actually closed."""
        candles = [
            _m15(10, 0, 100, 101, 99, 100),
            _m15(11, 0, 101, 102, 100, 101),
            _m15(11, 15, 101, 102, 100, 101),
            _m15(11, 30, 101, 102, 100, 101),
            _m15(11, 45, 101, 102, 100, 101),  # 11:00 bucket looks "full" but isn't closed
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert len(result) == 1
        assert result[0].timestamp == datetime(2026, 1, 5, 10, 0)


class TestResampleBucketAlignment:
    def test_buckets_align_to_clock_hour_not_stream_start(self):
        """The stream starts at 10:07 (not on the hour) — buckets must
        still align to the clock hour (10:00-10:59), not to wherever the
        data happens to start."""
        candles = [
            _m15(10, 7, 100, 101, 99, 100),
            _m15(10, 22, 100, 101, 99, 100),
            _m15(11, 7, 100, 101, 99, 100),  # closes the 10:00 bucket
        ]
        result = resample_to_higher_timeframe(candles, 60)
        assert len(result) == 1
        assert result[0].timestamp == datetime(2026, 1, 5, 10, 0)

    def test_non_hour_timeframe_e_g_30_minutes(self):
        candles = [
            _m15(10, 0, 100, 101, 99, 100),
            _m15(10, 15, 100, 102, 99, 101),
            _m15(10, 30, 101, 103, 100, 102),  # closes the 10:00-10:29 bucket
        ]
        result = resample_to_higher_timeframe(candles, 30)
        assert len(result) == 1
        assert result[0].timestamp == datetime(2026, 1, 5, 10, 0)
        assert result[0].close == pytest.approx(101.0)  # last candle IN the 10:00 bucket
