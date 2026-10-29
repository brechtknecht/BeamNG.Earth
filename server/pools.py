"""Worker process pools that come back when a process dies (GitHub #76, #91: roads that never loaded).

A ProcessPoolExecutor whose process dies (a native crash, the system killing it short of memory) is broken for good:
every later submit raises BrokenProcessPool. The road pool broken that way left a session without a single new road
tile while the terrain and the far buildings went on loading. RespawningPool makes the pool again at the next submit
(at most every RESPAWN_S: a pool whose processes cannot even start does not spawn without end) and logs it.
"""
import functools
import logging
import threading
import time
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import perftrace

log = logging.getLogger("earthd.pools")

RESPAWN_S = 10.0


class RespawningPool:
    """ProcessPoolExecutor(*args, **kwargs) made again when broken. submit / shutdown as the executor's."""

    def __init__(self, name, *args, **kwargs):
        if perftrace.ON:           # (BNGE_TRACE: each process traced under the pool's name)
            kwargs["initializer"] = functools.partial(perftrace.worker_init, name, kwargs.get("initializer"))
        self.name, self._args, self._kwargs = name, args, kwargs
        self._lock = threading.Lock()
        self._pool = ProcessPoolExecutor(*args, **kwargs)
        self._last = -float("inf")
        self.respawns = 0

    def submit(self, fn, *args, **kwargs):
        fn = perftrace.job(fn, self.name, args)
        with self._lock:
            pool = self._pool
        try:
            return pool.submit(fn, *args, **kwargs)
        except BrokenProcessPool:
            return self._renew(pool).submit(fn, *args, **kwargs)

    def _renew(self, broken):
        with self._lock:
            if self._pool is broken:
                now = time.monotonic()
                if now - self._last < RESPAWN_S:
                    raise BrokenProcessPool(f"{self.name} pool broken again within {RESPAWN_S:.0f} s")
                self._last = now
                self.respawns += 1
                log.warning("%s worker process died: its pool made again (%d so far)", self.name, self.respawns)
                try:
                    broken.shutdown(wait=False, cancel_futures=True)
                except Exception:       # (it is broken already)
                    pass
                self._pool = ProcessPoolExecutor(*self._args, **self._kwargs)
            return self._pool

    def prestart(self, fn):
        """Start every process now with a no-op job fn (each submit before the first has started spawns one)."""
        with self._lock:
            pool = self._pool
        for _ in range(getattr(pool, "_max_workers", 1)):
            pool.submit(fn)
    def recycle(self, why):
        """The pool's processes killed and the pool made again: for a worker that is alive but never answers (a
        road tile in flight for minutes left the loading line at "roads & buildings 0/26" for good, Windows,
        1.7.0-beta, KyzoFox): a dead process makes the pool broken (_renew), a hung one never did. The futures in
        flight end with BrokenProcessPool (their callers retry). At most every RESPAWN_S. True when recycled."""
        with self._lock:
            now = time.monotonic()
            if now - self._last < RESPAWN_S:
                return False
            self._last = now
            self.respawns += 1
            old = self._pool
            self._pool = ProcessPoolExecutor(*self._args, **self._kwargs)
        log.warning("%s worker processes restarted (%s; %d so far)", self.name, why, self.respawns)
        for p in list((getattr(old, "_processes", None) or {}).values()):
            try:
                p.kill()
            except Exception:           # (gone already)
                pass
        try:
            old.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        return True

    def shutdown(self, *args, **kwargs):
        with self._lock:
            pool = self._pool
        return pool.shutdown(*args, **kwargs)
