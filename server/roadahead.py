"""The car's road ahead (photoreal: what to download and build first).

From the map's road lines (mapdata's roads layer, the cells already on disk: never a download from here) the line the
car drives on is the nearest one running along its heading; from there the path follows that line and, at its end,
the line that continues it most straight (Overture splits its roads at every junction). Where no road is known (off
road, the cells not yet there, a gap) the path goes on straight along the last direction.

  ra = RoadAhead(frame, cell_fn)            # cell_fn(key) -> the roads Features of a map cell, or None (not on disk)
  pts = ra.path(x, y, ux, uy, 800.0)        # [(x, y, s)] every STEP_M m along the way, s the distance driven
"""
import math

import numpy as np

import mapcells

# the classes a car drives on (roads.STYLE without tracks and raceways)
DRIVE = {"motorway", "trunk", "primary", "secondary", "tertiary", "residential", "living_street", "unclassified",
         "service", "unknown"}
STEP_M = 16.0               # the path's point spacing
SNAP_M = 25.0               # the car's road: a line at most this far from the car
SNAP_COS = 0.7              # ... running within ~45 degrees of its heading
JOIN_M = 3.0                # line ends this close continue each other
TURN_COS = math.cos(math.radians(100.0))    # a continuation turning more than this is a U-turn, not taken
LOAD_PAD_M = 300.0          # cells loaded this far beyond the path's reach


class RoadAhead:
    def __init__(self, frame, cell_fn):
        self.frame = frame
        self.cell_fn = cell_fn
        self.loaded = set()         # cell keys asked (with lines or none)
        self.lines = []             # (k, 2) float64 arrays in frame
        self.on_road = False        # the last path started on a road line
        self._index()

    def _index(self):
        """Segment arrays of every line (A -> B, the line and the segment's index) and the line ends."""
        segs = [(np.asarray(L[:-1]), np.asarray(L[1:]), np.full(len(L) - 1, n), np.arange(len(L) - 1))
                for n, L in enumerate(self.lines)]
        if segs:
            self.A = np.concatenate([s[0] for s in segs])
            self.B = np.concatenate([s[1] for s in segs])
            self.line = np.concatenate([s[2] for s in segs])
            self.idx = np.concatenate([s[3] for s in segs])
            self.ends = np.array([[L[0], L[-1]] for L in self.lines])          # (n, 2 ends, 2)
        else:
            self.A = self.B = np.zeros((0, 2))
            self.line = self.idx = np.zeros(0, int)
            self.ends = np.zeros((0, 2, 2))

    def load(self, x, y, r):
        """The road lines of the cells within r of (x, y) that are on disk and not read yet. -> lines added."""
        la, lo, _ = self.frame.enu_to_geodetic(np.array([x - r, x + r]), np.array([y - r, y + r]), 0.0)
        keys = mapcells.cells_for_bbox(float(np.min(la)), float(np.min(lo)), float(np.max(la)), float(np.max(lo)))
        n0 = len(self.lines)
        for key in keys:
            if key in self.loaded:
                continue
            feats = self.cell_fn(key)
            if feats is None:           # not on disk yet: asked again on a later call
                continue
            self.loaded.add(key)
            if len(feats) == 0:
                continue
            xy = feats.xy_in(self.frame)
            cls = feats.attrs.get("class")
            sub = feats.attrs.get("subtype")
            for i in range(len(feats)):
                if cls is not None and str(cls[i]) not in DRIVE:
                    continue
                if sub is not None and str(sub[i]) != "road":
                    continue
                for part in feats.parts(i, xy):
                    for ring in part:
                        if len(ring) >= 2:
                            self.lines.append(np.array(ring, np.float64))
        if len(self.lines) != n0:
            self._index()
        return len(self.lines) - n0

    def _snap(self, x, y, ux, uy):
        """(line, segment index, point on it, direction +1 / -1) of the car's road, or None."""
        if not len(self.A):
            return None
        d = self.B - self.A
        ln = np.maximum(np.hypot(d[:, 0], d[:, 1]), 1e-9)
        t = np.clip(((x - self.A[:, 0]) * d[:, 0] + (y - self.A[:, 1]) * d[:, 1]) / ln ** 2, 0.0, 1.0)
        px, py = self.A[:, 0] + d[:, 0] * t, self.A[:, 1] + d[:, 1] * t
        dist = np.hypot(px - x, py - y)
        cos = (d[:, 0] * ux + d[:, 1] * uy) / ln
        ok = (dist <= SNAP_M) & (np.abs(cos) >= SNAP_COS)
        if not ok.any():
            return None
        score = np.where(ok, dist + 20.0 * (1.0 - np.abs(cos)), np.inf)
        k = int(np.argmin(score))
        return int(self.line[k]), int(self.idx[k]), (float(px[k]), float(py[k])), 1 if cos[k] > 0 else -1

    def _next_line(self, end, v, used):
        """(line, direction) continuing the path at point end coming in along v (unit), or None."""
        if not len(self.ends):
            return None
        best, best_cos = None, TURN_COS
        for side in (0, 1):
            dd = np.hypot(self.ends[:, side, 0] - end[0], self.ends[:, side, 1] - end[1])
            for n in np.flatnonzero(dd <= JOIN_M):
                n = int(n)
                if n in used:
                    continue
                L = self.lines[n]
                a, b = (L[0], L[1]) if side == 0 else (L[-1], L[-2])
                w = b - a
                lw = math.hypot(w[0], w[1])
                if lw < 1e-6:
                    continue
                c = (w[0] * v[0] + w[1] * v[1]) / lw
                if c > best_cos:
                    best, best_cos = (n, 1 if side == 0 else -1), c
        return best

    def path(self, x, y, ux, uy, length):
        """[(x, y, s)] along the car's road (or straight on) every STEP_M m for length m, from the car (s = 0)."""
        self.load(x, y, length + LOAD_PAD_M)
        verts = [(x, y)]
        snap = self._snap(x, y, ux, uy)
        self.on_road = snap is not None         # (the server's log)
        if snap is not None:
            n, k, p, sgn = snap
            used = {n}
            verts.append(p)
            got = 0.0
            while got < length:
                L = self.lines[n]
                seq = L[k + 1:] if sgn > 0 else L[k::-1]
                for q in seq:
                    got += math.hypot(q[0] - verts[-1][0], q[1] - verts[-1][1])
                    verts.append((float(q[0]), float(q[1])))
                    if got >= length:
                        break
                if got >= length:
                    break
                a, b = np.asarray(verts[-2]), np.asarray(verts[-1])
                v = b - a
                lv = math.hypot(v[0], v[1])
                if lv < 1e-6:
                    break
                nxt = self._next_line(b, v / lv, used)
                if nxt is None:
                    break
                n, sgn = nxt
                used.add(n)
                # forwards: from the vertex after the junction; backwards: from the last vertex (the junction)
                k = 0 if sgn > 0 else len(self.lines[n]) - 1
        # resample every STEP_M, then straight on along the last direction up to length
        out = [(x, y, 0.0)]
        s = 0.0
        nxt_s = STEP_M
        for (ax, ay), (bx, by) in zip(verts, verts[1:]):
            seg = math.hypot(bx - ax, by - ay)
            while seg > 0 and s + seg >= nxt_s:
                f = (nxt_s - s) / seg
                out.append((ax + (bx - ax) * f, ay + (by - ay) * f, nxt_s))
                nxt_s += STEP_M
            s += seg
        if len(verts) >= 2 and s > 0:
            (ax, ay), (bx, by) = verts[-2], verts[-1]
            lv = math.hypot(bx - ax, by - ay)
            if lv > 1e-6:
                ux, uy = (bx - ax) / lv, (by - ay) / lv
        lx, ly, ls = verts[-1][0], verts[-1][1], s
        while nxt_s <= length:
            out.append((lx + ux * (nxt_s - ls), ly + uy * (nxt_s - ls), nxt_s))
            nxt_s += STEP_M
        return out
