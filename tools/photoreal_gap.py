#!/usr/bin/env python3
"""How far the photorealistic tiles' roads lie from the ground the tyres touch, without the game (personal fork,
2026-10-23). The physics stays BeamNG.Earth's graded TerrainBlock; the photogrammetry (server/photoreal.py) is drawn
over it. Where the two disagree, a car floats over the drawn road or sinks into it. This measures by how much.

  python3 tools/photoreal_gap.py 48.1374,11.5755 [--radius 300] [--target-err 1] [--out build/gap]

Needs CESIUM_ION_TOKEN (or GOOGLE_MAPS_API_KEY). Reads the download cache (--cache-dir, default
~/.cache/beamng-earth) and fetches what is missing like the server does.

For a box of 2 x --radius around the point: the road plans the server grades (grading.Grader), points every --step m
along their centre lines, the drive ground at each (the DEM at the physics block's zoom, graded, + ROAD_LIFT, as
blockworker.build makes it) and the photogrammetry's lowest surface point there (Mesh.height_grid "min" on a
--cell m grid), shifted by the one geoid offset photoreal.ground_offset measures over the box.

gap = photogrammetry - drive ground: > 0 the drawn road is above the tyres (the wheels sink into it), < 0 below
(the car floats). A gap over --occluded m is something on top of the road in the mesh (a tree, a bridge, a bus): those
points are counted apart. Per road class: the median, the 5th / 95th percentile, the share within 0.1 / 0.3 m, and the
roughness (std of the gap's change over 10 m: a smooth offset is fixable by a shift, a rough one is the mesh's
bumps). Writes gap.csv (every point) and gap.png (the box from above, 1 px per --cell m: the mesh's heights in grey,
the points coloured blue (below) to red (above), full colour at +-1 m) to --out.
"""
import argparse
import csv
import math
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "server"))
import numpy as np  # noqa: E402

import dem as demmod  # noqa: E402
import geodesy  # noqa: E402
import grading  # noqa: E402
import mapcells  # noqa: E402
import mapdata as mapdatamod  # noqa: E402
import netfetch  # noqa: E402
import photoreal  # noqa: E402

COLOUR_M = 1.0          # gap at full red / blue in gap.png
ROUGH_M = 10.0          # roughness: the gap's change over this distance along the road


def latlon(s):
    a, b = s.split(",")
    return float(a), float(b)


def road_points(plans, box, step):
    """(x (k,), y (k,), plan index (k,), arc length (k,)) every step m along the plans' centre lines, inside box."""
    X, Y, P, S = [], [], [], []
    for k, p in enumerate(plans):
        for run in grading.clip_polyline(p.xy, box):
            seg = np.hypot(*np.diff(run, axis=0).T)
            s = np.r_[0.0, np.cumsum(seg)]
            if s[-1] <= 0:
                continue
            t = np.arange(0.0, s[-1] + 1e-9, step)
            X.append(np.interp(t, s, run[:, 0])), Y.append(np.interp(t, s, run[:, 1]))
            P.append(np.full(len(t), k)), S.append(t)
    if not X:
        e = np.zeros(0)
        return e, e, e.astype(np.int64), e
    return np.concatenate(X), np.concatenate(Y), np.concatenate(P), np.concatenate(S)


def grid_lookup(grid, box, cell, x, y):
    """grid[j, i] (height_grid's layout) at the points, NaN outside it."""
    x0, y0 = box[0], box[1]
    i = np.floor((x - x0) / cell).astype(np.int64)
    j = np.floor((y - y0) / cell).astype(np.int64)
    ok = (i >= 0) & (i < grid.shape[1]) & (j >= 0) & (j < grid.shape[0])
    out = np.full(len(x), np.nan)
    out[ok] = grid[j[ok], i[ok]]
    return out


def roughness(gap, plan, s, dist=ROUGH_M):
    """Std of gap(s + dist) - gap(s) along each road (points of one plan in arc order), NaN with too few."""
    d = []
    for k in np.unique(plan):
        m = (plan == k) & np.isfinite(gap)
        if m.sum() < 3:
            continue
        ss, gg = s[m], gap[m]
        o = np.argsort(ss)
        ss, gg = ss[o], gg[o]
        far = ss + dist <= ss[-1]
        if far.any():
            d.append(np.interp(ss[far] + dist, ss, gg) - gg[far])
    return float(np.std(np.concatenate(d))) if d else float("nan")


def summarise(gap, plan, s, classes, occluded):
    """{class: stats} over the points (and "all")."""
    out = {}
    for name in ["all"] + sorted(set(classes)):
        m = np.ones(len(gap), bool) if name == "all" else np.array([c == name for c in classes], bool)
        g = gap[m]
        seen = np.isfinite(g)
        occ = seen & (np.abs(g) > occluded)
        ok = seen & ~occ
        if not ok.any():
            out[name] = {"points": int(m.sum()), "no_mesh": int((~seen).sum()), "occluded": int(occ.sum())}
            continue
        gg = g[ok]
        gm = np.where(np.abs(gap) <= occluded, gap, np.nan)
        out[name] = {"points": int(m.sum()), "no_mesh": int((~seen).sum()), "occluded": int(occ.sum()),
                     "median": float(np.median(gg)), "p5": float(np.percentile(gg, 5)),
                     "p95": float(np.percentile(gg, 95)),
                     "within_0.1": float(np.mean(np.abs(gg) <= 0.1)), "within_0.3": float(np.mean(np.abs(gg) <= 0.3)),
                     "rough": roughness(gm[m], plan[m], s[m])}
    return out


def picture(path, low, box, cell, x, y, gap, occluded):
    from PIL import Image
    z = low[::-1]                           # north up
    fin = np.isfinite(z)
    lo, hi = (np.percentile(z[fin], [2, 98]) if fin.any() else (0.0, 1.0))
    g = np.where(fin, np.clip((z - lo) / max(hi - lo, 1e-6), 0, 1) * 160 + 40, 0).astype(np.uint8)
    rgb = np.repeat(g[..., None], 3, axis=2)
    i = np.floor((x - box[0]) / cell).astype(np.int64)
    j = rgb.shape[0] - 1 - np.floor((y - box[1]) / cell).astype(np.int64)
    ok = (i >= 0) & (i < rgb.shape[1]) & (j >= 0) & (j < rgb.shape[0])
    for ii, jj, gv in zip(i[ok], j[ok], gap[ok]):
        if not np.isfinite(gv):
            c = (255, 0, 255)               # no mesh
        elif abs(gv) > occluded:
            c = (255, 255, 0)               # something on the road
        else:
            t = float(np.clip(gv / COLOUR_M, -1, 1))
            c = (255, int(255 * (1 - t)), int(255 * (1 - t))) if t >= 0 else \
                (int(255 * (1 + t)), int(255 * (1 + t)), 255)
        rgb[max(jj - 1, 0):jj + 2, max(ii - 1, 0):ii + 2] = c
    Image.fromarray(rgb).save(path)


def road_classes(md, keys):
    """{feature id: class} of the roads in the cells."""
    out = {}
    for key in keys:
        f = md.cell(key, layers=("roads",), kind="demand").layers.get("roads")
        if f is None:
            continue
        for fid, c in zip(f.ids, f.attrs["class"]):
            out[str(fid)] = str(c)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("centre", type=latlon, help="lat,lon")
    ap.add_argument("--radius", type=float, default=300.0, help="half the box's side, m")
    ap.add_argument("--step", type=float, default=2.0, help="m between points along a road")
    ap.add_argument("--cell", type=float, default=0.5, help="the mesh's height grid, m")
    ap.add_argument("--target-err", type=float, default=1.0, help="the tiles' geometric error, m (finer: more data)")
    ap.add_argument("--occluded", type=float, default=2.0, help="|gap| over this: something on the road, m")
    ap.add_argument("--cache-dir", default=os.path.join(os.path.expanduser("~"), ".cache/beamng-earth"))
    ap.add_argument("--out", default=os.path.join(ROOT, "build", "photoreal_gap"))
    args = ap.parse_args()

    lat0, lon0 = args.centre
    frame = geodesy.EnuFrame(lat0, lon0)
    r = args.radius
    box = (-r, -r, r, r)
    net = netfetch.Fetcher()
    md = mapdatamod.MapData(args.cache_dir, fetcher=net)
    dem = demmod.Dem(os.path.join(args.cache_dir, "dem"), fetcher=net)
    zoom = demmod.zoom_for(lat0, 1.25, demmod.FINE_ZOOM)        # the physics block's (earthd: anchor.hm_zoom)
    grader = grading.Grader(frame, md, lambda la, lo: dem.sample(la, lo, grading.PROFILE_ZOOM))

    # the roads the server grades, and the drive ground along them
    la, lo, _ = frame.enu_to_geodetic(np.array([-r, r, -r, r]), np.array([-r, -r, r, r]), 0.0)
    keys = mapcells.cells_for_bbox(float(min(la)), float(min(lo)), float(max(la)), float(max(lo)))
    grader.plans(np.asarray(la), np.asarray(lo), roads_only=True)
    pairs = [fp for k in keys for fp in grader._cell_plans(k) if grading.TUNNEL_TAG not in fp[0]]
    plans = [p for _, p in pairs]
    cls_of = road_classes(md, keys)
    x, y, plan, s = road_points(plans, box, args.step)
    print(f"{len(plans)} road plans, {len(x)} points every {args.step:g} m")
    if not len(x):
        return
    classes = [cls_of.get(pairs[k][0], "?") for k in plan]
    pla, plo, _ = frame.enu_to_geodetic(x, y, 0.0)
    pla, plo = np.asarray(pla), np.asarray(plo)
    drive = grader.apply(pla, plo, dem.sample(pla, plo, zoom), grading.ROAD_LIFT)
    drive_u = np.asarray(frame.geodetic_to_enu(pla, plo, drive)[2])

    # the photogrammetry, placed on the (ungraded) ground once for the box
    pr = photoreal.Photoreal(args.cache_dir)
    mesh = pr.mesh(frame, box, args.target_err)
    print(f"photoreal: {pr.fetched} fetched, {pr.cached} from the cache")
    if mesh is None:
        print("no photogrammetry over the box")
        return
    print(f"mesh: {len(mesh.tris)} triangles, {len(mesh.textures)} textures")
    off_step = 2.0
    n = int(round(2 * r / off_step))
    cs = -r + (np.arange(n) + 0.5) * off_step
    gla, glo, _ = frame.enu_to_geodetic(*np.meshgrid(cs, cs), 0.0)
    gla, glo = np.asarray(gla), np.asarray(glo)
    ground_u = np.asarray(frame.geodetic_to_enu(gla, glo, dem.sample(gla, glo, zoom))[2])
    offset = photoreal.ground_offset(mesh, box, ground_u, off_step)
    print(f"geoid offset: {offset:+.2f} m (photogrammetry shifted by it)")
    low = mesh.height_grid(box, args.cell, "min") + offset
    gap = grid_lookup(low, box, args.cell, x, y) - drive_u

    stats = summarise(gap, plan, s, classes, args.occluded)
    print(f"\ngap = photogrammetry - drive ground (m); |gap| > {args.occluded:g} m counted as occluded")
    print(f"{'class':<16}{'points':>7}{'occl':>6}{'none':>6}{'median':>8}{'p5':>8}{'p95':>8}"
          f"{'<=0.1':>7}{'<=0.3':>7}{'rough':>7}")
    for name, st in sorted(stats.items(), key=lambda kv: (kv[0] != "all", -kv[1]["points"])):
        if "median" not in st:
            print(f"{name:<16}{st['points']:>7}{st['occluded']:>6}{st['no_mesh']:>6}")
            continue
        print(f"{name:<16}{st['points']:>7}{st['occluded']:>6}{st['no_mesh']:>6}{st['median']:>+8.2f}"
              f"{st['p5']:>+8.2f}{st['p95']:>+8.2f}{st['within_0.1']:>7.0%}{st['within_0.3']:>7.0%}"
              f"{st['rough']:>7.2f}")

    os.makedirs(args.out, exist_ok=True)
    with open(os.path.join(args.out, "gap.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["lat", "lon", "x", "y", "class", "drive_u", "gap"])
        for k in range(len(x)):
            w.writerow([f"{pla[k]:.7f}", f"{plo[k]:.7f}", f"{x[k]:.1f}", f"{y[k]:.1f}", classes[k],
                        f"{drive_u[k]:.3f}", "" if not math.isfinite(gap[k]) else f"{gap[k]:.3f}"])
    picture(os.path.join(args.out, "gap.png"), low, box, args.cell, x, y, gap, args.occluded)
    print(f"\nwrote {args.out}/gap.csv and gap.png")


if __name__ == "__main__":
    main()
