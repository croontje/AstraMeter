import functools
import json
import logging
import time
from typing import Any

import aiohttp
from aiohttp import BasicAuth
from jsonpath_ng.ext import parse

from astrameter.ct002.energy_policy import QuarterReading, QuarterTracker

from .base import as_list
from .http_client import HttpPowermeter

# Stdlib logger: avoid importing astrameter.config (config_loader imports powermeter).
logger = logging.getLogger("astrameter")


@functools.lru_cache(maxsize=64)
def _compile(path: str) -> Any:
    """*path* parsed once. jsonpath-ng builds a fresh PLY parser per ``parse``
    call (~10 ms and ~0.5 MiB of short-lived objects), far too much to repeat
    for every reading of every phase."""
    return parse(path)


def extract_json_value(data: Any, path: str) -> float:
    match = _compile(path).find(data)
    if not match:
        raise ValueError("No match found for the JSON path")
    value = match[0].value
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        # A meter that publishes ``null`` (or an object) for a reading it has
        # no answer for right now matches the path but yields nothing to
        # convert. That is a bad reading, not a programming error, so raise
        # what every caller already handles.
        raise ValueError(
            f"JSON path {path!r} matched a non-numeric value: {value!r}"
        ) from exc


class JsonHttpPowermeter(HttpPowermeter):
    def __init__(
        self,
        url: str,
        json_path: str | list[str],
        username: str | None = None,
        password: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.url = url
        self.json_paths = as_list(json_path)
        self.auth = (
            BasicAuth(username or "", password or "") if username or password else None
        )
        self.headers = headers or {}
        self._quarter_tracker = QuarterTracker()
        self._quarter: QuarterReading | None = None

    def quarter_reading(self) -> QuarterReading | None:
        return self._quarter

    def _session_options(self) -> dict[str, Any]:
        return {
            **super()._session_options(),
            "auth": self.auth,
            "headers": self.headers,
        }

    async def get_powermeter_watts(self) -> list[float]:
        self._quarter = None
        try:
            data = await self.get_json(self.url)
        except json.JSONDecodeError as e:
            logger.error("JSON HTTP: failed to decode response: %s", e)
            raise ValueError(f"Invalid JSON response: {e}") from e
        except aiohttp.ClientError as e:
            logger.error("JSON HTTP: request failed: %s", e)
            raise ValueError(f"HTTP request error: {e}") from e
        values = [extract_json_value(data, path) for path in self.json_paths]
        # HomeWizard v1 publishes these registers in the same response as
        # power. Only use them with the unmodified whole-house power field.
        # Other JSON sources continue working without quarter telemetry.
        if self.json_paths == ["$.active_power_w"] and isinstance(data, dict):
            q = data.get("active_power_average_w")
            imported = data.get("total_power_import_kwh")
            if (
                isinstance(q, (int, float))
                and isinstance(imported, (int, float))
                and not isinstance(q, bool)
                and not isinstance(imported, bool)
            ):
                self._quarter = self._quarter_tracker.feed(
                    float(q), float(imported), time.time(), time.monotonic()
                )
        return values
