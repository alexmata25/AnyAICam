"""Health requests must remain responsive while maintenance state is slow."""

import asyncio
import threading
from types import SimpleNamespace

import httpx
import main


def test_slow_maintenance_state_read_does_not_block_asgi_loop(monkeypatch):
    read_started = threading.Event()
    read_finished = threading.Event()
    release_read = threading.Event()

    def slow_state_read():
        read_started.set()
        release_read.wait(timeout=2)
        read_finished.set()
        return {"enabled": False}

    monkeypatch.setattr(main, "load_maintenance_state", slow_state_read)

    async def next_handler(_request):
        return "ok"

    async def exercise():
        request = SimpleNamespace(url=SimpleNamespace(path="/health"))
        middleware = asyncio.create_task(
            main.maintenance_mode_middleware(request, next_handler)
        )

        # If middleware performs the file read on-loop, this sleep cannot run
        # until the slow read times out. With the read offloaded, it runs while
        # the worker is still blocked on release_read.
        await asyncio.sleep(0.05)
        loop_progressed_during_read = read_started.is_set() and not read_finished.is_set()
        release_read.set()
        result = await asyncio.wait_for(middleware, timeout=1)
        return loop_progressed_during_read, result

    progressed, result = asyncio.run(exercise())
    assert progressed, "maintenance file read blocked the ASGI event loop"
    assert result == "ok"


def test_concurrent_health_requests_do_not_serialize_on_slow_state_read(monkeypatch):
    entered = 0
    peak_concurrent_reads = 0
    lock = threading.Lock()
    release_reads = threading.Event()

    def slow_state_read():
        nonlocal entered, peak_concurrent_reads
        with lock:
            entered += 1
            peak_concurrent_reads = max(peak_concurrent_reads, entered)
        try:
            release_reads.wait(timeout=2)
            return {"enabled": False}
        finally:
            with lock:
                entered -= 1

    monkeypatch.setattr(main, "load_maintenance_state", slow_state_read)

    async def exercise():
        timer = threading.Timer(1.0, release_reads.set)
        timer.start()
        try:
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                responses = await asyncio.gather(
                    *(client.get("/health") for _ in range(8))
                )
        finally:
            release_reads.set()
            timer.cancel()
        return responses

    responses = asyncio.run(exercise())
    assert peak_concurrent_reads > 1, "health requests serialized behind maintenance I/O"
    assert all(response.status_code == 200 for response in responses)


def test_maintenance_middleware_preserves_normal_and_maintenance_behavior(monkeypatch):
    request = SimpleNamespace(url=SimpleNamespace(path="/dashboard"))
    calls = []

    async def next_handler(_request):
        calls.append("next")
        return "served"

    async def exercise():
        monkeypatch.setattr(main, "load_maintenance_state", lambda: {"enabled": False})
        normal = await main.maintenance_mode_middleware(request, next_handler)

        monkeypatch.setattr(
            main,
            "load_maintenance_state",
            lambda: {"enabled": True, "message": "Scheduled maintenance"},
        )
        maintenance = await main.maintenance_mode_middleware(request, next_handler)
        return normal, maintenance

    normal, maintenance = asyncio.run(exercise())
    assert normal == "served"
    assert calls == ["next"]
    assert maintenance.status_code == 503
    assert b"Scheduled maintenance" in maintenance.body
