"""StreamHub: supervises one driver per configured channel and fans frames
out to RTSP subscribers.

Lifecycle is on-demand by default: the driver connects when the first
subscriber arrives (or a DESCRIBE needs SPS/PPS) and disconnects `linger`
seconds after the last one leaves. Legacy DVR boards have tiny concurrent-
connection budgets, so holding sockets open for no viewer is rude at best.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Callable

from .drivers.base import Driver, DriverError, StreamInfo
from .h264 import NAL_PPS, NAL_SPS, is_vcl, iter_nals, nal_type

log = logging.getLogger("dvrbridge.hub")

_BACKOFF_START = 1.0
_BACKOFF_CAP = 15.0
_STABLE_RESET = 30.0  # a connection alive this long resets the backoff


@dataclass
class HubFrame:
    nals: list[bytes]
    ts90k: int
    keyframe: bool


@dataclass(eq=False)  # identity semantics: subscribers live in a set
class Subscriber:
    queue: asyncio.Queue = field(default_factory=lambda: asyncio.Queue(maxsize=64))
    waiting_key: bool = True
    dropped: int = 0


class TimestampMapper:
    """Map device ms timestamps onto a continuous 90 kHz RTP timeline.

    Device clocks jump on reconnect and occasionally glitch; deltas are
    clamped and accumulated so the RTP timeline never goes backwards.
    """

    def __init__(self) -> None:
        self._last_ms: int | None = None
        self._last_wall: float | None = None
        self._typical_ms = 100.0
        self._rtp = 0

    def map(self, ts_ms: int | None) -> int:
        now = time.monotonic()
        if ts_ms is None or self._last_ms is None:
            if self._last_wall is None:
                delta_ms = 0.0
            else:
                delta_ms = min(max((now - self._last_wall) * 1000.0, 1.0), 5000.0)
        else:
            delta_ms = float((ts_ms - self._last_ms) & 0xFFFFFFFF)
            if not 0.0 <= delta_ms <= 5000.0:  # reconnect jump or backwards glitch
                delta_ms = self._typical_ms
            elif delta_ms > 0.0:  # 0 = same capture instant (multi-slice AU): keep
                self._typical_ms = 0.9 * self._typical_ms + 0.1 * delta_ms
        self._rtp = (self._rtp + int(delta_ms * 90)) & 0xFFFFFFFF
        self._last_ms = ts_ms
        self._last_wall = now
        return self._rtp


class Stream:
    def __init__(
        self,
        name: str,
        driver_factory: Callable[[], Driver],
        always_on: bool = False,
        linger: float = 10.0,
    ) -> None:
        self.name = name
        self._factory = driver_factory
        self.always_on = always_on
        self.linger = linger
        self.sps: bytes | None = None
        self.pps: bytes | None = None
        self.info: StreamInfo = StreamInfo()
        self.frames_relayed = 0
        self.reconnects = 0
        self._subs: set[Subscriber] = set()
        self._ready = asyncio.Event()
        self._wake = asyncio.Event()  # cut a reconnect backoff short on new sub
        self._task: asyncio.Task | None = None
        self._last_activity = time.monotonic()

    # ---- subscriber API (called by the RTSP server) ----

    def touch(self) -> None:
        self._last_activity = time.monotonic()
        self._ensure_running()

    async def wait_ready(self, timeout: float = 10.0) -> None:
        """Ensure the driver is up and SPS/PPS are cached (for DESCRIBE)."""
        self.touch()
        await asyncio.wait_for(self._ready.wait(), timeout)

    def subscribe(self) -> Subscriber:
        sub = Subscriber()
        self._subs.add(sub)
        self._wake.set()  # if _run is mid-backoff, reconnect now, not in 15s
        self.touch()
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        self._subs.discard(sub)
        self._last_activity = time.monotonic()

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    # ---- supervision ----

    def _ensure_running(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name=f"stream:{self.name}"
            )

    def _idle_expired(self) -> bool:
        return (
            not self.always_on
            and not self._subs
            and time.monotonic() - self._last_activity > self.linger
        )

    async def _run(self) -> None:
        backoff = _BACKOFF_START
        try:
            while not self._idle_expired():
                driver = self._factory()
                mapper = TimestampMapper()
                started = time.monotonic()
                gen = driver.frames()
                try:
                    async for frame in gen:
                        self.info = driver.info
                        self._on_frame(frame.payload, mapper.map(frame.ts_ms), frame.keyframe)
                        if self._idle_expired():
                            log.info("%s: idle, disconnecting from device", self.name)
                            return
                except (DriverError, OSError) as e:
                    log.warning("%s: driver failed: %s", self.name, e)
                finally:
                    # close the generator NOW so the driver's finally releases
                    # the DVR's connection slot immediately
                    await gen.aclose()
                if time.monotonic() - started > _STABLE_RESET:
                    backoff = _BACKOFF_START
                if self._idle_expired():
                    return
                self.reconnects += 1
                # drop cached parameter sets: a reconnected device may return a
                # different resolution/profile, so a DESCRIBE must wait for the
                # NEW SPS/PPS rather than advertise the stale ones
                self._ready.clear()
                self.sps = self.pps = None
                # interruptible backoff: a new subscriber wakes us immediately
                # instead of waiting out the full delay
                self._wake.clear()
                try:
                    await asyncio.wait_for(self._wake.wait(), backoff)
                except TimeoutError:
                    pass
                backoff = min(backoff * 2, _BACKOFF_CAP)
        finally:
            self._ready.clear()
        # Reached only on NORMAL loop exit (idle) — cancellation propagates out
        # through the finally and skips this. Clear the task handle and, if a
        # subscriber raced in during teardown (added a sub but _ensure_running
        # saw a not-yet-done task), restart so it can never deadlock waiting on
        # an empty queue. This block has no await, so subscribe() from another
        # task cannot interleave with the check.
        self._task = None
        if self._subs:
            self._ensure_running()

    def _on_frame(self, payload: bytes, ts90k: int, keyframe: bool) -> None:
        nals = [n for n in iter_nals(payload) if n]
        if not nals:
            return
        for n in nals:
            t = nal_type(n)
            if t == NAL_SPS:
                self.sps = n
            elif t == NAL_PPS:
                self.pps = n
        if self.sps and self.pps and not self._ready.is_set():
            self._ready.set()
        self.frames_relayed += 1
        # ensure a decoder joining at this AU has parameter sets
        if keyframe and self.sps and self.pps and not any(
            nal_type(n) == NAL_SPS for n in nals
        ):
            nals = [self.sps, self.pps, *nals]
        item = HubFrame(nals=nals, ts90k=ts90k, keyframe=keyframe)
        for sub in self._subs:
            if sub.waiting_key:
                if not keyframe:
                    continue
                sub.waiting_key = False
            try:
                sub.queue.put_nowait(item)
            except asyncio.QueueFull:
                # slow client: drop everything until the next keyframe
                sub.dropped += 1
                sub.waiting_key = True


class StreamHub:
    def __init__(self) -> None:
        self.streams: dict[str, Stream] = {}

    def add(self, stream: Stream) -> None:
        if stream.name in self.streams:
            raise ValueError(f"duplicate stream name: {stream.name}")
        self.streams[stream.name] = stream

    def get(self, name: str) -> Stream:
        return self.streams[name]

    async def start_always_on(self) -> None:
        for s in self.streams.values():
            if s.always_on:
                s.touch()

    async def stop(self) -> None:
        await asyncio.gather(*(s.stop() for s in self.streams.values()))
