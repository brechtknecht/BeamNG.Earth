"""earthd.Server.download_ahead: the raw data along a driving player's straight path, fetched ahead."""
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import geodesy  # noqa: E402


class SyncPool:
    def submit(self, fn):
        fn()


class FakeDem:
    def __init__(self):
        self.asked = []

    def keys_around(self, lat, lon, radius, zoom):
        return [("k", round(lat, 4), round(lon, 4), zoom)]

    def prefetch(self, keys):
        self.asked += keys
        return len(keys), False


class FakeMapdata:
    def __init__(self):
        self.cells = []

    def has(self, key, layer):
        return False

    def cell(self, key, layers, kind):
        self.cells.append((key, layers, kind))


def server():
    s = types.SimpleNamespace(dem=FakeDem(), mapdata=FakeMapdata(), buildings=True, ahead_pool=SyncPool(),
                              ahead_busy=False, ahead_last=None, ahead_stats={"dem": 0, "cells": 0, "deferred": 0},
                              layout=types.SimpleNamespace(block=1024))
    return s


def anchor(aid=1):
    return types.SimpleNamespace(id=aid, frame=geodesy.EnuFrame(36.17, -115.145), hm_zoom=15)


class DownloadAheadTest(unittest.TestCase):
    def run_at(self, s, a, x, y, vx, vy):
        earthd.Server.download_ahead(s, a, x, y, vx, vy)

    def test_points_ahead_on_the_path(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 0.0, 40.0)          # 40 m/s north: 60 s would be 2400 m (< AHEAD_MAX_M)
        self.assertEqual(len(s.dem.asked), len(earthd.AHEAD_S))
        lats = [k[1] for k in s.dem.asked]
        self.assertEqual(lats, sorted(lats), "nearest point first, going north")
        self.assertAlmostEqual((lats[-1] - 36.17) * 111000, 40.0 * earthd.AHEAD_S[-1], delta=60)
        self.assertTrue(s.mapdata.cells and all(k == "prefetch" for _, _, k in s.mapdata.cells))
        self.assertFalse(s.ahead_busy)

    def test_far_points_capped(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 200.0, 0.0)         # 200 m/s: capped at AHEAD_MAX_M
        lon_m = (s.dem.asked[-1][2] + 115.145) * 111000 * 0.807
        self.assertAlmostEqual(lon_m, earthd.AHEAD_MAX_M, delta=80)

    def test_slow_or_unmoved_does_nothing(self):
        s, a = server(), anchor()
        self.run_at(s, a, 0.0, 0.0, 2.0, 0.0)           # walking pace
        self.assertEqual(s.dem.asked, [])
        self.run_at(s, a, 0.0, 0.0, 30.0, 0.0)
        n = len(s.dem.asked)
        self.run_at(s, a, 50.0, 0.0, 30.0, 0.0)         # less than AHEAD_MOVE_M later: no new round
        self.assertEqual(len(s.dem.asked), n)
        self.run_at(s, a, 50.0 + earthd.AHEAD_MOVE_M, 0.0, 30.0, 0.0)
        self.assertGreater(len(s.dem.asked), n)

    def test_no_buildings_roads_only(self):
        # (from PR #64) without buildings the road layer is still fetched ahead (the road tiles need it)
        s, a = server(), anchor()
        s.buildings = False
        self.run_at(s, a, 0.0, 0.0, 30.0, 0.0)
        self.assertTrue(s.dem.asked)
        self.assertTrue(s.mapdata.cells and all(layers == ("roads",) for _, layers, _ in s.mapdata.cells))



class FakeFuture:
    def result(self):
        return 0.0


class FakeMapAheadPool:
    workers = 1

    def __init__(self):
        import threading
        self.keys = []
        self.lock = threading.Lock()

    def submit(self, fn, key, layers, veg):
        with self.lock:
            self.keys.append(key)
        return FakeFuture()


def map_ahead_server():
    import threading
    s = server()
    s.map_ahead_pool = FakeMapAheadPool()
    s.map_ahead_last, s.map_ahead_want, s.map_ahead_done = None, [], set()
    s.map_ahead_cv, s.map_ahead_threads = threading.Condition(), None
    s.map_ahead_stats = {"cells": 0, "s": 0.0}
    s.veg = None
    s._map_ahead_loop = lambda: earthd.Server._map_ahead_loop(s)
    return s


class MapAheadTest(unittest.TestCase):
    def wait_for(self, pool, n):
        import time
        t0 = time.monotonic()
        while len(pool.keys) < n and time.monotonic() - t0 < 5.0:
            time.sleep(0.01)

    def test_download_ahead_leaves_the_cells_to_it(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.download_ahead(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertTrue(s.dem.asked)
        self.assertEqual(s.mapdata.cells, [])

    def test_cells_along_the_path_nearest_first_once(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 40.0)      # 40 m/s north: 4.8 km ahead
        n = len(s.map_ahead_done) + len(s.map_ahead_want)
        self.wait_for(s.map_ahead_pool, n)
        keys = s.map_ahead_pool.keys
        self.assertGreater(len(keys), 2)
        self.assertEqual(len(keys), len(set(keys)))
        ys = [k[2] for k in keys]       # (tile rows count southwards)
        self.assertGreaterEqual(ys[0], max(ys) - 1, "the cells around the car first")
        self.assertEqual(ys[-1], min(ys), "the farthest north last")
        earthd.Server.map_ahead(s, a, 50.0, 0.0, 0.0, 40.0)     # less than MAP_AHEAD_MOVE_M later: no new list
        self.assertEqual(s.map_ahead_last[1], 0.0)
        earthd.Server.map_ahead(s, a, 0.0, 300.0, 0.0, 40.0)    # further on: only new cells
        self.wait_for(s.map_ahead_pool, len(keys) + 1)
        self.assertEqual(len(s.map_ahead_pool.keys), len(set(s.map_ahead_pool.keys)))

    def test_standing_asks_nothing(self):
        s, a = map_ahead_server(), anchor()
        earthd.Server.map_ahead(s, a, 0.0, 0.0, 0.0, 0.5)
        self.assertEqual(s.map_ahead_pool.keys, [])
        self.assertIsNone(s.map_ahead_threads)


if __name__ == "__main__":
    unittest.main()
