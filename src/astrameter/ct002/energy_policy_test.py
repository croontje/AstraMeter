"""Policy acceptance cases; no devices or services are contacted."""

from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from astrameter.ct002.energy_policy import (
    EnergyPolicyConfig,
    QuarterReading,
    decide_energy,
    in_charge_window,
)

CONFIG = EnergyPolicyConfig(night_charge_enabled=True, quarter_enabled=True)


@pytest.mark.parametrize(
    ("grid", "battery", "minute", "expected"),
    [
        (500, 0, 0, -800),  # midnight starts charging
        (500, 0, 359, -800),
        (500, 0, 360, 0),  # end is exclusive
        (2500, -800, 360, 0),  # stop an existing charge
        (2200, 0, 1, -200),  # little import headroom
        (3000, 0, 1, 600),  # peak demand preempts night charging
        (4000, 0, 1, 800),  # hardware limit; cannot meet threshold
        (2400, 600, 600, 600),  # include actual battery output
        (1000, 600, 600, 0),  # wind down after demand falls
        (-500, 0, 600, -500),  # solar charging outside the night window
        (-1500, 0, 600, -800),
        (500, 0, None, 0),  # no synchronized clock: no scheduled charge
    ],
)
def test_instantaneous_fallback(
    grid: float, battery: float, minute: int | None, expected: float
) -> None:
    decision = decide_energy(
        CONFIG, grid_w=grid, battery_w=battery, epoch=450, local_minute=minute
    )
    assert decision.battery_target_w == expected
    assert decision.grid_target_w == grid + battery - expected
    assert decision.correction_w == expected - battery
    assert not decision.quarter_used


@pytest.mark.parametrize(
    ("minute", "expected"),
    [(1319, False), (1320, True), (0, True), (359, True), (360, False)],
)
def test_midnight_crossing(minute: int, expected: bool) -> None:
    assert in_charge_window(minute, 1320, 360) is expected
    assert not in_charge_window(minute, 0, 0)


@pytest.mark.parametrize("fold", [0, 1])
def test_repeated_autumn_hour_is_inside_window(fold: int) -> None:
    local = datetime(2026, 10, 25, 2, 30, tzinfo=ZoneInfo("Europe/Brussels"), fold=fold)
    decision = decide_energy(
        CONFIG,
        grid_w=500,
        battery_w=0,
        epoch=local.timestamp(),
        local_minute=local.hour * 60 + local.minute,
    )
    assert decision.night_window
    assert decision.battery_target_w == -800


def test_quarter_allows_short_peak_without_spending_battery() -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=3200,
        battery_w=0,
        epoch=450,
        local_minute=600,
        quarter=QuarterReading(500, 450),
    )
    assert decision.quarter_used
    assert decision.battery_target_w == 0


def test_quarter_reduces_charge_then_requires_discharge() -> None:
    cfg = replace(CONFIG, response_margin_s=0)
    targets = []
    for q in (1000, 1500, 2000):
        decision = decide_energy(
            cfg,
            grid_w=1600,
            battery_w=0,
            epoch=450,
            local_minute=1,
            quarter=QuarterReading(q, 450),
        )
        targets.append(decision.battery_target_w)
    assert targets == [-800, -200, 800]


@pytest.mark.parametrize("q", [2400, 2600])
def test_spent_quarter_never_claims_it_can_undo_import(q: float) -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=2000,
        battery_w=0,
        epoch=600,
        local_minute=1,
        quarter=QuarterReading(q, 600),
    )
    assert decision.grid_limit_w == 0
    assert decision.battery_target_w == 800
    assert decision.grid_target_w == 1200
    assert decision.limited


@pytest.mark.parametrize(
    "quarter",
    [
        QuarterReading(1200, 446),
        QuarterReading(1200, 451),
        QuarterReading(-1, 450),
        QuarterReading(float("nan"), 450),
        QuarterReading(0, float("inf")),
    ],
)
def test_invalid_or_stale_quarter_falls_back(quarter: QuarterReading) -> None:
    decision = decide_energy(
        CONFIG, grid_w=3000, battery_w=0, epoch=450, local_minute=1, quarter=quarter
    )
    assert not decision.quarter_used
    assert decision.battery_target_w == 600


def test_previous_quarter_is_invalid_even_one_second_old() -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=3000,
        battery_w=0,
        epoch=900,
        local_minute=1,
        quarter=QuarterReading(2390, 899),
    )
    assert not decision.quarter_used
    assert decision.battery_target_w == 600


def test_first_decision_after_restart_keeps_import_already_recorded() -> None:
    # There is deliberately no in-memory accumulator to restart at zero.
    decision = decide_energy(
        CONFIG,
        grid_w=1600,
        battery_w=0,
        epoch=600,
        local_minute=1,
        quarter=QuarterReading(2300, 600),
    )
    assert decision.quarter_used
    assert decision.battery_target_w == 800


def test_export_does_not_recover_spent_quarter_budget() -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=-500,
        battery_w=0,
        epoch=600,
        local_minute=600,
        quarter=QuarterReading(2500, 600),
    )
    assert decision.grid_limit_w == 0
    assert decision.battery_target_w == -500


def test_defaults_do_not_enable_scheduled_or_quarter_behavior() -> None:
    decision = decide_energy(
        EnergyPolicyConfig(),
        grid_w=3000,
        battery_w=0,
        epoch=450,
        local_minute=1,
        quarter=QuarterReading(500, 450),
    )
    assert not decision.night_window
    assert not decision.quarter_used
    assert decision.battery_target_w == 600


def test_sample_age_and_response_margin_reserve_import() -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=2000,
        battery_w=0,
        epoch=452,
        local_minute=1,
        quarter=QuarterReading(1200, 450),
    )
    assert decision.grid_limit_w == pytest.approx((1200 * 900 - 2000 * 12) / 438)


@pytest.mark.parametrize("q", [1200, 2400])
def test_end_of_quarter_has_no_division_by_zero(q: float) -> None:
    decision = decide_energy(
        CONFIG,
        grid_w=2000,
        battery_w=0,
        epoch=899,
        local_minute=1,
        quarter=QuarterReading(q, 899),
    )
    assert decision.grid_limit_w == (2400 if q == 1200 else 0)


@pytest.mark.parametrize("field", ["grid_w", "battery_w", "epoch"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_bad_input_must_not_become_zero(field: str, bad: float) -> None:
    values = {"grid_w": 500.0, "battery_w": 0.0, "epoch": 450.0}
    values[field] = bad
    with pytest.raises(ValueError):
        decide_energy(
            CONFIG,
            local_minute=1,
            grid_w=values["grid_w"],
            battery_w=values["battery_w"],
            epoch=values["epoch"],
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"threshold_w": 0},
        {"max_charge_w": -1},
        {"max_discharge_w": float("nan")},
        {"charge_start_minute": 1440},
        {"charge_end_minute": -1},
        {"response_margin_s": 900},
    ],
)
def test_invalid_settings(changes: dict) -> None:
    with pytest.raises(ValueError):
        replace(CONFIG, **changes)


def test_quarter_replay_with_delayed_battery_and_cooking_load() -> None:
    """Two fixed quarters, 3 s device delay, charging interrupted by cooking.

    This checks the pure policy with a simple plant, not firmware accuracy.
    The 3000 W cooking demand is physically shaveable with an 800 W battery.
    """
    battery = 0.0
    import_ws = 0.0
    pending = [0.0] * 3
    quarter_peaks = []
    charged_ws = 0.0
    for second in range(1800):
        if second % 900 == 0:
            import_ws = 0.0
        demand = 3000.0 if second % 900 >= 200 else 1600.0
        grid = demand - battery
        decision = decide_energy(
            CONFIG,
            grid_w=grid,
            battery_w=battery,
            epoch=float(second),
            local_minute=second // 60,
            quarter=QuarterReading(import_ws / 900, float(second)),
        )
        assert -800 <= decision.battery_target_w <= 800
        assert decision.correction_w == pytest.approx(
            decision.battery_target_w - battery
        )
        import_ws += max(0.0, grid)
        charged_ws += max(0.0, -battery)
        pending.append(decision.battery_target_w)
        battery = pending.pop(0)
        if second % 900 == 899:
            quarter_peaks.append(import_ws / 900)
    assert charged_ws > 0
    assert max(quarter_peaks) <= 2400
