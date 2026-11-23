from alphamacro.telegram_bot.throttle import (
    ActiveUserRegistry,
    SlidingWindowRateLimiter,
)


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


def test_rate_limit_window():
    clock = Clock()
    limiter = SlidingWindowRateLimiter(2, 60, clock=clock)
    assert limiter.check(1) == (True, 0.0)
    assert limiter.check(1)[0] is True
    allowed, retry = limiter.check(1)
    assert allowed is False
    assert 0 < retry <= 60
    clock.now = 61
    assert limiter.check(1)[0] is True


def test_rate_limit_is_per_user():
    limiter = SlidingWindowRateLimiter(1, 60, clock=Clock())
    assert limiter.check(1)[0]
    assert limiter.check(2)[0]
    assert not limiter.check(1)[0]


def test_active_registry():
    reg = ActiveUserRegistry()
    assert reg.try_acquire(1)
    assert not reg.try_acquire(1)
    reg.release(1)
    assert reg.try_acquire(1)
