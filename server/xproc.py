"""One process does a piece of work while the server's other processes wait for its result (2026-10-23).

The server's processes (the main one, the two terrain-block processes, the far-tile, road-tile and far-building
workers) all start on a new place at once and need the same things: a fresh place's network log on a PC showed 51 %
of the Overture requests and 72 % of the OpenStreetMap ones repeated by another process within ~0.1 s (146 of ~400 MB),
and every process planned the same road cells (plancache.py). The in-process locks (threads) never saw each other.

once(lock, ready, ...) before such a piece of work, whose result lands in a file: ready() returns the result when the
file is there. A lock file (O_EXCL) says who does it; the others poll ready() while it exists. The lock holds how
urgent its holder is (the request kind's rank, then the process's PRIORITY, the niceness the server gave it): a
process waits only for a holder at least as urgent as itself, so the terrain block under the car never waits behind
a background fetch or a road tile's process below it. A lock older than STALE_S (its process died) is taken over; a
wait longer than wait_s does the work itself.
"""
import os
import threading
import sys
import time

STALE_S = 120.0
POLL_S = 0.05
PRIORITY = 0                # this process's niceness above the server's (blockworker.init's nice)
KIND_RANK = {"urgent": 0, "demand": 1, "prefetch": 2, "background": 3, "predownload": 4}
# Where niceness exists (os.nice): a process skips a lock held by a less favoured one, which runs slower. On Windows
# every server process runs at one priority (no os.nice), so the holder is as fast as the one asking, and doing the
# work twice only halved both (the CPU audit, 2026-10-26): there only the request kind counts.
NICE_RANKS = hasattr(os, "nice") and os.environ.get("BNGE_XPROC_NICE", "1") != "0"
# The holder's heartbeat (2026-10-26): every HEARTBEAT_S the process touches the locks it holds, so a lock is stale
# only when its process stopped touching it, not after STALE_S of honest work (a fresh place's cell plans took up to
# 87 s on 4 cores, longer on a game PC's 2 server cores), and a waiter past wait_s keeps waiting for a holder that is
# alive (at most LIVE_WAIT_X times wait_s) instead of doing the same work again. BNGE_XPROC_BEAT=0: as before.
BEAT_ON = os.environ.get("BNGE_XPROC_BEAT", "1") != "0"
HEARTBEAT_S = 5.0
LIVE_S = 20.0               # a lock touched this recently has a live holder (BEAT_ON)
LIVE_WAIT_X = 3.0
HOLD_MAX_S = 900.0          # a lock held longer is no longer touched (a holder that never released it)
_held = {}                  # lock path -> monotonic time taken (this process's)
_held_lock = threading.Lock()
_beat = None


def _rank(kind):
    return KIND_RANK.get(kind, 1), int(PRIORITY) if NICE_RANKS else 0


def _beating():
    while True:
        time.sleep(HEARTBEAT_S)
        now = time.monotonic()
        with _held_lock:
            locks = [lk for lk, t in _held.items() if now - t < HOLD_MAX_S]
        for lk in locks:
            try:
                os.utime(lk, None)
            except OSError:
                pass


def _hold(lock):
    global _beat
    if not BEAT_ON:
        return
    with _held_lock:
        _held[lock] = time.monotonic()
        if _beat is None:
            _beat = threading.Thread(target=_beating, daemon=True, name="xproc-beat")
            _beat.start()


def _live(lock):
    """Whether lock's holder touched it within LIVE_S (heartbeat)."""
    try:
        return time.time() - os.path.getmtime(lock) <= LIVE_S
    except OSError:
        return False


def stale_s():
    """Seconds after which an untouched lock's holder counts as gone."""
    return LIVE_S * 3 if BEAT_ON else STALE_S


def take(lock, kind="demand"):
    """True when this process now holds lock (or no lock file can be made: then it does the work unlocked); False
    when another process holds it (a stale lock is taken over)."""
    for _ in range(2):
        try:
            os.makedirs(os.path.dirname(lock), exist_ok=True)
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock) > stale_s():
                    release(lock)           # its process died doing it
                    continue
            except OSError:
                continue                    # (released meanwhile)
            return False
        except OSError:
            return True
        try:
            os.write(fd, ("%d %d %d" % (*_rank(kind), os.getpid())).encode())
        finally:
            os.close(fd)
        _hold(lock)
        return True
    return False


def _holder(lock):
    """(rank, priority, pid or None) of lock's holder, or None (released, or being written)."""
    try:
        with open(lock, "rb") as fh:
            held = fh.read().split()
        return int(held[0]), int(held[1]), (int(held[2]) if len(held) > 2 else None)
    except (OSError, ValueError, IndexError):
        return None


def _less_urgent(lock, kind):
    """Whether lock's holder is less urgent than this process asking for kind."""
    h = _holder(lock)
    return h is not None and h[:2] > _rank(kind)


def alive(pid):
    """Whether process pid still runs (True when it cannot be told)."""
    if pid is None or pid <= 0 or pid == os.getpid():
        return True
    if sys.platform == "win32":
        try:
            return _alive_windows(int(pid))
        except Exception:               # (never a reason to fail a download: waited out as before)
            return True
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return True


def _alive_windows(pid):
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE          # (64-bit handles: budget.lower_priority)
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)            # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return ctypes.get_last_error() == 5             # (access denied: it exists)
    try:
        code = wintypes.DWORD()
        return not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value == 259     # STILL_ACTIVE
    finally:
        k32.CloseHandle(h)


def once(lock, ready, wait_s, kind="demand", on_wait=None):
    """(result, False): ready() gave it (at once or after waiting for another process); (None, True): this process
    does the work (release(lock) after the result is written); (None, False): do it without the lock (a wait past
    wait_s, or a less urgent holder). on_wait(): called once when this process starts waiting."""
    got = ready()
    if got is not None:
        return got, False
    t0 = time.monotonic()
    waited = False
    holder = None
    while True:
        if take(lock, kind):
            got = ready()                   # (written by the process whose lock we just followed)
            if got is not None:
                release(lock)
                return got, False
            return None, True
        # the holder's urgency read once per holder (a lock file of another time or size: a new holder), not at every
        # poll: on Windows a lock open for reading cannot be removed, and its holder's release failed (below)
        try:
            st = os.stat(lock)
            now_holder = (st.st_mtime_ns, st.st_size)
        except OSError:
            now_holder = None
        if now_holder is not None and now_holder[1] > 0 and now_holder != holder:
            holder = now_holder
            h = _holder(lock)
            if h is not None and not alive(h[2]):
                # its process is gone (the game closed mid-download: the server's processes end with it); the
                # lock was waited out for STALE_S before, a minute a block after a quick restart
                release(lock)
                holder = None
                continue
            if h is not None and h[:2] > _rank(kind):
                return None, False          # do it here, not wait
        if not waited:
            waited = True
            if on_wait is not None:
                on_wait()
        waited_s = time.monotonic() - t0
        if waited_s > wait_s and not (BEAT_ON and waited_s < wait_s * LIVE_WAIT_X and _live(lock)):
            return None, False
        time.sleep(POLL_S)
        got = ready()
        if got is not None:
            return got, False


RELEASE_S = 2.0         # s a release keeps trying to remove its lock file


def release(lock):
    """Remove the lock file. On Windows a file another process has open (a waiter reading its urgency) cannot be
    removed: tried again for up to RELEASE_S. A failed remove left the lock behind, so every process wanting that piece
    waited out its wait_s and the next ones STALE_S (road tiles "building for 90 s", the road workers "stuck", on
    Windows since the locks came in 1.6.1: GitHub #435, PR #481's diagnosis)."""
    with _held_lock:
        _held.pop(lock, None)
    t0 = time.monotonic()
    while True:
        try:
            os.remove(lock)
            return
        except FileNotFoundError:
            return
        except OSError:
            if time.monotonic() - t0 > RELEASE_S:
                return
            time.sleep(0.01)
