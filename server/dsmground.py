"""The ground under a surface model: buildings and tree stands taken out of a DSM (2026-10-15).

Outside the national lidar models Mapterhorn's elevation is Copernicus GLO-30, a surface model: a city block
stands 5-25 m above its streets (Mexico City's Zocalo, a flat square, read 9-20 m below the blocks around it;
roads graded into the ground ran in trenches and the square was a pit), and Botafogo's beach stood several
metres up beside its towers. A morphological opening (the lowest surface a window of WINDOW_M can reach from
below) keeps the terrain's shapes wider than the window and cuts what is narrower: a block between streets,
a building, a small stand of trees. What it cuts beyond a threshold (higher on slopes, where a narrow ridge or
knoll is real) is taken off, softly at its edge; larger forests and whole hills stay as they are.

    ground(a, px_m) -> float32 array (a: elevation [rows, cols] in m, px_m: its pixel size in m)
"""
import numpy as np

WINDOW_M = 400.0        # features narrower than this are surface objects (blocks, and contiguous blocks between
                        # streets too narrow for 30 m pixels: Mexico City's historic centre needed ~400 m)
THRESHOLD_M = 2.0       # a cut this deep at least is an object (GLO-30's noise is ~1-2 m)
SLOPE_K = 0.35          # ... plus this times the ground's rise across half a window (steep ground: real knolls)
MAX_CUT_M = 45.0        # never lower the ground more than this (a tower's footprint in a 30 m pixel)
RELIEF_M = 600.0        # the window the local relief is measured over ...
RELIEF_FLAT_M = 40.0    # ... cut fully where the opened ground's relief is under this, not at all over
RELIEF_HILLS_M = 90.0   # this: the opening also shaves real peaks and ridges, so mountains are left alone
RELIEF_OPEN_M = 150.0   # the relief is that of the ground opened this much (towers out, hills kept)
TERRAIN_CUT_M = (35.0, 55.0)    # a cut this deep within a window is a hill, not a building (GLO-30's blocks and
                                # trees stand 5-30 m): nothing is cut around it (fully over the second value)


def _run(a, w, axis, op):
    """Running min / max (op: np.minimum / np.maximum) over a centred window of w (odd) samples along axis,
    van Herk / Gil-Werman: O(n) whatever w; edges padded with the edge values."""
    if w <= 1:
        return a
    h = w // 2
    a = np.moveaxis(a, axis, -1)
    n = a.shape[-1]
    pad = (-(n + 2 * h)) % w
    p = np.pad(a, [(0, 0)] * (a.ndim - 1) + [(h, h + pad)], mode="edge")
    m = p.shape[-1]
    blocks = p.reshape(p.shape[:-1] + (m // w, w))
    g = op.accumulate(blocks, axis=-1).reshape(p.shape)                              # prefix within blocks
    hh = op.accumulate(blocks[..., ::-1], axis=-1)[..., ::-1].reshape(p.shape)      # suffix within blocks
    # window [i, i + w - 1] (padded index) = op(suffix at i, prefix at i + w - 1)
    out = op(hh[..., :n], g[..., w - 1:w - 1 + n])
    return np.moveaxis(out, -1, axis)


def erode(a, w):
    return _run(_run(a, w, 0, np.minimum), w, 1, np.minimum)


def dilate(a, w):
    return _run(_run(a, w, 0, np.maximum), w, 1, np.maximum)


def opening(a, w):
    return dilate(erode(a, w), w)


def _box_mean(a, w):
    """Mean over a centred w x w window (edges replicated)."""
    if w <= 1:
        return a
    h = w // 2
    p = np.pad(a, h, mode="edge").astype(np.float64)
    c = np.cumsum(np.cumsum(p, 0), 1)
    c = np.pad(c, ((1, 0), (1, 0)))
    s = c[w:, w:] - c[:-w, w:] - c[w:, :-w] + c[:-w, :-w]
    return (s / (w * w)).astype(np.float32)


def ground(a, px_m, window_m=WINDOW_M, threshold_m=THRESHOLD_M, slope_k=SLOPE_K, max_cut_m=MAX_CUT_M,
           relief_m=RELIEF_M):
    """a with the surface objects narrower than window_m taken down to the ground around them (where the ground
    is flat enough to tell them from hills)."""
    a = np.asarray(a, np.float32)
    w = max(3, int(round(window_m / max(px_m, 1e-3))) | 1)
    o = opening(a, w)
    cut = a - o                                     # >= 0
    # the opened ground's rise across half a window: a slope's knolls and ridges are kept
    gy, gx = np.gradient(_box_mean(o, max(3, w // 2 | 1)), px_m)
    rise = np.hypot(gx, gy) * (window_m / 2)
    thr = threshold_m + slope_k * rise
    # soft edge: full cut well above the threshold, none below it
    k = np.clip((cut - thr) / max(threshold_m, 0.5), 0.0, 1.0)
    wr = max(3, int(round(relief_m / max(px_m, 1e-3))) | 1)
    # relief of the ground with only single buildings and towers taken out (the full opening also flattens a hill
    # narrower than its window: Sugarloaf Mountain's 300 m would have read as relief 80 m)
    o_small = opening(a, max(3, int(round(RELIEF_OPEN_M / max(px_m, 1e-3))) | 1))
    relief = dilate(o_small, wr) - erode(o_small, wr)
    k *= np.clip((RELIEF_HILLS_M - relief) / (RELIEF_HILLS_M - RELIEF_FLAT_M), 0.0, 1.0)
    deep = dilate(cut, w)
    k *= np.clip((TERRAIN_CUT_M[1] - deep) / (TERRAIN_CUT_M[1] - TERRAIN_CUT_M[0]), 0.0, 1.0)
    return (a - np.minimum(cut * k, max_cut_m)).astype(np.float32)
