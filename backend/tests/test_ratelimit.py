from __future__ import annotations

from app.core.ratelimit import SlidingWindowLimiter


def test_allows_up_to_max_attempts():
    t = [0.0]
    limiter = SlidingWindowLimiter(3, 60, clock=lambda: t[0])
    assert limiter.allow("k") and limiter.allow("k") and limiter.allow("k")
    assert limiter.allow("k") is False


def test_window_slides():
    t = [0.0]
    limiter = SlidingWindowLimiter(2, 10, clock=lambda: t[0])
    assert limiter.allow("k") and limiter.allow("k")
    assert limiter.allow("k") is False
    t[0] = 10.1  # window expired
    assert limiter.allow("k") is True


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(1, 60)
    assert limiter.allow("a") is True
    assert limiter.allow("b") is True
    assert limiter.allow("a") is False


def test_reset_clears_key():
    limiter = SlidingWindowLimiter(1, 60)
    assert limiter.allow("k") is True
    assert limiter.allow("k") is False
    limiter.reset("k")
    assert limiter.allow("k") is True
