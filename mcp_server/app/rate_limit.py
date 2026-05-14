from __future__ import annotations

import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from math import ceil

from fastapi import HTTPException, status


@dataclass
class AuthStartRateLimiter:
    max_requests: int
    window_seconds: int
    _requests: dict[str, deque[float]] = field(default_factory=dict)

    def check(self, *, tenant_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if self.max_requests <= 0 or self.window_seconds <= 0:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="rate_limit_misconfigured",
            )

        now = time.monotonic()
        oldest_allowed = now - self.window_seconds
        key = f"{tenant_id}:{user_id}"
        timestamps = self._requests.setdefault(key, deque())

        while timestamps and timestamps[0] <= oldest_allowed:
            timestamps.popleft()

        if len(timestamps) >= self.max_requests:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail="rate_limited",
                headers={"Retry-After": str(self._retry_after_seconds(timestamps[0], now))},
            )

        timestamps.append(now)

    def _retry_after_seconds(self, oldest_request: float, now: float) -> int:
        retry_after = self.window_seconds - (now - oldest_request)
        return max(1, ceil(retry_after))
