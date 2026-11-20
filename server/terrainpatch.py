"""Terrain collision patches for collision islands off the terrain block (BeamMP support, the user, 2026-10-22: "As cars
spread apart they can have their own island of collision").

The engine collides with one TerrainBlock only (the first; the block follows the active car), so a vehicle far from
it has no ground. The mod (terrainpatch.lua) asks for a patch under each other island (collisionfoci.lua): a
collision-only shape (cdae collision_only: never drawn) of the same ground the block is made of (the graded DEM,
lifted under roads like the block), on a STEP_M grid, placed at the box's centre.
"""
import numpy as np

STEP_M = 4.0                # grid spacing: 640 m square = 25.6k quads, ~51k triangles (~2 ms of a collision rebuild)
MAX_SIDE_M = 1280.0         # a patch's side at most
SNAP_M = 64.0               # boxes are snapped out to this grid (keys stay the same while a car moves a little)


def snap_box(x0, y0, x1, y1):
    """The box snapped outward to SNAP_M and clipped to MAX_SIDE_M round its centre."""
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    hw, hh = min((x1 - x0) / 2, MAX_SIDE_M / 2), min((y1 - y0) / 2, MAX_SIDE_M / 2)
    return (float(np.floor((cx - hw) / SNAP_M) * SNAP_M), float(np.floor((cy - hh) / SNAP_M) * SNAP_M),
            float(np.ceil((cx + hw) / SNAP_M) * SNAP_M), float(np.ceil((cy + hh) / SNAP_M) * SNAP_M))


def grid(box, step=STEP_M):
    """(xs, ys) of the patch's grid over box (x0, y0, x1, y1), both ends included."""
    x0, y0, x1, y1 = box
    nx, ny = max(1, int(round((x1 - x0) / step))), max(1, int(round((y1 - y0) / step)))
    return np.linspace(x0, x1, nx + 1), np.linspace(y0, y1, ny + 1)


def mesh(xs, ys, z, origin):
    """(positions (n, 3) float32 relative to origin (x, y, 0), triangles (m, 3) int32) of the height grid z
    (len(ys), len(xs)), two triangles a quad, counter-clockwise seen from above (the Rust earthfast.grid_mesh)."""
    import fast
    fast.require(46, "grid_mesh")
    pos, tris = fast._ef.grid_mesh(np.ascontiguousarray(xs, np.float64), np.ascontiguousarray(ys, np.float64),
                                   np.ascontiguousarray(z, np.float64), float(origin[0]), float(origin[1]))
    return np.asarray(pos), np.asarray(tris)


def build(frame, elev, box, path, step=STEP_M):
    """Write the patch for box (anchor-frame metres) to path (.cdae, collision only); elev(lat, lon) -> the ground
    height in the anchor frame (as the block's). Returns (path, (cx, cy), triangles)."""
    import cdae
    xs, ys = grid(box, step)
    gx, gy = np.meshgrid(xs, ys)
    la, lo, _ = frame.enu_to_geodetic(gx.ravel(), gy.ravel(), np.zeros(gx.size))
    z = np.asarray(elev(la, lo), np.float64).reshape(gx.shape)
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    pos, tris = mesh(xs, ys, z, (cx, cy))
    cdae.write(path, pos, tris, collision_only=True)
    return path, (cx, cy), len(tris)
