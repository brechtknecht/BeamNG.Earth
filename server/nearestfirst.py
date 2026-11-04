"""A small executor that picks its next job when a worker is free, by a rank asked at that moment.

A queue in arrival order made the stand-in photos of a block a car at 200 mph had already left: they waited 18 s in the
median while the car went 1.6 km (A13, 2026-11-04). Here the job the car reaches next goes first and a job it no longer
needs never runs.
"""
import concurrent.futures
import threading


class NearestFirst:
    """submit(fn, *args) -> a Future. rank(*args) is asked each time a worker picks: the lowest goes first (arrival
    order among equals); None: not wanted any more, its Future gets None and fn never runs."""

    def __init__(self, rank, workers=1, name="nearest-first"):
        self.rank = rank
        self.jobs = []
        self.lock = threading.Lock()
        self.wake = threading.Condition(self.lock)
        self.closed = False
        self.seq = 0
        self.dropped = 0
        self.threads = [threading.Thread(target=self._work, name=f"{name}_{i}", daemon=True) for i in range(workers)]
        for t in self.threads:
            t.start()

    def submit(self, fn, *args):
        fut = concurrent.futures.Future()
        with self.wake:
            if self.closed:
                fut.cancel()
                fut.set_running_or_notify_cancel()
                return fut
            self.seq += 1
            self.jobs.append((self.seq, fn, args, fut))
            self.wake.notify()
        return fut

    def _rank(self, args):
        try:
            return self.rank(*args)
        except Exception:       # (a rank that fails never loses the job)
            return 0

    def _pick(self):
        """The best job and those no longer wanted (taken off the list); call with the lock held."""
        best, best_key, gone = None, None, []
        keep = []
        for job in self.jobs:
            r = self._rank(job[2])
            if r is None:
                gone.append(job)
                continue
            keep.append(job)
            key = (r, job[0])
            if best is None or key < best_key:
                best, best_key = job, key
        if best is not None:
            keep.remove(best)
        self.jobs = keep
        return best, gone

    def _work(self):
        while True:
            with self.wake:
                while not self.jobs and not self.closed:
                    self.wake.wait()
                if self.closed and not self.jobs:
                    return
                job, gone = self._pick()
                self.dropped += len(gone)
            for _, _, _, fut in gone:
                if fut.set_running_or_notify_cancel():
                    fut.set_result(None)
            if job is None:
                continue
            _, fn, args, fut = job
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn(*args))
            except BaseException as e:
                fut.set_exception(e)

    def shutdown(self, wait=True):
        with self.wake:
            self.closed = True
            self.wake.notify_all()
        if wait:
            for t in self.threads:
                t.join()
