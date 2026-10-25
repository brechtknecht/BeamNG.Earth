#!/usr/bin/env python3
"""Headless photoreal drive: a fake game client that drives a road route against a running earthd and measures
how late the photo tiles come where the car is.

It speaks the mod's protocol (hello, prepare_anchor / commit_anchor, prepare_terrain / commit_terrain, pose at
10 Hz with the slots it lets go), follows the route the server's own router plans (OSRM, along real roads) at a
constant speed, and keeps the shown far tiles from far_tile / far_swap / far_remove. No game, no MCP.

The measure: the route is cut into the 64 m L-2 cells it passes through; for each, the time the car entered it and
the time an L-2 tile there was first shown. late = shown - entered (s; negative: it was there before the car).
Also the share of samples in which the ground under the car / 100 m / 200 m ahead (along the route) is shown at
L-2, or at L-1 or finer.

  py tools/photoreal_drive.py --port 47700 --from 48.1374,11.5755 --to 48.1620,11.6200 --speed 25 --out run1
"""
import argparse
import asyncio
import json
import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
import protocol  # noqa: E402

BASE = 256.0
PROTOCOL = 4
POSE_HZ = 10.0
BLOCK_STEP = 128.0          # block centres snap to this
BLOCK_MOVE = 160.0          # m from the committed block's centre before the next is asked for
AHEAD = (0.0, 100.0, 200.0)


def size(level):
    return BASE * 2.0 ** level


def cell(level, x, y):
    s = size(level)
    return f"{level}_{math.floor(x / s)}_{math.floor(y / s)}"


class Route:
    def __init__(self, pts):
        self.p = pts
        self.s = [0.0]
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            self.s.append(self.s[-1] + math.hypot(x1 - x0, y1 - y0))
        self.length = self.s[-1]
        self.k = 0

    def at(self, s):
        """(x, y, heading unit vector) at arc length s."""
        s = min(max(s, 0.0), self.length)
        k = self.k if self.s[self.k] <= s else 0
        while k + 1 < len(self.s) - 1 and self.s[k + 1] < s:
            k += 1
        self.k = k
        (x0, y0), (x1, y1) = self.p[k], self.p[k + 1]
        seg = max(self.s[k + 1] - self.s[k], 1e-9)
        f = (s - self.s[k]) / seg
        return x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, ((x1 - x0) / seg, (y1 - y0) / seg)


def enu(lat0, lon0, lat, lon):
    """Local tangent plane (m): good to a few m over the few km of a drive."""
    k = math.cos(math.radians(lat0))
    return (lon - lon0) * 111319.49 * k, (lat - lat0) * 110574.0


class Client:
    def __init__(self, reader, writer):
        self.r, self.w = reader, writer
        self.inbox = asyncio.Queue()
        self.held = []
        self.shown = {}             # key -> time shown
        self.sent = {}              # key -> (slot, serial) of tiles sent, not yet shown
        self.slot_of = {}           # key -> (slot, serial) of shown tiles
        self.gone = []
        self.first_l2 = {}          # L-2 key -> first time shown
        self.swaps = 0

    def send(self, m):
        self.w.write(protocol.encode(m))

    async def pump(self):
        while True:
            m = await protocol.read_message(self.r)
            if m is None:
                return
            t = m["type"]
            now = time.monotonic()
            if t == "far_tile":
                slot = None
                if "slotSerial" in m and "material" in m:
                    slot = [int(m["material"].rsplit("_", 1)[1]), m["slotSerial"]]
                self.sent[m["key"]] = slot
            elif t == "far_swap":
                self.swaps += 1
                for k in m.get("remove", []):
                    self._drop(k)
                for k in m.get("add", []):
                    self.shown[k] = now
                    self.slot_of[k] = self.sent.pop(k, None)
                    if k.startswith("-2_") and k not in self.first_l2:
                        self.first_l2[k] = now
            elif t == "far_remove":
                k = m["key"]
                if k in self.sent:
                    s = self.sent.pop(k)
                    if s:
                        self.gone.append(s)
                else:
                    self._drop(k)
            else:
                await self.inbox.put(m)

    def _drop(self, k):
        self.shown.pop(k, None)
        s = self.slot_of.pop(k, None)
        if s:
            self.gone.append(s)

    async def wait(self, typ, timeout=600):
        """The next message of type typ (one that came while waiting for another counts: kept in self.held)."""
        for k, m in enumerate(self.held):
            if m["type"] == typ:
                return self.held.pop(k)
        end = time.monotonic() + timeout
        while True:
            m = await asyncio.wait_for(self.inbox.get(), max(0.1, end - time.monotonic()))
            if m["type"] == typ:
                return m
            if m["type"] in ("anchor_error", "terrain_error"):
                raise RuntimeError(m)
            if m["type"] in ("far_done", "terrain_refresh", "terrain_ready", "route_result"):
                self.held.append(m)

    def finest(self, x, y):
        for lvl in range(-2, 11):
            if cell(lvl, x, y) in self.shown:
                return lvl
        return None


async def main_async(a):
    reader, writer = await asyncio.open_connection("127.0.0.1", a.port)
    c = Client(reader, writer)
    pump = asyncio.create_task(c.pump())
    c.send({"type": "hello", "protocol": PROTOCOL, "format": 3})
    lat0, lon0 = (float(v) for v in a.start.split(","))
    lat1, lon1 = (float(v) for v in a.to.split(","))
    # the route once, kept beside the script's runs (the public router rate-limits repeated asks)
    rfile = a.route_file or os.path.join(os.environ.get("TEMP", "."), f"bnge_route_{a.start}_{a.to}.json")
    if os.path.exists(rfile):
        with open(rfile) as f:
            r = json.load(f)
    else:
        c.send({"type": "route", "id": 1, "lat0": lat0, "lon0": lon0, "lat1": lat1, "lon1": lon1})
        r = await c.wait("route_result", 120)
        if not r.get("points"):
            raise SystemExit(f"no route: {r.get('error')}")
        with open(rfile, "w") as f:
            json.dump(r, f)
    lat0, lon0 = r["points"][0]
    pts = [enu(lat0, lon0, la, lo) for la, lo in r["points"]]
    route = Route(pts)
    km = min(route.length, a.km * 1000)
    print(f"route {route.length / 1000:.2f} km, driving {km / 1000:.2f} km at {a.speed} m/s", flush=True)

    t0 = time.monotonic()
    c.send({"type": "prepare_anchor", "requestId": 1, "lat": lat0, "lon": lon0})
    ar = await c.wait("anchor_ready", 600)
    aid, gz = ar["anchorId"], ar.get("groundZ") or 0.0
    c.send({"type": "commit_anchor", "anchorId": aid})
    print(f"anchor {aid} ready in {time.monotonic() - t0:.1f} s", flush=True)
    if ar.get("provisional"):       # the graded first block (the game's loading screen waits for it too)
        await c.wait("terrain_refresh", 600)
        print(f"graded first block after {time.monotonic() - t0:.1f} s", flush=True)
    try:                            # the far field around the start loaded (a player looks around first)
        await c.wait("far_done", a.load_wait)
        print(f"far field done after {time.monotonic() - t0:.1f} s", flush=True)
    except asyncio.TimeoutError:
        print(f"far field not done after {a.load_wait:.0f} s: driving anyway", flush=True)
    block = (0.0, 0.0)
    pending = None

    async def terrain(cx, cy, req):
        nonlocal block, pending
        c.send({"type": "prepare_terrain", "requestId": req, "anchorId": aid, "cx": cx, "cy": cy})
        try:
            await c.wait("terrain_ready", 300)
        except Exception as e:
            print("terrain", cx, cy, e, flush=True)
        c.send({"type": "commit_terrain", "anchorId": aid, "cx": cx, "cy": cy})
        block, pending = (cx, cy), None

    # stand still while the start loads (the player waits on the loading screen too)
    dt = 1.0 / POSE_HZ
    for _ in range(int(a.settle * POSE_HZ)):
        c.send({"type": "pose", "anchorId": aid, "x": 0.0, "y": 0.0, "z": gz + 1.5, "vx": 0.0, "vy": 0.0,
                "lat": lat0, "lon": lon0, "follow": True, "slotsGone": c.gone})
        c.gone = []
        await writer.drain()
        await asyncio.sleep(dt)

    entered = {}                    # L-2 cell -> time the car entered it
    samples = []
    s, req = 0.0, 1
    tstart = time.monotonic()
    tasks = []
    while s < km:
        now = time.monotonic()
        s = (now - tstart) * a.speed
        x, y, (hx, hy) = route.at(s)
        k = cell(-2, x, y)
        entered.setdefault(k, now)
        la = lat0 + y / 110574.0
        lo = lon0 + x / (111319.49 * math.cos(math.radians(lat0)))
        c.send({"type": "pose", "anchorId": aid, "x": x, "y": y, "z": gz + 1.5, "vx": hx * a.speed,
                "vy": hy * a.speed, "lat": la, "lon": lo, "follow": True, "slotsGone": c.gone})
        c.gone = []
        if pending is None and max(abs(x - block[0]), abs(y - block[1])) > BLOCK_MOVE:
            req += 1
            pending = (round(x / BLOCK_STEP) * BLOCK_STEP, round(y / BLOCK_STEP) * BLOCK_STEP)
            tasks.append(asyncio.create_task(terrain(*pending, req)))
        if int(s / (a.speed * 0.5)) != int((s - a.speed * dt) / (a.speed * 0.5)):    # every 0.5 s
            row = {"t": round(now - tstart, 2), "s": round(s)}
            for d in AHEAD:
                px, py, _ = route.at(s + d)
                row[f"lvl{int(d)}"] = c.finest(px, py)
            samples.append(row)
        await writer.drain()
        await asyncio.sleep(dt)
    # a few seconds more so the last cells can still come
    await asyncio.sleep(3.0)

    late = []
    for k, te in entered.items():
        ts = c.first_l2.get(k)
        late.append(None if ts is None else ts - te)
    shown = sorted(v for v in late if v is not None)

    def pct(v, p):
        return round(v[min(len(v) - 1, int(p * len(v)))], 1) if v else None
    summ = {"km": round(km / 1000, 2), "speed_ms": a.speed, "cells": len(late),
            "l2_never": sum(1 for v in late if v is None),
            "l2_late_median_s": pct(shown, 0.5), "l2_late_p90_s": pct(shown, 0.9),
            "l2_on_time": sum(1 for v in shown if v <= 0), "swaps": c.swaps}
    for d in AHEAD:
        lv = [r[f"lvl{int(d)}"] for r in samples]
        summ[f"under_l2_pct_{int(d)}m"] = round(100 * sum(1 for v in lv if v == -2) / max(1, len(lv)))
        summ[f"under_l1_pct_{int(d)}m"] = round(100 * sum(1 for v in lv if v is not None and v <= -1) / max(1, len(lv)))
    print(json.dumps(summ, indent=1), flush=True)
    if a.out:
        os.makedirs(a.out, exist_ok=True)
        with open(os.path.join(a.out, "summary.json"), "w") as f:
            json.dump(summ, f, indent=1)
        with open(os.path.join(a.out, "samples.jsonl"), "w") as f:
            for row in samples:
                f.write(json.dumps(row) + "\n")
    for t in tasks:
        t.cancel()
    pump.cancel()
    writer.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=47613)
    ap.add_argument("--from", dest="start", required=True, help="lat,lon")
    ap.add_argument("--to", required=True, help="lat,lon")
    ap.add_argument("--speed", type=float, default=25.0, help="m/s")
    ap.add_argument("--km", type=float, default=4.0, help="at most this far along the route")
    ap.add_argument("--settle", type=float, default=5.0, help="s standing at the start first")
    ap.add_argument("--load-wait", type=float, default=180.0, help="s at most waiting for far_done at the start")
    ap.add_argument("--route-file", help="route_result JSON (default: one per from/to in TEMP, asked once)")
    ap.add_argument("--out")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    main()
