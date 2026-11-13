"""Integration of policy, telemetry, existing balancer and configuration."""

from datetime import datetime
from ipaddress import IPv4Network
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest

from astrameter.config.config_loader import ClientFilter
from astrameter.config.settings import ConfiguredPowermeter, CtSettings, GeneralSettings
from astrameter.ct002.ct002 import CT002, CT002Request
from astrameter.ct002.energy_policy import (
    MonthlyPeakReading,
    QuarterReading,
    QuarterTracker,
)
from astrameter.main import _build_device
from astrameter.powermeter.json_http import JsonHttpPowermeter
from astrameter.powermeter.wrappers.throttling import ThrottledPowermeter
from astrameter.powermeter.wrappers.transform import TransformedPowermeter
from astrameter.status.serialize import ct002_to_wire


def epoch(hour: int, minute: int = 0) -> float:
    return datetime(
        2026, 11, 8, hour, minute, tzinfo=ZoneInfo("Europe/Brussels")
    ).timestamp()


@pytest.mark.asyncio
async def test_month_peak_from_same_response_survives_restart_and_missing_fields_clear_it(
    monkeypatch,
):
    wall = epoch(12)
    monkeypatch.setattr(
        "astrameter.powermeter.json_http.time", SimpleNamespace(time=lambda: wall)
    )
    for _ in range(2):  # fresh process state must recover the meter's existing maximum
        meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
        meter.get_json = AsyncMock(
            return_value={
                "active_power_w": 3000,
                "montly_power_peak_w": 3500,
                "montly_power_peak_timestamp": 261106190000,
            }
        )
        wrapper = ThrottledPowermeter(meter, throttle_interval=1)
        device = CT002(
            peakshaving_threshold=2400, quarter_shaving=True, wall_clock=lambda: wall
        )
        device._update_consumer_report("battery", "A", 0)
        await meter.get_powermeter_watts()
        device.monthly_peak_reading = wrapper.monthly_peak_reading()
        assert device._apply_peakshaving(3000) == 0
        assert device.energy_threshold_w == 3400
        assert device.energy_strategy == "instantaneous+monthly_peak"
        assert meter.get_json.await_count == 1
        assert (
            TransformedPowermeter(meter, [0], [1]).monthly_peak_reading()
            is device.monthly_peak_reading
        )
        assert TransformedPowermeter(meter, [10], [1]).monthly_peak_reading() is None
        assert TransformedPowermeter(meter, [0], [2]).monthly_peak_reading() is None
        meter.get_json.return_value = {"active_power_w": 3000}
        await meter.get_powermeter_watts()
        device.monthly_peak_reading = wrapper.monthly_peak_reading()
        assert device._apply_peakshaving(3000) == 600
        assert device.energy_threshold_w == 2400
        assert device.energy_strategy == "instantaneous"


@pytest.mark.parametrize(
    "old_date,new_date,peak_stamp",
    [
        ((2026, 11, 30), (2026, 12, 1), 261106190000),
        ((2026, 12, 31), (2027, 1, 1), 261206190000),
        ((2026, 3, 31), (2026, 4, 1), 260306190000),  # DST summer offset
    ],
)
def test_month_rollover_uses_brussels_calendar_even_with_fresh_cached_peak(
    old_date, new_date, peak_stamp
):
    wall = datetime(
        *old_date, 23, 59, 59, tzinfo=ZoneInfo("Europe/Brussels")
    ).timestamp()
    d = CT002(peakshaving_threshold=2400, quarter_shaving=True, wall_clock=lambda: wall)
    d._update_consumer_report("battery", "A", 0)
    d.monthly_peak_reading = MonthlyPeakReading(3500, peak_stamp, wall)
    assert d._apply_peakshaving(3000) == 0
    wall = datetime(*new_date, tzinfo=ZoneInfo("Europe/Brussels")).timestamp()
    assert d._apply_peakshaving(3000) == 600
    assert d.energy_threshold_w == 2400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "power,stamp",
    [
        (True, 261106190000),
        (3500, True),
        (None, 261106190000),
        (3500, "bad"),
        (3500, {}),
    ],
)
async def test_bad_monthly_fields_do_not_break_power_reading(power, stamp):
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter.get_json = AsyncMock(
        return_value={
            "active_power_w": 3000,
            "montly_power_peak_w": power,
            "montly_power_peak_timestamp": stamp,
        }
    )
    assert await meter.get_powermeter_watts() == [3000]
    assert meter.monthly_peak_reading() is None


@pytest.mark.asyncio
async def test_failed_http_read_drops_previous_monthly_peak():
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter._monthly_peak = MonthlyPeakReading(3500, 261106190000, epoch(12))
    meter.get_json = AsyncMock(side_effect=TimeoutError)
    with pytest.raises(TimeoutError):
        await meter.get_powermeter_watts()
    assert meter.monthly_peak_reading() is None


@pytest.mark.asyncio
async def test_main_callback_propagates_monthly_peak_and_status_then_clears_on_failure(
    monkeypatch,
):
    wall = epoch(12)
    monkeypatch.setattr(
        "astrameter.powermeter.json_http.time", SimpleNamespace(time=lambda: wall)
    )
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter.get_json = AsyncMock(
        return_value={
            "active_power_w": 3000,
            "montly_power_peak_w": 3500,
            "montly_power_peak_timestamp": 261106190000,
        }
    )
    source = ConfiguredPowermeter(
        meter, ClientFilter([IPv4Network("127.0.0.1/32")]), False
    )
    device = _build_device(
        "ct003",
        CtSettings(quarter_shaving=True, peakshaving_threshold=2400),
        GeneralSettings(),
        [source],
        "test-device",
    )
    device._wall_clock = lambda: wall
    device._update_consumer_report("battery", "A", 0)
    assert await device.before_send(("127.0.0.1", 12345)) == [3000, 0, 0]
    assert device._apply_peakshaving(3000) == 0
    control = ct002_to_wire(device.status_snapshot())["control"]
    assert control["peakshaving_threshold_w"] == 2400
    assert control["energy_threshold_w"] == 3400
    assert control["energy_strategy"] == "instantaneous+monthly_peak"
    meter.get_json.side_effect = TimeoutError
    with pytest.raises(TimeoutError):
        await device.before_send(("127.0.0.1", 12345))
    assert device.monthly_peak_reading is None


@pytest.mark.parametrize(
    "hour,grid,expected", [(0, 500, -800), (5, 2200, -200), (5, 3000, 600), (6, 500, 0)]
)
def test_existing_balancer_receives_scheduled_correction(hour, grid, expected):
    device = CT002(
        peakshaving_threshold=2400, night_charge=True, wall_clock=lambda: epoch(hour)
    )
    device._update_consumer_report("battery", "A", 0)
    device._balancer.compute_target = Mock(return_value=[0, 0, 0])
    device._compute_smooth_target([grid, 0, 0], "battery")
    assert device._balancer.compute_target.call_args.args[3] == expected


def test_loaded_quarter_preempts_night_charge_and_falls_back_on_staleness():
    wall = epoch(1, 10)
    device = CT002(
        peakshaving_threshold=2400,
        night_charge=True,
        quarter_shaving=True,
        wall_clock=lambda: wall,
    )
    device._update_consumer_report("battery", "A", 0)
    device.quarter_reading = QuarterReading(2400, wall)
    assert device._apply_peakshaving(1800) == 1800
    assert device.energy_strategy == "quarter+night_charge"
    device.quarter_reading = QuarterReading(2400, wall - 4)
    assert device._apply_peakshaving(1800) == -600
    assert device.energy_strategy == "instantaneous+night_charge"


def test_paused_manual_and_phase_detecting_batteries_are_not_reconstructed():
    d = CT002(
        peakshaving_threshold=2400, night_charge=True, wall_clock=lambda: epoch(1)
    )
    d._update_consumer_report("auto", "A", 0)
    d._update_consumer_report("manual", "A", 600)
    d._consumers["manual"].manual_enabled = True
    d._update_consumer_report("paused", "A", 700)
    d._consumers["paused"].active = False
    assert d._apply_peakshaving(500) == -800
    assert d._consumer_mode("manual").mode == "manual"
    assert d._consumer_mode("paused").mode == "inactive"


def test_switching_off_active_control_keeps_relay_path():
    d = CT002(
        active_control=False,
        peakshaving_threshold=2400,
        night_charge=True,
        wall_clock=lambda: epoch(1),
    )
    assert d._compute_smooth_target([500, 0, 0]) == [500, 0, 0]


def test_counter_validation_restart_gaps_reset_and_clock_jump():
    tracker = QuarterTracker()
    # Restart ten minutes into a quarter: retain the import register, don't zero it.
    for t in range(600, 631):
        reading = tracker.feed(
            1500 + t - 600, 100 + (t - 600) / 4000, float(t), float(t)
        )
        assert (reading is not None) == (t == 630)
    assert reading.accumulated_w == 1530
    assert tracker.feed(1545, 100.02, 645, 645) is None  # gap
    assert tracker.feed(1546, 100.02025, 646, 700) is None  # clock jump
    assert tracker.feed(10, 100.03, 647, 701) is None  # meter reset poisons slot
    for t in range(648, 700):
        assert tracker.feed(10 + t - 647, 100.03 + (t - 647) / 4000, t, t + 54) is None
    for t in range(915, 946):
        reading = tracker.feed(t - 900, 101 + (t - 900) / 4000, t, t + 54)
    assert reading is not None


@pytest.mark.asyncio
async def test_delayed_meter_rollover_restores_quarter_control(monkeypatch):
    wall = epoch(1) - 1
    monkeypatch.setattr(
        "astrameter.powermeter.json_http.time",
        SimpleNamespace(time=lambda: wall, monotonic=lambda: wall),
    )
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter.get_json = AsyncMock()
    device = CT002(
        peakshaving_threshold=2400, quarter_shaving=True, wall_clock=lambda: wall
    )
    device._update_consumer_report("battery", "A", 0)
    for offset in [-1, 0, *range(3, 61, 3)]:
        wall = epoch(1) + offset
        meter.get_json.return_value = {
            "active_power_w": 900,
            "active_power_average_w": 1000 if offset <= 0 else offset,
            "total_power_import_kwh": 100 + (offset + 1) / 4000,
        }
        grid = await meter.get_powermeter_watts()
        device.quarter_reading = meter.quarter_reading()
        device._apply_peakshaving(grid[0])
        assert device.energy_strategy == (
            "quarter" if offset >= 45 else "instantaneous"
        )
    assert meter.get_json.await_count == 22  # no additional telemetry request


@pytest.mark.asyncio
async def test_one_http_response_supplies_power_and_quarter_without_extra_request():
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter.get_json = AsyncMock(
        return_value={
            "active_power_w": 1800,
            "active_power_average_w": 1600,
            "total_power_import_kwh": 100,
        }
    )
    reading = QuarterReading(1600, epoch(1, 10))
    meter._quarter_tracker.feed = Mock(return_value=reading)
    wrapper = ThrottledPowermeter(meter, throttle_interval=1)
    assert await meter.get_powermeter_watts() == [1800]
    assert wrapper.quarter_reading() is reading
    assert meter.get_json.await_count == 1
    meter.get_json.return_value = {"active_power_w": 1700}
    assert await meter.get_powermeter_watts() == [1700]
    assert wrapper.quarter_reading() is None


def test_transformed_power_does_not_reuse_physical_import_counter():
    meter = JsonHttpPowermeter("http://unused", "$.active_power_w")
    meter._quarter = QuarterReading(1600, epoch(1, 10))
    assert TransformedPowermeter(meter, [0], [1]).quarter_reading() is meter._quarter
    assert TransformedPowermeter(meter, [100], [1]).quarter_reading() is None
    assert TransformedPowermeter(meter, [0], [2]).quarter_reading() is None


@pytest.mark.parametrize("value", ["6:00", "24:00", "12:60", "bad"])
def test_bad_window_rejected_at_startup(value):
    with pytest.raises(ValueError):
        CT002(night_charge_start=value)


@pytest.mark.parametrize("phase", ["A", "B", "C", "D"])
@pytest.mark.parametrize("power", [-800, 600])
def test_meter_loss_requests_zero_output_without_running_balancer(phase, power):
    d = CT002(night_charge=True, peakshaving_threshold=2400)
    d._update_consumer_report("battery", phase, power)
    d._balancer.compute_target = Mock(
        side_effect=AssertionError("must not use fake grid")
    )
    request = CT002Request(
        "HMG-50", "battery", "HME-4", "", phase, power, True, ("localhost", 1)
    )
    result = d._resolve_target(request, True)
    assert sum(result.values) == -power
    assert result.meter_failed
    assert d.energy_strategy == "meter_unavailable_stop"


@pytest.mark.parametrize("manual,active", [(True, True), (False, False)])
def test_meter_loss_does_not_cancel_manual_or_paused_battery(manual, active):
    d = CT002(night_charge=True, peakshaving_threshold=2400)
    d._update_consumer_report("battery", "A", -800)
    d._consumers["battery"].manual_enabled = manual
    d._consumers["battery"].active = active
    request = CT002Request(
        "HMG-50", "battery", "HME-4", "", "A", -800, True, ("localhost", 1)
    )
    assert d._resolve_target(request, True).values == [0, 0, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("monthly_peak", [None, 3500])
async def test_real_udp_firmware_model_charges_shaves_and_ends_window(monthly_peak):
    from astrameter.simulator.battery import BatterySimulator
    from astrameter.simulator.eval_harness import _EvalClock, _start_ct002

    clock = _EvalClock(epoch(5, 45))
    battery = BatterySimulator(
        mac="02B250000001",
        phase="A",
        ct_mac="112233445566",
        ct_host="127.0.0.1",
        ct_port=0,
        meter_dev_type="HMG-50",
        max_charge_power=800,
        max_discharge_power=800,
        capacity_wh=100000,
        initial_soc=0.5,
        poll_interval=3,
    )
    device = CT002(
        ct_mac="112233445566",
        peakshaving_threshold=2400,
        night_charge=True,
        quarter_shaving=True,
        clock=clock,
        wall_clock=clock,
    )
    tracker = QuarterTracker()
    accumulated = 0.0  # this simulated meter starts on a quarter boundary
    imported_ws = accumulated
    previous = clock()
    previous_grid = 500.0
    window = int(clock() // 900)
    powers = []
    quarter_means = []
    elapsed = 0

    async def read_meter(*args):
        nonlocal previous, previous_grid, accumulated, imported_ws, window
        now = clock()
        # Polls land exactly on boundaries in this scenario.
        delta = max(0, previous_grid) * (now - previous)
        accumulated += delta
        imported_ws += delta
        if int(now // 900) != window:
            quarter_means.append(accumulated / 900)
            accumulated = 0
            window = int(now // 900)
        demand = (4000.0 if monthly_peak else 3000.0) if 150 <= elapsed < 900 else 500.0
        grid = demand - battery.current_power
        device.quarter_reading = tracker.feed(
            accumulated / 900, imported_ws / 3600000, now, now
        )
        if monthly_peak:
            device.monthly_peak_reading = MonthlyPeakReading(
                monthly_peak, 261106190000, now
            )
        previous, previous_grid = now, grid
        return [grid, 0, 0]

    device.before_send = read_meter
    await _start_ct002(device, [battery])
    try:
        for elapsed in range(3, 1801, 3):
            clock.set(epoch(5, 45) + elapsed)
            await battery.step(3)
            powers.append((elapsed, battery.current_power))
    finally:
        await device.stop()
    assert min(p for t, p in powers if 60 <= t < 150) < -500
    assert max(p for t, p in powers if 700 <= t < 900) > (100 if monthly_peak else 300)
    assert max(abs(p) for t, p in powers if 1050 <= t < 1200) < 80
    assert max(quarter_means) < (3500 if monthly_peak else 2500)
    if monthly_peak:
        assert device.energy_threshold_w == 3400
        assert max(quarter_means) > 3000  # uses the existing monthly headroom


@pytest.mark.asyncio
@pytest.mark.parametrize("soc,hour,demand", [(1.0, 1, 500), (0.0, 7, 3500)])
async def test_real_udp_respects_battery_soc_limits(soc, hour, demand):
    from astrameter.simulator.battery import BatterySimulator
    from astrameter.simulator.eval_harness import _EvalClock, _start_ct002

    clock = _EvalClock(epoch(hour))
    battery = BatterySimulator(
        mac="02B250000001",
        phase="A",
        ct_mac="112233445566",
        ct_host="127.0.0.1",
        ct_port=0,
        meter_dev_type="HMG-50",
        max_charge_power=800,
        max_discharge_power=800,
        initial_soc=soc,
        poll_interval=3,
    )
    device = CT002(
        ct_mac="112233445566",
        peakshaving_threshold=2400,
        night_charge=True,
        quarter_shaving=True,
        clock=clock,
        wall_clock=clock,
    )

    async def read_meter(*args):
        return [demand - battery.current_power, 0, 0]

    device.before_send = read_meter
    await _start_ct002(device, [battery])
    try:
        for elapsed in range(3, 301, 3):
            clock.set(epoch(hour) + elapsed)
            await battery.step(3)
            assert battery.current_power == 0
        assert battery.soc == soc
    finally:
        await device.stop()
