"""Tests for cc_cortex.cost_tracker."""

from __future__ import annotations

import pytest

from cc_cortex.cost_tracker import CostTracker


@pytest.fixture
def tracker(tmp_path):
    return CostTracker(str(tmp_path), "test-session-1234", budget_usd=5.0)


class TestRecord:
    def test_single_record(self, tracker):
        tracker.record(1000, 500)
        s = tracker.stats()
        assert s["total_input"] == 1000
        assert s["total_output"] == 500

    def test_accumulation(self, tracker):
        tracker.record(1000, 200)
        tracker.record(2000, 300)
        s = tracker.stats()
        assert s["total_input"] == 3000
        assert s["total_output"] == 500

    def test_zero_tokens(self, tracker):
        tracker.record(0, 0)
        s = tracker.stats()
        assert s["estimated_usd"] == 0.0


class TestStats:
    def test_empty_stats(self, tracker):
        s = tracker.stats()
        assert s["total_input"] == 0
        assert s["total_output"] == 0
        assert s["estimated_usd"] == 0.0
        assert s["budget_usd"] == 5.0
        assert s["percent_used"] == 0.0

    def test_cost_calculation(self, tracker):
        # 1M input + 1M output = $3 + $15 = $18
        tracker.record(1_000_000, 1_000_000)
        s = tracker.stats()
        assert abs(s["estimated_usd"] - 18.0) < 0.01

    def test_percent_calculation(self, tracker):
        # Budget $5, spend enough to hit 50%
        # $2.50 = input_tokens * 3/1M + output_tokens * 15/1M
        # Use 0 input, 166_667 output -> ~$2.50
        tracker.record(0, 166_667)
        s = tracker.stats()
        assert 49.0 < s["percent_used"] < 51.0

    def test_custom_budget(self, tmp_path):
        t = CostTracker(str(tmp_path), "sess", budget_usd=1.0)
        t.record(100_000, 50_000)
        s = t.stats()
        # cost = 100k*3/1M + 50k*15/1M = 0.3 + 0.75 = 1.05
        assert s["percent_used"] > 100.0


class TestOverBudget:
    def test_under_budget(self, tracker):
        tracker.record(100, 100)
        assert tracker.is_over_budget() is False

    def test_over_budget(self, tracker):
        # $5 budget, spend $18
        tracker.record(1_000_000, 1_000_000)
        assert tracker.is_over_budget() is True

    def test_exactly_at_budget(self, tmp_path):
        t = CostTracker(str(tmp_path), "sess", budget_usd=0.018)
        # 1k in + 1k out = 0.003 + 0.015 = 0.018
        t.record(1000, 1000)
        assert t.is_over_budget() is True


class TestAlertMessage:
    def test_no_alert_under_80(self, tracker):
        tracker.record(100, 100)
        assert tracker.alert_message() is None

    def test_alert_at_80_percent(self, tmp_path):
        # Budget $1, spend $0.80+
        t = CostTracker(str(tmp_path), "sess", budget_usd=1.0)
        # need > $0.80: 0 input + 54_000 output -> 54k * 15/1M = $0.81
        t.record(0, 54_000)
        msg = t.alert_message()
        assert msg is not None
        assert "Cost alert" in msg

    def test_alert_over_budget(self, tracker):
        tracker.record(1_000_000, 1_000_000)
        msg = tracker.alert_message()
        assert msg is not None
        assert "%" in msg

    def test_zero_budget(self, tmp_path):
        t = CostTracker(str(tmp_path), "sess", budget_usd=0.0)
        t.record(100, 100)
        # percent_used = 0.0 when budget is 0 (division guard)
        assert t.alert_message() is None


class TestUpdateSnapshot:
    def test_first_snapshot_records_full_amount(self, tracker):
        delta_in, delta_out = tracker.update_snapshot(10_000, 2_000)
        assert (delta_in, delta_out) == (10_000, 2_000)
        s = tracker.stats()
        assert s["total_input"] == 10_000
        assert s["total_output"] == 2_000

    def test_second_snapshot_records_delta(self, tracker):
        tracker.update_snapshot(10_000, 2_000)
        delta_in, delta_out = tracker.update_snapshot(15_000, 2_500)
        assert (delta_in, delta_out) == (5_000, 500)
        s = tracker.stats()
        assert s["total_input"] == 15_000
        assert s["total_output"] == 2_500

    def test_reset_detection(self, tracker):
        """Autocompact or model switch — snapshot drops, don't record negatives."""
        tracker.update_snapshot(100_000, 5_000)
        # Simulate autocompact: context drops below prior
        delta_in, delta_out = tracker.update_snapshot(20_000, 5_000)
        assert delta_in == 0
        assert delta_out == 0
        s = tracker.stats()
        # Totals stay at first snapshot (no negative accounting)
        assert s["total_input"] == 100_000
        assert s["total_output"] == 5_000

    def test_after_reset_new_baseline(self, tracker):
        """After a reset, the next growth is measured from the post-reset baseline."""
        tracker.update_snapshot(100_000, 5_000)
        tracker.update_snapshot(20_000, 5_000)  # reset
        delta_in, delta_out = tracker.update_snapshot(25_000, 5_100)
        assert delta_in == 5_000
        assert delta_out == 100
