"""Recovery is retried with backoff and remembered across unavailable outputs."""
import asyncio

import pytest

from allydsp import recoverywatch


class Clocks:
    def __init__(self):
        self.now = 0.0
        self.suspended = 0.0

    def monotonic(self):
        return self.now

    def suspend_elapsed(self):
        return self.suspended

    def advance(self, seconds):
        self.now += seconds


def watcher_for(callback, clocks, **kwargs):
    return recoverywatch.RecoveryWatcher(callback, clock=clocks.monotonic,
                                       suspend_clock=clocks.suspend_elapsed, **kwargs)


def test_first_poll_and_healthy_checks_do_not_claim_a_resume():
    async def scenario():
        clocks, calls = Clocks(), []

        async def check(resumed):
            calls.append(resumed)
            return True

        watcher = watcher_for(check, clocks)
        assert await watcher.poll() is True
        clocks.advance(3)
        assert await watcher.poll() is True
        assert calls == [False, False]

    asyncio.run(scenario())


def test_suspend_is_detected_even_when_monotonic_only_advanced_one_poll():
    async def scenario():
        clocks, calls = Clocks(), []

        async def check(resumed):
            calls.append(resumed)
            return True

        watcher = watcher_for(check, clocks)
        await watcher.poll()
        clocks.advance(3)
        clocks.suspended += 120
        await watcher.poll()
        clocks.advance(3)
        await watcher.poll()
        assert calls == [False, True, False]

    asyncio.run(scenario())


@pytest.mark.parametrize("clock_jitter", [.01, .5, 1.0, -.5])
def test_small_suspend_clock_jitter_is_not_a_resume(clock_jitter):
    async def scenario():
        clocks, calls = Clocks(), []

        async def check(resumed):
            calls.append(resumed)
            return True

        watcher = watcher_for(check, clocks)
        await watcher.poll()
        clocks.advance(3)
        clocks.suspended += clock_jitter
        await watcher.poll()
        assert calls == [False, False]

    asyncio.run(scenario())


def test_failed_checks_back_off_exponentially_with_a_cap():
    async def scenario():
        clocks, calls = Clocks(), []

        async def check(resumed):
            calls.append((clocks.now, resumed))
            return False

        watcher = watcher_for(check, clocks, retry_initial=3, retry_max=12)
        await watcher.poll()  # t=0 -> retry at 3
        for tick in range(1, 34):
            clocks.now = float(tick)
            await watcher.poll()
        assert calls == [(0, False), (3, False), (9, False), (21, False), (33, False)]

    asyncio.run(scenario())


def test_new_resume_bypasses_pending_retry_and_stays_pending_until_success():
    async def scenario():
        clocks, calls = Clocks(), []
        healthy = False

        async def check(resumed):
            calls.append((clocks.now, resumed))
            return healthy

        watcher = watcher_for(check, clocks, retry_initial=3, retry_max=12)
        await watcher.poll()
        clocks.advance(3)
        await watcher.poll()  # retry would now wait until t=9
        clocks.advance(1)
        clocks.suspended += 60
        await watcher.poll()  # resume must be attempted immediately at t=4
        assert calls[-1] == (4, True)
        healthy = True
        clocks.advance(3)
        await watcher.poll()
        assert calls[-1] == (7, True)
        clocks.advance(3)
        await watcher.poll()
        assert calls[-1] == (10, False)

    asyncio.run(scenario())


def test_callback_error_is_retried_and_success_resets_backoff():
    async def scenario():
        clocks, calls = Clocks(), []
        results = [RuntimeError("PipeWire not ready"), False, True, False, True]

        async def check(resumed):
            calls.append((clocks.now, resumed))
            result = results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        watcher = watcher_for(check, clocks, retry_initial=3, retry_max=12)
        assert await watcher.poll() is False
        clocks.advance(3)
        assert await watcher.poll() is False
        clocks.advance(6)
        assert await watcher.poll() is True
        clocks.advance(3)
        assert await watcher.poll() is False
        clocks.advance(3)
        assert await watcher.poll() is True
        assert calls == [(0, False), (3, False), (9, False), (12, False), (15, False)]

    asyncio.run(scenario())


def test_poll_cancellation_propagates_instead_of_becoming_a_retry():
    async def scenario():
        clocks = Clocks()

        async def check(resumed):
            raise asyncio.CancelledError()

        watcher = watcher_for(check, clocks)
        with pytest.raises(asyncio.CancelledError):
            await watcher.poll()

    asyncio.run(scenario())


def test_start_is_idempotent_and_cancel_stops_owned_loop():
    async def scenario():
        entered = asyncio.Event()
        calls = []

        async def check(resumed):
            calls.append(resumed)
            entered.set()
            return True

        watcher = recoverywatch.RecoveryWatcher(check, interval=.01)
        watcher.start()
        owned = watcher._task
        watcher.start()
        assert watcher._task is owned
        await asyncio.wait_for(entered.wait(), timeout=1)
        watcher.cancel()
        watcher.cancel()
        await asyncio.gather(owned, return_exceptions=True)
        count = len(calls)
        await asyncio.sleep(.03)
        assert owned.done() and len(calls) == count

    asyncio.run(scenario())


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), True, None, "0.25"])
def test_tick_interval_must_be_a_finite_positive_number(value):
    async def check(_):
        return True
    with pytest.raises(ValueError, match="intervals"):
        watcher_for(check, Clocks(), tick_interval=value)


async def drive_loop(watcher, clocks, monkeypatch, *, until, on_tick=None):
    """Advance virtual lightweight ticks without real waits or process probes."""
    async def sleep(delay):
        assert 0 < delay <= watcher.tick_interval
        clocks.advance(delay)
        if on_tick:
            on_tick()
        if clocks.now >= until:
            raise asyncio.CancelledError()
    monkeypatch.setattr(recoverywatch.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await watcher.run()


def test_lightweight_ticks_do_not_run_full_health_checks_four_times_per_second(monkeypatch):
    async def scenario():
        clocks, calls = Clocks(), []
        async def check(resumed):
            calls.append((clocks.now, resumed))
            return True
        watcher = watcher_for(check, clocks)
        await drive_loop(watcher, clocks, monkeypatch, until=10)
        assert calls == [(0, False), (3, False), (6, False), (9, False)]
    asyncio.run(scenario())


def test_resume_is_checked_on_next_lightweight_tick_before_full_health_deadline(monkeypatch):
    async def scenario():
        clocks, calls = Clocks(), []
        async def check(resumed):
            calls.append((clocks.now, resumed))
            return True
        def tick():
            if clocks.now == .5:
                clocks.suspended += 120
        watcher = watcher_for(check, clocks)
        await drive_loop(watcher, clocks, monkeypatch, until=4, on_tick=tick)
        assert calls == [(0, False), (.5, True), (3.5, False)]
    asyncio.run(scenario())


def test_pending_resume_does_not_bypass_backoff_on_every_lightweight_tick(monkeypatch):
    async def scenario():
        clocks, calls = Clocks(), []
        async def check(resumed):
            calls.append((clocks.now, resumed))
            return False
        def tick():
            if clocks.now == .5:
                clocks.suspended += 120
        watcher = watcher_for(check, clocks, retry_initial=3, retry_max=12)
        await drive_loop(watcher, clocks, monkeypatch, until=11, on_tick=tick)
        assert calls == [(0, False), (.5, True), (3.5, True), (9.5, True)]
    asyncio.run(scenario())


def test_shorter_health_interval_is_respected_even_with_default_lightweight_tick(monkeypatch):
    async def scenario():
        clocks, calls = Clocks(), []
        async def check(resumed):
            calls.append(clocks.now)
            return True
        watcher = watcher_for(check, clocks, interval=.125)
        await drive_loop(watcher, clocks, monkeypatch, until=.5)
        assert calls == [0, .125, .25, .375]
    asyncio.run(scenario())
