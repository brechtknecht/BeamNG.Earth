"""Separate cores for the world server and the game (Linux; a recorded session 2026-10-15, Las Vegas: whenever the
server's workers were busy the game fell to 12-24 fps with its physics at 0.5-0.9 of real time, even at idle
scheduling priority: the workers ran on the other half of the game's physical cores, hyperthreads sharing the cores'
execution units and caches. With the server on two cores and the game on the other six: 51-56 fps and the physics at
real time while the server worked; the server alone pinned did not help, the game's threads still landed there).

    server_cpus, game_cpus = split()              # the CPU sets, or (None, None) when it cannot be done
    pin_self(server_cpus)                          # the server (its threads and worker processes inherit it)
    pin_game(game_cpus)                            # every thread of the running game
"""
import os

EXE = "BeamNG.drive.x64"
SERVER_SHARE = 0.25         # of the physical cores, at least one, never more than half


def _siblings(usable=None):
    """[(core key, [cpu ids])] of the usable CPUs (default: this process's) grouped by physical core, in core order."""
    if usable is None:
        usable = sorted(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else []
    groups = {}
    for cpu in usable:
        base = f"/sys/devices/system/cpu/cpu{cpu}/topology/"
        try:
            core = int(open(base + "core_id").read())
            pkg = int(open(base + "physical_package_id").read())
        except (OSError, ValueError):
            core, pkg = cpu, 0
        groups.setdefault((pkg, core), []).append(cpu)
    return sorted(groups.items())


def split(share=SERVER_SHARE):
    """(server CPU set, game CPU set): the last cores (with their hyperthread siblings) for the server."""
    cores = _siblings()
    if len(cores) < 4:
        return None, None              # too few cores to split usefully
    k = max(1, min(len(cores) // 2, int(round(len(cores) * share))))
    server = {c for _, cpus in cores[-k:] for c in cpus}
    game = {c for _, cpus in cores[:-k] for c in cpus}
    return server, game


def pin_self(cpus):
    if cpus and hasattr(os, "sched_setaffinity"):
        os.sched_setaffinity(0, cpus)


def game_pids():
    """The running game's main processes (not its CEF helpers) of this user."""
    out = []
    uid = os.getuid() if hasattr(os, "getuid") else None
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            if uid is not None and os.stat(f"/proc/{d}").st_uid != uid:
                continue
            exe = os.path.basename(os.readlink(f"/proc/{d}/exe"))
            if exe != EXE:
                continue
            with open(f"/proc/{d}/cmdline", "rb") as f:
                if b"--type=" in f.read():
                    continue
            out.append(int(d))
        except OSError:
            continue
    return out


def pin_game(cpus):
    """Every thread of the game on cpus; the number of threads set (0: no game, or not allowed)."""
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    n = 0
    for pid in game_pids():
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n


# --- the loading boost ------------------------------------------------------------------------------------------------
# Right after a teleport the player stands and waits for the world (2026-10-16: ~30 s in Fells Point, the server's
# four CPUs busy throughout while the game's twelve mostly idled). Until the far field is done or the car moves, the
# server's processes may also run on half of the game's cores (the last ones); they keep their idle scheduling, so a
# game thread that wants a CPU still gets it. Then they go back to their own cores.
LEND_SHARE = 0.5            # of the game's physical cores


def boosted(server_cpus, game_cpus, share=LEND_SHARE):
    """The server's CPU set while loading: its own plus the last `share` of the game's physical cores (with their
    hyperthread siblings); None when there is nothing to lend."""
    if not server_cpus or not game_cpus:
        return None
    # (every CPU of the split: the server process itself is pinned to its own by now)
    cores = [cpus for _, cpus in _siblings(sorted(set(server_cpus) | set(game_cpus))) if set(cpus) <= set(game_cpus)]
    k = int(len(cores) * share)
    if k <= 0:
        return None
    return set(server_cpus) | {c for cpus in cores[-k:] for c in cpus}


def _children():
    kids = {}
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        kids.setdefault(ppid, []).append(int(d))
    return kids


def pin_tree(cpus, root=None):
    """Every thread of this process (or root) and of its descendants on cpus; the number of threads set."""
    if not cpus or not hasattr(os, "sched_setaffinity"):
        return 0
    kids = _children()
    todo, n = [root or os.getpid()], 0
    while todo:
        pid = todo.pop()
        todo += kids.get(pid, [])
        try:
            tids = os.listdir(f"/proc/{pid}/task")
        except OSError:
            continue
        for tid in tids:
            try:
                os.sched_setaffinity(int(tid), cpus)
                n += 1
            except OSError:
                pass
    return n
