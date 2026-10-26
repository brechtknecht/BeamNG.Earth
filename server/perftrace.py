"""Performance tracing (debugging, off by default): with BNGE_TRACE=<folder> every server process writes what it does to
<folder>/<role>-<pid>.jsonl, one Chrome trace event a line (ph "X" spans, "i" marks, "C" counters, "M" names; ts / dur in
microseconds of the Unix clock, so the processes line up). The profiler in beamng-perf/profiler (record.py, the viewer)
merges a folder into one recording. Without the variable every call here returns at once.

What is traced: every job of a process pool (pools.RespawningPool) or thread pool (its wait in the queue and its run),
the hot calls patched in install() (map data, DEM samples, grading, road plans, cross-process waits), each network
request (netfetch), the block stages (stage), the terrain requests and pose (earthd), and the process's CPU and memory
every CPU_S. Spans shorter than BNGE_TRACE_MIN_MS (default 2) are only counted (the "agg" record, per name)."""
import atexit
import functools
import importlib
import json
import os
import sys
import threading
import time

DIR = os.environ.get("BNGE_TRACE") or None
ON = DIR is not None
MIN_US = float(os.environ.get("BNGE_TRACE_MIN_MS", "2") or 2) * 1000
CPU_S = 0.5
AGG_S = 5.0

_fh = None
_lock = threading.Lock()
_pid = os.getpid()
_role = None
_tids = set()
_agg = {}                   # span name -> [count, total us] (the short ones too)
_OFF = time.time() - time.perf_counter()        # perf_counter -> Unix seconds


def now_us():
    return time.time() * 1e6


def perf_us(t):
    """time.perf_counter() value t in this module's clock (Unix microseconds)."""
    return (t + _OFF) * 1e6


def _write(ev):
    line = json.dumps(ev, separators=(",", ":"), default=str) + "\n"
    with _lock:
        if _fh is not None:
            try:
                _fh.write(line)
            except (OSError, ValueError):
                pass


def _tid():
    tid = threading.get_native_id()
    if tid not in _tids:
        _tids.add(tid)
        _write({"ph": "M", "name": "thread_name", "pid": _pid, "tid": tid,
                "args": {"name": threading.current_thread().name}})
    return tid


def init(role):
    """Start tracing this process as role ("main", "block", "road tile", ...): once, and only with BNGE_TRACE."""
    global _fh, _role, _pid
    if not ON or _fh is not None:
        return
    _pid, _role = os.getpid(), role
    os.makedirs(DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() else "_" for c in role)
    _fh = open(os.path.join(DIR, f"{safe}-{_pid}.jsonl"), "a", buffering=1, encoding="utf-8")
    _write({"ph": "M", "name": "process_name", "pid": _pid, "tid": 0, "ts": now_us(),
            "args": {"name": role, "argv": sys.argv[:1], "cpus": os.cpu_count()}})
    _patch_thread_pools()
    install()
    threading.Thread(target=_sample_loop, daemon=True, name="perftrace").start()
    atexit.register(_flush_agg)


def worker_init(role, initializer, *initargs):
    """A pool process's initializer (pools.RespawningPool with tracing on): traced as role, then its own."""
    init(role)
    if initializer is not None:
        initializer(*initargs)


def complete(name, cat, t0_us, dur_us, args=None, tid=None):
    a = _agg.get(name)
    if a is None:
        a = _agg[name] = [0, 0.0]
    a[0] += 1
    a[1] += dur_us
    if dur_us < MIN_US:
        return
    ev = {"ph": "X", "name": name, "cat": cat, "pid": _pid, "tid": _tid() if tid is None else tid,
          "ts": round(t0_us), "dur": round(dur_us)}
    if args:
        ev["args"] = args
    _write(ev)


class span:
    """with span(name, cat, **args): the block as one span on this thread."""
    __slots__ = ("name", "cat", "args", "t0")

    def __init__(self, name, cat="", **args):
        self.name, self.cat, self.args = name, cat, args

    def __enter__(self):
        self.t0 = time.perf_counter()
        return self

    def __exit__(self, *exc):
        if ON and _fh is not None:
            t1 = time.perf_counter()
            complete(self.name, self.cat, perf_us(self.t0), (t1 - self.t0) * 1e6, self.args)
        return False


def instant(name, cat="", **args):
    if ON and _fh is not None:
        _write({"ph": "i", "s": "p", "name": name, "cat": cat, "pid": _pid, "tid": _tid(), "ts": round(now_us()),
                "args": args})


def counter(name, **values):
    if ON and _fh is not None:
        _write({"ph": "C", "name": name, "pid": _pid, "tid": 0, "ts": round(now_us()), "args": values})


def request(name, cat, t0_perf, t1_perf=None, **args):
    """A span on the process's shared "requests" track (they overlap: the viewer stacks them), e.g. a terrain
    request from its message to terrain_ready."""
    if ON and _fh is not None:
        t1 = time.perf_counter() if t1_perf is None else t1_perf
        complete(name, cat, perf_us(t0_perf), (t1 - t0_perf) * 1e6, args, tid="requests")


def stage(name, t0_perf, cat="block", **args):
    """A stage that began at perf_counter time t0_perf and ends now, on this thread (a block's landcover, ...)."""
    if ON and _fh is not None:
        complete(name, cat, perf_us(t0_perf), (time.perf_counter() - t0_perf) * 1e6, args or None)


def net(host, kind, status, nbytes, elapsed_s, url):
    """One network request (netfetch), ending now: always written (the viewer sums their bytes)."""
    if ON and _fh is not None:
        dur = elapsed_s * 1e6
        a = _agg.setdefault("net " + host, [0, 0.0])
        a[0] += 1
        a[1] += dur
        _write({"ph": "X", "name": "net " + host, "cat": "net", "pid": _pid, "tid": _tid(), "ts": round(now_us() - dur),
                "dur": round(dur), "args": {"kind": kind, "status": status, "bytes": nbytes, "url": url[-90:]}})


def _brief(a):
    """The job's scalar arguments (a block's anchor, lat, lon, ..., cx, cy), for the viewer's details."""
    out = []
    for x in a[:12]:
        if isinstance(x, (bool, int, float)) or (isinstance(x, str) and len(x) <= 40):
            out.append(round(x, 1) if isinstance(x, float) else x)
        elif isinstance(x, tuple) and len(x) <= 4 and all(isinstance(y, (int, float)) for y in x):
            out.append(list(x))
    return out


class Job:
    """A pool job: its wait in the queue (from submit to its start, "wait <pool>") and its run (the function's
    name, cat the pool), on the thread that runs it. Picklable when fn is (process pools)."""

    def __init__(self, fn, pool, args):
        self.fn, self.pool, self.sub, self.a = fn, pool, now_us(), _brief(args)

    def __call__(self, *a, **kw):
        t0 = now_us()
        try:
            return self.fn(*a, **kw)
        finally:
            if ON and _fh is not None:
                t1 = now_us()
                name = getattr(self.fn, "__qualname__", None) or getattr(self.fn, "__name__", None) or "job"
                if t1 - self.sub >= MIN_US:
                    if t0 - self.sub >= MIN_US:
                        complete("wait " + self.pool, "queue", self.sub, t0 - self.sub, {"job": name})
                    complete(name, "job:" + self.pool, t0, t1 - t0, {"a": self.a, "waited_ms": round((t0 - self.sub) / 1000)})


def job(fn, pool, args):
    return Job(fn, pool, args) if ON and _fh is not None else fn


def _patch_thread_pools():
    from concurrent.futures import ThreadPoolExecutor
    orig = ThreadPoolExecutor.submit
    if getattr(orig, "_traced", False):
        return

    def submit(self, fn, /, *args, **kwargs):
        return orig(self, Job(fn, (self._thread_name_prefix or "threads").split("-")[0], args), *args, **kwargs)
    submit._traced = True
    ThreadPoolExecutor.submit = submit


def patch(owner, attr, name, cat, argf=None):
    """owner.attr (a function or method) traced as span name; argf(*a, **kw) -> the span's args."""
    fn = getattr(owner, attr, None)
    if fn is None or getattr(fn, "_traced", False):
        return

    @functools.wraps(fn)
    def run(*a, **kw):
        t0 = time.perf_counter()
        try:
            return fn(*a, **kw)
        finally:
            dur = (time.perf_counter() - t0) * 1e6
            args = None
            if dur >= MIN_US and argf is not None:
                try:
                    args = argf(*a, **kw)
                except Exception:
                    args = None
            complete(name, cat, perf_us(t0), dur, args)
    run._traced = True
    setattr(owner, attr, run)


def _size(x):
    return int(getattr(x, "size", 0) or 0)


# (module, owner attribute or "", function, span name, cat, args)
PATCHES = [
    ("mapdata", "MapData", "cell", "mapdata.cell", "mapdata",
     lambda self, key, layers=None, kind="demand": {"key": str(key), "layers": list(layers or ()), "kind": kind}),
    ("mapdata", "MapData", "layer_arrays", "map layer", "mapdata",
     lambda self, key, layer, kind="demand", *a, **k: {"key": str(key), "layer": layer, "kind": kind}),
    ("dem", "Dem", "sample", "dem.sample", "dem", lambda self, lat, lon, zoom, *a, **k: {"n": _size(lat), "z": zoom}),
    ("heightmap", "", "generate", "heights", "block", lambda frame, gen, path, cx, cy, *a, **k: {"cx": cx, "cy": cy}),
    ("grading", "Grader", "apply", "grade", "grading", lambda self, lat, lon, *a, **k: {"n": _size(lat)}),
    ("grading", "Grader", "_cell_plans", "road plan (cell)", "plan", lambda self, key: {"key": str(key)}),
    ("grading", "Grader", "_make_cell", "road plan build", "plan", lambda self, key, box=None: {"key": str(key),
                                                                                                "region": box is not None}),
    ("grading", "Grader", "region", "road plan (region)", "plan", None),
    ("xproc", "", "once", "xproc.once", "wait", lambda lock, *a, **k: {"lock": os.path.basename(str(lock))}),
]


def install():
    for mod, owner, fn, name, cat, argf in PATCHES:
        try:
            m = importlib.import_module(mod)
            patch(getattr(m, owner) if owner else m, fn, name, cat, argf)
        except Exception:           # (a module this build does not have)
            pass


def _flush_agg():
    if _fh is not None:
        _write({"ph": "M", "name": "agg", "pid": _pid, "tid": 0, "ts": round(now_us()),
                "args": {k: [v[0], round(v[1])] for k, v in list(_agg.items())}})


def _rss_mb():
    try:
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE") / 1e6
    except (OSError, ValueError, AttributeError):
        pass
    try:
        import psutil
        return psutil.Process().memory_info().rss / 1e6
    except Exception:
        return None


def _sample_loop():
    """This process's CPU (cores busy over the last CPU_S, all its threads) and memory; every AGG_S the span totals."""
    last_w, last_c = time.perf_counter(), time.process_time()
    last_agg = last_w
    while True:
        time.sleep(CPU_S)
        w, c = time.perf_counter(), time.process_time()
        vals = {"cores": round((c - last_c) / max(1e-6, w - last_w), 3)}
        rss = _rss_mb()
        if rss is not None:
            vals["rss_mb"] = round(rss)
        counter("cpu", **vals)
        last_w, last_c = w, c
        if w - last_agg >= AGG_S:
            last_agg = w
            _flush_agg()
