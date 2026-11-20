"""Temporary files of the atomic writes into the game's user folder, kept outside it.

The game watches its user folder: every file created or changed there is an onFileChanged event, handled in Lua by
every loaded extension, and the engine's FileWatchers took up to ~1 s in a frame during a 100 mph drive (2026-10-17).
A write as "<path>.tmp" then os.replace() made two events per tile file (3,220 in a 3 km drive, 1,079 of them .tmp).
tmp_for(path) puts the temporary file in "beamngEarth_staging" next to the user folder (the same disk: the rename stays
atomic), so only the finished file shows up. Paths outside a user folder's "temp" (the download cache) keep
"<path>.tmp". Pure but for creating that folder.

replace() is the atomic rename into the game's folder; a destination folder that vanished mid-session is made again
(GitHub #27: a player's session folder under temp/beamngEarth was gone, and every terrain write after it failed).
"""
import itertools
import logging
import os
import threading
import time

STAGING = "beamngEarth_staging"
_made = set()
_lock = threading.Lock()
_serial = itertools.count()
_remade = set()
log = logging.getLogger("earthd.staging")


SWEEP_AGE_S = 3600


def _sweep(d):
    """Temporary files left by a process that ended mid-write (older than SWEEP_AGE_S), once per process."""
    import time
    now = time.time()
    for name in os.listdir(d):
        p = os.path.join(d, name)
        try:
            if name.endswith(".tmp") and now - os.path.getmtime(p) > SWEEP_AGE_S:
                os.remove(p)
        except OSError:
            pass


def staging_dir(path):
    """The staging folder for a file in a user folder's temp tree, or None."""
    p = os.path.abspath(path)
    sep = os.sep
    i = p.rfind(f"{sep}temp{sep}")
    if i <= 0:
        return None
    user = p[:i]                                        # .../BeamNG.drive/current
    return os.path.join(os.path.dirname(user), STAGING)


def tmp_for(path):
    """A temporary path to write path's content to before os.replace(tmp, path)."""
    d = staging_dir(path)
    if d is not None:
        with _lock:
            known = d in _made
        if not known:
            try:
                os.makedirs(d, exist_ok=True)
                _sweep(d)
                with _lock:
                    _made.add(d)
            except OSError:
                d = None
    if d is None:
        return path + ".tmp"
    return os.path.join(d, f"{os.getpid()}_{threading.get_ident()}_{next(_serial)}_{os.path.basename(path)}.tmp")


def replace(tmp, path):
    """os.replace(tmp, path), making path's folder again if it was deleted under us (the game or the player
    clearing its temp folder) instead of failing every write after it."""
    try:
        os.replace(tmp, path)
    except FileNotFoundError:
        if not os.path.exists(tmp):
            raise
        d = os.path.dirname(path)
        os.makedirs(d, exist_ok=True)
        with _lock:
            first = d not in _remade
            _remade.add(d)
        if first:
            log.warning("folder %s was deleted while in use (cleared temp folder?): made again", d)
        os.replace(tmp, path)


REPLACE_WAITS = (0.02, 0.05, 0.1, 0.25, 0.5)    # s between the tries of replace_cached (~0.9 s in all)


def replace_cached(tmp, path):
    """replace(tmp, path) for a cache file whose content is the same whoever writes it (a download, a decoded or built
    copy of one). On Windows a file another thread or process has open, or memory-mapped, cannot be replaced
    (PermissionError, WinError 5: GitHub #67, the DEM tile cache on a cloud PC failed terrain requests): retried
    REPLACE_WAITS; still held but there, that copy is kept and tmp removed. True when tmp went in, False when the
    copy already there was kept."""
    for wait in REPLACE_WAITS + (None,):
        try:
            replace(tmp, path)
            return True
        except PermissionError:
            if wait is None:
                if not os.path.exists(path):
                    raise
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                return False
            time.sleep(wait)
