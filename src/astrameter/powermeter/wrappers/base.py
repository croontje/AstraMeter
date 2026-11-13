from __future__ import annotations

from typing import TYPE_CHECKING

from astrameter.powermeter.base import Powermeter

if TYPE_CHECKING:
    from astrameter.ct002.energy_policy import MonthlyPeakReading, QuarterReading


class PowermeterWrapper(Powermeter):
    """Base for wrappers that decorate another Powermeter."""

    def __init__(self, wrapped_powermeter: Powermeter) -> None:
        self.wrapped_powermeter = wrapped_powermeter

    async def get_passive_watts(self) -> list[float]:
        return await self.wrapped_powermeter.get_passive_watts()

    def stream_online(self) -> bool | None:
        return self.wrapped_powermeter.stream_online()

    def quarter_reading(self) -> QuarterReading | None:
        return self.wrapped_powermeter.quarter_reading()

    def monthly_peak_reading(self) -> MonthlyPeakReading | None:
        return self.wrapped_powermeter.monthly_peak_reading()

    async def wait_for_message(self, timeout: float = 5) -> None:
        await self.wrapped_powermeter.wait_for_message(timeout)

    async def wait_for_next_message(self, timeout: float = 5) -> None:
        await self.wrapped_powermeter.wait_for_next_message(timeout)

    async def start(self) -> None:
        await self.wrapped_powermeter.start()

    async def stop(self) -> None:
        await self.wrapped_powermeter.stop()

    def reset(self) -> None:
        self.wrapped_powermeter.reset()
