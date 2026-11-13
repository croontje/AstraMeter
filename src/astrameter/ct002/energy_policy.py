"""Shared scheduled charging / fixed-quarter policy used by CT002.

Positive battery power means discharge. The caller supplies synchronized, fresh
grid and steered-battery power, validated meter import energy, and local time.
No battery API, clock, file, or network access occurs here. See
docs/scheduled-quarter-development.md for the adapter contract.
"""

from dataclasses import dataclass
from math import isfinite


@dataclass(frozen=True)
class EnergyPolicyConfig:
    threshold_w: float = 2400.0
    max_charge_w: float = 800.0
    max_discharge_w: float = 800.0
    charge_start_minute: int = 0
    charge_end_minute: int = 360
    night_charge_enabled: bool = False
    quarter_enabled: bool = False
    response_margin_s: float = 10.0
    night_charge_power_w: float = 800.0

    def __post_init__(self) -> None:
        powers = (
            self.threshold_w,
            self.max_charge_w,
            self.max_discharge_w,
            self.night_charge_power_w,
        )
        if not all(isfinite(v) and v >= 0 for v in powers):
            raise ValueError("power limits must be finite and nonnegative")
        if self.threshold_w <= 0:
            raise ValueError("threshold must be positive")
        if not all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v < 1440
            for v in (self.charge_start_minute, self.charge_end_minute)
        ):
            raise ValueError("window endpoints must be minutes in [0, 1440)")
        if (
            not isfinite(self.response_margin_s)
            or not 0 <= self.response_margin_s < 900
        ):
            raise ValueError("response margin must be in [0, 900)")


@dataclass(frozen=True)
class QuarterReading:
    """Validated import contribution to the FULL quarter, in W (import Wh * 4).

    Export must never subtract from this value. An adapter must validate the
    register against the import counter before constructing this reading.
    """

    accumulated_w: float
    sample_epoch: float


class QuarterTracker:
    """Validate a meter's quarter register against its cumulative import kWh.

    Fixed quarters use UTC epoch boundaries (:00/:15/:30/:45 in Brussels too).
    A process restart waits for 30 seconds of consistent growth; it never
    assumes that earlier import was zero. Quarter-register resets during the
    initial settling period are expected; later resets invalidate the slot.
    """

    def __init__(self) -> None:
        self.previous: tuple[float, float, float, float] | None = None
        self.anchor: tuple[float, float, float, float] | None = None
        self.bad_window: int | None = None

    def feed(
        self, accumulated_w: float, import_kwh: float, epoch: float, monotonic: float
    ) -> QuarterReading | None:
        point = (accumulated_w, import_kwh, epoch, monotonic)
        if not all(isfinite(v) and v >= 0 for v in point):
            self.previous = self.anchor = None
            return None
        window = int(epoch // 900)
        previous = self.previous
        self.previous = point
        if previous is not None:
            pq, pi, pe, pm = previous
            if (
                monotonic < pm
                or monotonic - pm > 10
                or abs(epoch - pe - monotonic + pm) > 2
            ):
                self.anchor = None
            # The meter may briefly return the previous quarter's register
            # after our clock crosses the boundary. Only compare quarter
            # decreases once BOTH samples are outside the settling period.
            # The cumulative import counter must never reset within a slot,
            # including during settling.
            quarter_reset = (
                epoch % 900 >= 15 and pe % 900 >= 15 and accumulated_w < pq - 1
            )
            if int(pe // 900) == window and (quarter_reset or import_kwh < pi):
                self.bad_window = window
        if epoch % 900 < 15 or self.bad_window == window:
            self.anchor = None
            return None
        if self.anchor is None or int(self.anchor[2] // 900) != window:
            self.anchor = point
        aq, ai, _, am = self.anchor
        if abs(accumulated_w - aq - (import_kwh - ai) * 4000) > 8:
            self.anchor = point
            return None
        if monotonic - am < 30:
            return None
        return QuarterReading(accumulated_w, epoch)


@dataclass(frozen=True)
class EnergyDecision:
    battery_target_w: float
    grid_target_w: float
    correction_w: float
    grid_limit_w: float
    night_window: bool
    quarter_used: bool
    limited: bool


def in_charge_window(minute: int, start: int, end: int) -> bool:
    """Start inclusive, end exclusive; equal endpoints disable the window."""
    if not 0 <= minute < 1440 or start == end:
        return False
    if start < end:
        return start <= minute < end
    return minute >= start or minute < end


def decide_energy(
    config: EnergyPolicyConfig,
    *,
    grid_w: float,
    battery_w: float,
    epoch: float,
    local_minute: int | None,
    quarter: QuarterReading | None = None,
) -> EnergyDecision:
    """Choose an aggregate target, allowing peak discharge to preempt charging.

    Missing/stale quarter data uses the instantaneous threshold. Missing local
    time disables scheduled charging. Invalid power/time inputs raise: the
    adapter must use CT002's meter-failure path, never substitute zero.
    A hardware output limit is a request, not proof of delivered power.
    """
    if not all(isfinite(v) for v in (grid_w, battery_w, epoch)) or epoch < 0:
        raise ValueError("finite power and nonnegative epoch required")
    demand = grid_w + battery_w
    if not isfinite(demand):
        raise ValueError("invalid reconstructed demand")
    night = (
        config.night_charge_enabled
        and local_minute is not None
        and in_charge_window(
            local_minute, config.charge_start_minute, config.charge_end_minute
        )
    )
    limit = config.threshold_w
    quarter_used = False
    if config.quarter_enabled and quarter is not None:
        q, sampled = quarter.accumulated_w, quarter.sample_epoch
        if (
            isfinite(q)
            and q >= 0
            and isfinite(sampled)
            and sampled >= 0
            and 0 <= epoch - sampled <= 3
            and int(epoch // 900) == int(sampled // 900)
        ):
            remaining = 900 - epoch % 900
            budget_ws = (config.threshold_w - q) * 900
            # Count only import during sample age and response delay. Never
            # credit export or unconfirmed future battery output.
            delay = min(config.response_margin_s, remaining)
            budget_ws -= max(0.0, grid_w) * (epoch - sampled + delay)
            if remaining > delay:
                limit = max(0.0, budget_ws / (remaining - delay))
            else:
                # No controllable time left: retain the instantaneous limit
                # only if the remaining budget covers the response delay.
                limit = config.threshold_w if budget_ws >= 0 else 0.0
            quarter_used = True

    # Preserve energy below the limit outside the window. During the window,
    # ask for charging only to the extent the remaining import budget allows.
    requested = max(
        demand - limit,
        -min(config.night_charge_power_w, config.max_charge_w) if night else 0.0,
    )
    if demand < 0:
        # Solar charging is allowed at all times, without importing by accident.
        requested = min(requested, demand)
    target = max(-config.max_charge_w, min(config.max_discharge_w, requested))
    net_target = demand - target
    return EnergyDecision(
        battery_target_w=target,
        grid_target_w=net_target,
        correction_w=grid_w - net_target,
        grid_limit_w=limit,
        night_window=night,
        quarter_used=quarter_used,
        limited=abs(requested - target) > 1e-6,
    )
