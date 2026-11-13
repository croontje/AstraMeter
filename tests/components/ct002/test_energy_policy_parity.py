"""Run identical inputs through Python and a real compiled C++ policy."""

import random
import shutil
import subprocess
from dataclasses import astuple
from pathlib import Path

import pytest

from astrameter.ct002.energy_policy import (
    EnergyPolicyConfig,
    MonthlyPeakReading,
    QuarterReading,
    QuarterTracker,
    decide_energy,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture(scope="module")
def policy_binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    tmp_path = tmp_path_factory.mktemp("energy_policy")
    compiler = shutil.which("g++") or shutil.which("clang++")
    if compiler is None:
        pytest.skip("a C++17 compiler is required for energy policy parity")
    binary = tmp_path / "energy_policy.exe"
    subprocess.run(
        [
            compiler,
            "-std=c++17",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-I",
            str(ROOT),
            str(ROOT / "tests/components/ct002/energy_policy_driver.cpp"),
            str(ROOT / "esphome/components/ct002/energy_policy.cpp"),
            "-o",
            str(binary),
        ],
        check=True,
        timeout=60,
    )
    return binary


def test_shared_energy_policy_vectors(policy_binary: Path) -> None:
    rng = random.Random(8123)
    rows, expected = [], []
    for _ in range(1000):
        c = EnergyPolicyConfig(
            threshold_w=rng.choice([1500, 2400, 2500]),
            max_charge_w=rng.choice([0, 500, 800]),
            max_discharge_w=rng.choice([0, 500, 800]),
            charge_start_minute=rng.choice([0, 360, 1320]),
            charge_end_minute=360,
            night_charge_enabled=rng.choice([False, True]),
            quarter_enabled=rng.choice([False, True]),
            response_margin_s=rng.choice([0, 10, 30]),
            night_charge_power_w=rng.choice([0, 300, 800, 1600]),
        )
        epoch = rng.choice([450, 899, 900, 901, 1799]) + rng.random() / 10
        grid, battery = rng.uniform(-3000, 6000), rng.uniform(-800, 800)
        minute = rng.choice([None, 0, 359, 360, 1320, 1439])
        q = QuarterReading(rng.uniform(0, 3000), epoch + rng.choice([-4, -1, 0, 1]))
        quarter = rng.choice([None, q])
        peak = MonthlyPeakReading(
            rng.choice([2000, 2450, 3500, 4500, -1]),
            rng.choice([261106190000, 261031190000, 261132000000, 261114000000]),
            epoch + rng.choice([-4, -3, 0, 1]),
        )
        monthly_peak = rng.choice([None, peak])
        local_stamp = rng.choice([None, 261113120000, 261201000000])
        expected.append(
            astuple(
                decide_energy(
                    c,
                    grid_w=grid,
                    battery_w=battery,
                    epoch=epoch,
                    local_minute=minute,
                    quarter=quarter,
                    monthly_peak=monthly_peak,
                    local_timestamp=local_stamp,
                )
            )
        )
        values = (
            *astuple(c),
            grid,
            battery,
            epoch,
            minute if minute is not None else -1,
            quarter is not None,
            q.accumulated_w,
            q.sample_epoch,
            monthly_peak is not None,
            *astuple(peak),
            local_stamp if local_stamp is not None else -1,
        )
        rows.append(
            " ".join(str(int(v)) if isinstance(v, bool) else str(v) for v in values)
        )
    result = subprocess.run(
        [str(policy_binary)],
        input="\n".join(rows) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    actual = result.stdout.splitlines()
    assert len(actual) == len(expected)
    for row, target in zip(actual, expected, strict=True):
        assert [float(v) for v in row.split()] == pytest.approx(
            [float(v) for v in target], abs=1e-6
        )


@pytest.mark.parametrize(
    "stamp,local_stamp,age,expected",
    [
        (261106190000, 261113120000, 0, 3400),
        (261106190000, 261201000000, 0, 2400),
        (261206190000, 270101000000, 0, 2400),
        (261106190000, 261113120000, 4, 2400),
        (261106190000, 261113120000, -1, 2400),
        (261132000000, 261113120000, 0, 2400),
        (261114000000, 261113120000, 0, 2400),
        (240229120000, 240229130000, 0, 3400),
        (260229120000, 260228130000, 0, 2400),
    ],
)
def test_month_peak_python_cpp_known_outcomes(
    policy_binary, stamp, local_stamp, age, expected
):
    cfg = EnergyPolicyConfig(quarter_enabled=True)
    d = decide_energy(
        cfg,
        grid_w=3000,
        battery_w=0,
        epoch=450,
        local_minute=600,
        monthly_peak=MonthlyPeakReading(3500, stamp, 450 - age),
        local_timestamp=local_stamp,
    )
    assert d.effective_threshold_w == expected
    values = (
        *astuple(cfg),
        3000,
        0,
        450,
        600,
        False,
        0,
        450,
        True,
        3500,
        stamp,
        450 - age,
        local_stamp,
    )
    result = subprocess.run(
        [str(policy_binary)],
        input=" ".join(str(int(v)) if isinstance(v, bool) else str(v) for v in values)
        + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    actual = [float(v) for v in result.stdout.split()]
    assert actual == pytest.approx([float(v) for v in astuple(d)])
    assert actual[-2] == expected


def test_shared_quarter_tracker_vectors(policy_binary: Path) -> None:
    tracker = QuarterTracker()
    rows, expected = [], []
    imported = 100.0
    q = 0.0
    for mono in range(0, 3600, 3):
        epoch = 1800 + mono
        if mono % 900 == 0:
            q = 0.0
        imported += 0.001
        q += 4.0
        # Counter reset, bad register growth, gaps and clock correction.
        if mono == 600:
            q = 0.0
        if mono == 1200:
            q += 100
        if 1500 <= mono < 1524:
            continue
        if 2100 <= mono < 2200:
            epoch += 60
        point = (q, imported, epoch, mono)
        rows.append(" ".join(map(str, point)))
        expected.append(tracker.feed(*point))
    result = subprocess.run(
        [str(policy_binary), "tracker"],
        input="\n".join(rows) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    actual = result.stdout.splitlines()
    assert len(actual) == len(expected)
    assert any(value is not None for value in expected)
    for row, value in zip(actual, expected, strict=True):
        if value is None:
            assert row == "invalid"
        else:
            assert [float(v) for v in row.split()] == pytest.approx(astuple(value))


@pytest.mark.parametrize("reset_second", [1, 3, 14, 15, 16])
@pytest.mark.parametrize("import_counter_reset", [False, True])
def test_rollover_recovers_without_hiding_counter_resets(
    policy_binary: Path, reset_second: int, import_counter_reset: bool
) -> None:
    # The meter can still return the previous quarter's register just after
    # our clock crosses :00. The next response carries its normal reset.
    points = [(1000.0, 100.0, 899, 899)]
    for epoch in [*range(900, 961), *range(1800, 1846)]:
        elapsed = epoch % 900
        old_register = epoch < 900 + reset_second
        q = float(1000 + elapsed if old_register else elapsed)
        imported = 100 + (epoch - 899) / 4000
        if import_counter_reset and not old_register:
            imported -= 50
        points.append((q, imported, epoch, epoch))

    result = subprocess.run(
        [str(policy_binary), "tracker"],
        input="\n".join(" ".join(map(str, point)) for point in points) + "\n",
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    lines = result.stdout.splitlines()
    assert len(lines) == len(points)
    tracker = QuarterTracker()
    for point, line in zip(points, lines, strict=True):
        epoch = point[2]
        # A reset first observed at the initial settled sample is allowed.
        # Once both samples are settled, a drop must reject the whole slot.
        accepted = (
            945 <= epoch < 1800 and reset_second <= 15 and not import_counter_reset
        ) or epoch >= 1845
        reading = tracker.feed(*point)
        assert (reading is not None) == accepted, (epoch, point)
        if accepted:
            assert reading == QuarterReading(point[0], epoch)
            assert [float(value) for value in line.split()] == pytest.approx(
                [point[0], epoch]
            )
        else:
            assert line == "invalid"
