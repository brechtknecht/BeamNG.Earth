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
                    os.remove(lock)         # its process died doing it
                    continue
            except OSError:
                continue                    # (released meanwhile)
            return False
        except OSError:
            return True
        try:
            os.write(fd, ("%d %d" % _rank(kind)).encode())
        finally:
            os.close(fd)
        _hold(lock)
        return True
    return False


def _less_urgent(lock, kind):
    """Whether lock's holder is less urgent than this process asking for kind."""
    try:
        with open(lock, "rb") as fh:
            held = fh.read().split()
        return (int(held[0]), int(held[1])) > _rank(kind)
    except (OSError, ValueError, IndexError):
        return False                        # (released, or being written)


def once(lock, ready, wait_s, kind="demand", on_wait=None):
    """(result, False): ready() gave it (at once or after waiting for another process); (None, True): this process
    does the work (release(lock) after the result is written); (None, False): do it without the lock (a wait past
    wait_s, or a less urgent holder). on_wait(): called once when this process starts waiting."""
    got = ready()
    if got is not None:
        return got, False
    t0 = time.monotonic()
    waited = False
    while True:
        if take(lock, kind):
            got = ready()                   # (written by the process whose lock we just followed)
            if got is not None:
                release(lock)
                return got, False
            return None, True
        if _less_urgent(lock, kind):
            return None, False              # do it here, not wait
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


def release(lock):
    with _held_lock:
        _held.pop(lock, None)
    try:
        os.remove(lock)
    except OSError:
        pass
