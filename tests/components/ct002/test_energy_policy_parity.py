"""Run identical inputs through Python and a real compiled C++ policy."""

import random
import shutil
import subprocess
from dataclasses import astuple
from pathlib import Path

import pytest

from astrameter.ct002.energy_policy import (
    EnergyPolicyConfig,
    QuarterReading,
    QuarterTracker,
    decide_energy,
)

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def policy_binary(tmp_path: Path) -> Path:
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
        expected.append(
            astuple(
                decide_energy(
                    c,
                    grid_w=grid,
                    battery_w=battery,
                    epoch=epoch,
                    local_minute=minute,
                    quarter=quarter,
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
