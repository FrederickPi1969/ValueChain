from __future__ import annotations

import time
from dataclasses import dataclass


@dataclass
class RateLimiter:
    requests_per_second: float
    _last_request_ts: float = 0.0

    def wait(self) -> None:
        if self.requests_per_second <= 0:
            return
        min_interval = 1.0 / self.requests_per_second
        now = time.monotonic()
        elapsed = now - self._last_request_ts
        if elapsed < min_interval:
            time.sleep(min_interval - elapsed)
        self._last_request_ts = time.monotonic()
