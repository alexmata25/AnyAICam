"""Own activity finalizers across worker-thread submission and app shutdown.

Timeouts are reported as incomplete media work, never as successful flushing.
No recording or buffer files are removed by this owner.
"""
import asyncio
import logging
import threading


class ActivityFinalizers:
    def __init__(self):
        self._lock = threading.Lock()
        self._futures = set()
        self._tasks = set()
        self._external_tasks = set()
        self.stopping = False

    def start(self):
        with self._lock:
            if self._futures or self._tasks or any(not task.done() for task in self._external_tasks):
                raise RuntimeError('previous activity finalizers have not stopped')
            self._external_tasks.clear()
            self.stopping = False

    def submit(self, coroutine, loop, *, name='ai_activity_finalizer'):
        async def owned():
            task = asyncio.current_task()
            task.set_name(name)
            with self._lock:
                self._tasks.add(task)
            try:
                return await coroutine
            finally:
                with self._lock:
                    self._tasks.discard(task)

        with self._lock:
            if self.stopping:
                coroutine.close()
                raise RuntimeError('activity finalizers are shutting down')
            wrapper = owned()
            try:
                future = asyncio.run_coroutine_threadsafe(wrapper, loop)
            except BaseException:
                wrapper.close()
                coroutine.close()
                raise
            self._futures.add(future)
        # Register outside the lock: already-completed futures invoke inline.
        def finished(done):
            with self._lock:
                self._futures.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None:
                    logging.getLogger(__name__).error('Activity finalizer failed: %s', error)
        future.add_done_callback(finished)
        return future

    async def drain(self, *, timeout=20.0, cancel_timeout=2.0, extra_tasks=()):
        """Seal admission, finish accepted work, then bound cancellation cleanup.

        Call on the owning application loop, while recording/buffers still run.
        The separate cancellation bound cannot guarantee cancellation-resistant
        native/thread work has stopped; remaining work is explicitly reported.
        """
        with self._lock:
            self.stopping = True
            futures = list(self._futures)
        waiting = {asyncio.wrap_future(future) for future in futures}
        extra_tasks = set(extra_tasks)
        with self._lock:
            self._external_tasks.update(extra_tasks)
        if any(task.get_loop() is not asyncio.get_running_loop() for task in extra_tasks):
            raise RuntimeError('clip task belongs to a different event loop')
        waiting.update(extra_tasks)
        for future in waiting:
            # A cancellation-resistant operation may outlive this bounded wait.
            # Its eventual error is already logged by the submission callback.
            future.add_done_callback(lambda done: None if done.cancelled() else done.exception())
        if not waiting:
            return {'accepted': 0, 'timed_out': 0, 'remaining': 0, 'failed': 0}
        done, pending = await asyncio.wait(waiting, timeout=timeout)
        timed_out = len(pending)
        if pending:
            # A concurrent Future becomes done on cancel before its coroutine's
            # finally finishes. Cancel actual Tasks and await those Tasks instead.
            with self._lock:
                tasks = list(self._tasks | extra_tasks)
            logging.getLogger(__name__).error('Activity finalizer drain timed out: %s',
                                             [task.get_name() for task in tasks])
            if any(task.get_loop() is not asyncio.get_running_loop() for task in tasks):
                raise RuntimeError('activity finalizer belongs to a different event loop')
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.wait(tasks, timeout=cancel_timeout)
            await asyncio.sleep(0)  # run future completion/exception callbacks
        failed = sum(1 for future in waiting if future.done()
                     and (future.cancelled() or future.exception() is not None))
        with self._lock:
            remaining = sum(not task.done() for task in self._tasks)
            remaining += sum(not future.done() for future in self._futures) if not self._tasks else 0
            remaining += sum(not task.done() for task in extra_tasks - self._tasks)
        return {'accepted': len(waiting), 'timed_out': timed_out,
                'remaining': remaining, 'failed': failed}
