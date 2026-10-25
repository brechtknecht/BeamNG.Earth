#!/usr/bin/env python3
"""Simulated drive for --near-visual photoreal (personal fork): how late the near photo tiles come while driving.

Starts its own earthd (a free port, a temporary user folder, the given download cache) with start.py's recommended
options, teleports to (lat, lon), waits for the far field at rest, then drives straight at SPEED m/s for DIST m on
HEADING degrees (0 = north), sending poses ten times a second and the terrain-follow requests the mod would (a new
block every 256 m, committed when ready). Engine work (imports) is not modelled: the numbers are the server's side.

Per level (L-2: 64 m, L-1: 128 m tiles) it prints how many seconds after a tile came within its level's radius of
the car the far_swap that shows it arrived (median / p90 / max; "never": not shown before the end), for all near
tiles past the first 200 m, and for the ones the road (the drive's line) passes through. Use a new heading each
run for a cold cache, as on a real drive.

  python3 tools/photoreal_drive.py 28 2500 30
  python3 tools/photoreal_drive.py 15 1500 120 --lat 48.1585 --lon 11.5856 --cache-dir ~/.cache/beamng-earth
"""
import argparse
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "tests"))
sys.path.insert(0, str(ROOT / "server"))
from netclient import Client, stop_server  # noqa: E402
import farfield as ff  # noqa: E402

RADII = {-2: 150.0, -1: 400.0}      # earthd NEAR_RADII[32]
# start.py's recommended options; --pin off: no core split (Linux only; on Windows the server has every core)
SERVER_ARGS = ["--pin", "off", "--deshadow", "full", "--airports", "on", "--sea", "on", "--trees", "on", "--buildings", "on"]


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("speed", type=float, help="m/s")
    ap.add_argument("dist", type=float, help="m")
    ap.add_argument("heading", type=float, nargs="?", default=0.0, help="degrees, 0 = north")
    ap.add_argument("--lat", type=float, default=48.1585)
    ap.add_argument("--lon", type=float, default=11.5856)
    ap.add_argument("--cache-dir", default=os.path.expanduser("~/.cache/beamng-earth"))
    ap.add_argument("--keep-log", help="copy the server's log here")
    a, more = ap.parse_known_args()        # anything else: more earthd options
    extra = SERVER_ARGS + [s for s in more if s != "--"]
    hdg = math.radians(a.heading)
    user = tempfile.mkdtemp(prefix="photoreal_drive_")
    log = open(os.path.join(user, "earthd.log"), "w+", encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(ROOT / "server" / "earthd.py"), "--port", "0", "--user-dir", user,
                             "--cache-dir", a.cache_dir, "--weather", "off", "--gravity", "off",
                             "--update-check", "off", "--near-visual", "photoreal", *extra],
                            stdout=subprocess.PIPE, stderr=log, text=True)
    try:
        run(a, hdg, proc)
    finally:
        stop_server(proc)
        log.close()
        if a.keep_log:
            Path(a.keep_log).write_text(Path(log.name).read_text(encoding="utf-8"), encoding="utf-8")
        print("server log:", a.keep_log or log.name)


def run(a, hdg, proc):
    line = proc.stdout.readline()
    c = Client(int(line.split("127.0.0.1:")[1].split()[0]))
    msgs, lock = [], threading.Lock()

    def reader():
        while True:
            try:
                m = c.recv(timeout=3600)
            except Exception:
                return
            with lock:
                msgs.append((time.monotonic(), m))
    threading.Thread(target=reader, daemon=True).start()

    def wait(pred, timeout):
        end, i = time.monotonic() + timeout, 0
        while time.monotonic() < end:
            with lock:
                for _, m in msgs[i:]:
                    if pred(m):
                        return m
                i = len(msgs)
            time.sleep(0.05)
        raise TimeoutError("no answer from the server")

    c.send({"type": "hello", "protocol": 4, "format": 3, "modVersion": "0.4.0"})
    wait(lambda m: m["type"] == "hello", 60)
    t0 = time.monotonic()
    c.send({"type": "prepare_anchor", "requestId": 1, "lat": a.lat, "lon": a.lon})
    an = wait(lambda m: m["type"] in ("anchor_ready", "anchor_error"), 900)
    if an["type"] != "anchor_ready":
        raise SystemExit(f"anchor: {an}")
    aid, z = an["anchorId"], an["groundZ"]
    print(f"anchor in {time.monotonic() - t0:.1f} s", flush=True)
    c.send({"type": "commit_anchor", "anchorId": aid})
    c.send({"type": "commit_terrain", "anchorId": aid, "cx": 0, "cy": 0})

    def pose(x, y, vx, vy):
        c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": z, "lat": a.lat, "lon": a.lon,
                "speed": math.hypot(vx, vy), "vx": vx, "vy": vy})
    pose(0, 0, 0, 0)
    t0 = time.monotonic()
    wait(lambda m: m["type"] == "far_done", 1800)
    print(f"far field at rest in {time.monotonic() - t0:.1f} s; driving {a.dist:.0f} m at {a.speed * 3.6:.0f} km/h",
          flush=True)

    need = {}                       # key -> (time it came within its level's radius, m driven then)
    cx = cy = 0.0
    req, pending = 2, None
    ux, uy = math.sin(hdg), math.cos(hdg)
    start = time.monotonic()
    while True:
        t = time.monotonic() - start
        d = min(a.speed * t, a.dist)
        x, y = d * ux, d * uy
        moving = d < a.dist
        pose(x, y, a.speed * ux if moving else 0.0, a.speed * uy if moving else 0.0)
        now = time.monotonic()
        for lvl, r in RADII.items():
            s = ff.level_size(lvl)
            for i in range(int((x - r) // s), int((x + r) // s) + 1):
                for j in range(int((y - r) // s), int((y + r) // s) + 1):
                    tl = ff.Tile(lvl, i, j)
                    if tl.key not in need and tl.distance(x, y) <= r:
                        need[tl.key] = (now, d)
        if pending is None and max(abs(x - cx), abs(y - cy)) > 256:
            pending = (req, round(x / 256) * 256.0, round(y / 256) * 256.0)
            c.send({"type": "prepare_terrain", "requestId": req, "anchorId": aid, "cx": pending[1], "cy": pending[2]})
            req += 1
        if pending is not None:
            with lock:
                ready = any(m["type"] in ("terrain_ready", "terrain_error") and m.get("requestId") == pending[0]
                            for _, m in msgs)
            if ready:
                c.send({"type": "commit_terrain", "anchorId": aid, "cx": pending[1], "cy": pending[2]})
                cx, cy, pending = pending[1], pending[2], None
        if not moving and t > a.dist / a.speed + 30:
            break
        time.sleep(0.1)
    c.close()

    shown = {}
    with lock:
        for t, m in msgs:
            if m["type"] == "far_swap":
                for k in m["add"]:
                    shown.setdefault(k, t)

    def on_road(k):
        x0, y0, x1, y1 = ff.tile_of(k).bbox
        return any(x0 - 10 <= ux * d <= x1 + 10 and y0 - 10 <= uy * d <= y1 + 10 for d in range(0, int(a.dist) + 1, 8))

    for label, pick in (("all", lambda k: True), ("on the road", on_road)):
        for lvl in RADII:
            rows = [(shown.get(k), tn) for k, (tn, d) in need.items()
                    if int(k.split("_")[0]) == lvl and d > 200 and pick(k)]
            got = sorted(max(0.0, ts - tn) for ts, tn in rows if ts is not None)
            never = sum(1 for ts, _ in rows if ts is None)
            if got:
                q = lambda p: got[min(len(got) - 1, int(p * len(got)))]     # noqa: E731
                print(f"{label:12s} L{lvl}: {len(rows):4d} tiles, shown {q(0.5):5.1f} s after needed (median), "
                      f"p90 {q(0.9):5.1f} s, max {got[-1]:5.1f} s, never {never}")
            else:
                print(f"{label:12s} L{lvl}: {len(rows):4d} tiles, none shown")


if __name__ == "__main__":
    main()
