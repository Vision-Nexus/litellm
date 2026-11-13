from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Final

from litellm.models.session_topics import TopicAttempt, TopicConfig, TopicGenerator, TopicScope, TopicState, TopicStore

_LOG: Final = logging.getLogger("litellm.session_topics")


@dataclass(frozen=True, slots=True)
class TopicJob:
    scope: TopicScope
    text: str = field(repr=False)
    enqueued_at: float


class TopicEngine:
    def __init__(
        self,
        config: TopicConfig,
        store: TopicStore,
        generator: TopicGenerator,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.store = store
        self.generator = generator
        self.clock = clock
        self.queue: asyncio.Queue[TopicJob] = asyncio.Queue(maxsize=config.queue_capacity)
        self.workers: tuple[asyncio.Task[None], ...] = ()
        self.stopping = False
        self.capacity_skips = 0
        self.unassociated_requests = 0

    def start(self) -> None:
        if not self.workers:
            self.workers = tuple(asyncio.create_task(self._run()) for _ in range(self.config.concurrency))

    def submit(self, scope: TopicScope, text: str) -> bool:
        if self.stopping:
            return False
        try:
            self.queue.put_nowait(TopicJob(scope=scope, text=text, enqueued_at=self.clock()))
        except asyncio.QueueFull:
            self.capacity_skips += 1
            return False
        return True

    async def stop(self) -> None:
        self.stopping = True
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.workers = ()

    async def _run(self) -> None:
        while True:
            await self._run_one()

    async def _run_one(self) -> None:
        job: Final = await self.queue.get()
        try:
            remaining: Final = self.config.memory_ttl_seconds - (self.clock() - job.enqueued_at)
            if remaining > 0:
                await asyncio.wait_for(self.process(job), timeout=remaining)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # Never log exceptions that may contain provider request data.
            _LOG.warning("session_topic_processing_failed")
        finally:
            self.queue.task_done()

    async def process(self, job: TopicJob) -> None:
        if self.clock() - job.enqueued_at > self.config.memory_ttl_seconds:
            await self.store.observe(job.scope, self.config.model, TopicState.EXPIRED)
            return
        await self.store.observe(job.scope, self.config.model, TopicState.INSUFFICIENT_INPUT)
        if len(job.text.replace("[redacted]", "").strip()) < 3:
            return
        for number in range(2):
            if await self._attempt(job, repair=number > 0):
                return

    async def _attempt(self, job: TopicJob, repair: bool) -> bool:
        lease: Final = uuid.uuid4().hex
        if not await self.store.claim(job.scope, self.config.model, lease):
            return True
        remaining: Final = self.config.memory_ttl_seconds - (self.clock() - job.enqueued_at)
        if remaining <= 0:
            await self.store.finish(job.scope.session_ref, lease, TopicAttempt(state=TopicState.EXPIRED))
            return True
        try:
            attempt: Final = await asyncio.wait_for(
                self.generator.generate(job.text, repair=repair),
                timeout=min(self.config.timeout_seconds, remaining),
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001  # Provider bodies stay out of logs and persistent state.
            await self.store.finish(job.scope.session_ref, lease, TopicAttempt(state=TopicState.UPSTREAM_FAILURE))
            return False
        await self.store.finish(job.scope.session_ref, lease, attempt)
        return attempt.state in (TopicState.READY, TopicState.INSUFFICIENT_INPUT)
