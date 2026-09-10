"""HEARTBEAT_OFFLOOP_20260910: a escrita periódica do heartbeat não bloqueia o event loop."""
import asyncio
import time

from gateway import shutdown_watchdog as sw


def test_slow_heartbeat_write_does_not_block_the_loop(monkeypatch):
    calls = []

    def slow_write(**kwargs):
        calls.append(time.monotonic())
        if len(calls) > 1:
            time.sleep(0.6)  # simula fsync lento sob disco saturado

    monkeypatch.setattr(sw, "write_loop_heartbeat", slow_write)

    async def scenario():
        hb = asyncio.create_task(sw.loop_heartbeat_forever(interval_s=1.0))
        ticks = 0
        t0 = time.monotonic()
        # 1,0 s de sleep + 0,6 s de escrita: se a escrita rodasse no loop, os ticks de 50 ms parariam
        while time.monotonic() - t0 < 1.9:
            await asyncio.sleep(0.05)
            ticks += 1
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        return ticks

    ticks = asyncio.run(scenario())
    assert len(calls) >= 2, calls
    # com o loop livre, ~38 ticks em 1,9 s; bloqueado por 0,6 s daria no máximo ~26
    assert ticks >= 30, ticks


def test_write_failure_does_not_kill_the_task(monkeypatch):
    n = {"calls": 0}

    def failing_write(**kwargs):
        n["calls"] += 1
        if n["calls"] > 1:
            raise OSError("disk full")

    monkeypatch.setattr(sw, "write_loop_heartbeat", failing_write)

    async def scenario():
        hb = asyncio.create_task(sw.loop_heartbeat_forever(interval_s=1.0))
        await asyncio.sleep(2.3)
        alive = not hb.done()
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        return alive

    assert asyncio.run(scenario()) is True
    assert n["calls"] >= 3
