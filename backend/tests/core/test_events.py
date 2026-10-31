import asyncio

from veriloop.core.events import EventBus


async def _collect(bus: EventBus, job_id: str, **kw) -> list[dict]:
    return [e async for e in bus.subscribe(job_id, **kw)]


async def test_live_subscriber_receives_events_until_close() -> None:
    bus = EventBus()
    task = asyncio.create_task(_collect(bus, "j1"))
    await asyncio.sleep(0)
    bus.publish("j1", {"type": "node", "node": "a"})
    bus.publish("j1", {"type": "node", "node": "b"})
    bus.close("j1")
    events = await asyncio.wait_for(task, 1)
    assert [e["node"] for e in events] == ["a", "b"]
    assert [e["seq"] for e in events] == [1, 2]
    assert bus.subscriber_count("j1") == 0


async def test_late_subscriber_gets_replay_then_live_without_duplicates() -> None:
    bus = EventBus()
    bus.publish("j2", {"type": "status", "n": 1})
    bus.publish("j2", {"type": "status", "n": 2})
    task = asyncio.create_task(_collect(bus, "j2"))
    await asyncio.sleep(0)
    bus.publish("j2", {"type": "status", "n": 3})
    bus.close("j2")
    assert [e["n"] for e in await asyncio.wait_for(task, 1)] == [1, 2, 3]


async def test_subscribe_after_close_replays_and_ends_and_after_seq_resumes() -> None:
    bus = EventBus()
    for n in range(1, 5):
        bus.publish("j3", {"n": n})
    bus.close("j3")
    assert [e["n"] for e in await asyncio.wait_for(_collect(bus, "j3"), 1)] == [1, 2, 3, 4]
    assert [e["n"] for e in await asyncio.wait_for(_collect(bus, "j3", after_seq=2), 1)] == [3, 4]
    bus.publish("j3", {"n": 99})  # ignored after close
    assert len(bus.history("j3")) == 4


async def test_multiple_subscribers_and_isolation_between_jobs() -> None:
    bus = EventBus()
    t1 = asyncio.create_task(_collect(bus, "a"))
    t2 = asyncio.create_task(_collect(bus, "a"))
    t3 = asyncio.create_task(_collect(bus, "b"))
    await asyncio.sleep(0)
    bus.publish("a", {"x": 1})
    bus.publish("b", {"x": 2})
    bus.close_all()
    r1, r2, r3 = await asyncio.wait_for(asyncio.gather(t1, t2, t3), 1)
    assert r1 == r2 == [{"x": 1, "seq": 1}] and r3 == [{"x": 2, "seq": 1}]


async def test_history_is_bounded_and_closed_jobs_are_evicted() -> None:
    bus = EventBus(max_events_per_job=3, max_jobs=2)
    for n in range(5):
        bus.publish("k", {"n": n})
    assert [e["n"] for e in bus.history("k")] == [2, 3, 4]
    bus.close("k")
    bus.publish("l", {})
    bus.publish("m", {})
    assert bus.history("k") == []  # oldest closed channel evicted
    assert bus.history("l") and bus.history("m")
