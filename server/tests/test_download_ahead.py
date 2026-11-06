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
    s.map_ahead_stats, s.map_ahead_state = {"cells": 0, "s": 0.0}, {}
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



class LoadMapTest(unittest.TestCase):
    def test_zone_cells_and_lead(self):
        s, a = map_ahead_server(), anchor()
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertEqual(m["type"], "load_map")
        self.assertGreater(len(m["zone"]), 2)
        self.assertTrue(m["cells"])
        self.assertTrue(all(c[4] == earthd.LOAD_MAP_MISSING for c in m["cells"]))
        self.assertEqual(m["lead"], 0)
        for c in m["cells"]:            # (west < east, south < north in the anchor frame)
            self.assertLess(c[0], c[2])
            self.assertLess(c[1], c[3])
        # every cell ready: the lead reaches the end of the path
        import mapcells
        for k in mapcells.cells_for_bbox(36.0, -115.4, 36.4, -114.9):
            s.map_ahead_state[(k.z, k.x, k.y)] = (earthd.LOAD_MAP_READY, 2.0)
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 40.0)
        self.assertAlmostEqual(m["lead"], 40.0 * earthd.MAP_AHEAD_S, delta=50)

    def test_standing_is_a_circle(self):
        s, a = map_ahead_server(), anchor()
        m = earthd.Server.load_map(s, a, 0.0, 0.0, 0.0, 0.0)
        self.assertEqual(m["zone"], [[0, 0, round(earthd.WARM_AROUND)]])



class LoadMapBlocksTest(unittest.TestCase):
    def test_block_states(self):
        from collections import OrderedDict
        import farfield
        lay = types.SimpleNamespace(block=512)
        srv = types.SimpleNamespace(layout=lay, hm=OrderedDict({(1, 0.0, 0.0): {"seconds": 4.2},
                                                               (1, 384.0, 0.0): {"seconds": 6.0}}),
                                    hm_inflight={(1, 768.0, 0.0): None, (1, 1152.0, 0.0): None,
                                                 (1, 1536.0, 0.0): None},
                                    hm_stage={(1, 768.0, 0.0): "the block process",
                                              (1, 1152.0, 0.0): "its base photo"})
        c = types.SimpleNamespace(s=srv, active=1, block=farfield.Block(0.0, 0.0, 512),
                                  pending_block=farfield.Block(384.0, 0.0, 512), pending_at=None,
                                  predicted_block=farfield.Block(1920.0, 0.0, 512), predicted_chain=[])
        blocks, asked = earthd.Connection.load_map_blocks(c, 1)
        st = {(b[0] + b[2]) / 2: b[4] for b in blocks}
        self.assertEqual(st[0.0], earthd.BLOCK_IN_USE)
        self.assertEqual(st[384.0], earthd.BLOCK_READY)     # asked for, but built: no wait
        self.assertEqual(st[768.0], earthd.BLOCK_BUILDING)
        self.assertEqual(st[1152.0], earthd.BLOCK_PHOTO)
        self.assertEqual(st[1536.0], earthd.BLOCK_QUEUED)
        self.assertEqual(st[1920.0], earthd.BLOCK_WANTED)
        self.assertEqual(asked, 0.0)
        # asked for and not built: the game waits on it
        import time
        c.pending_block, c.pending_at = farfield.Block(768.0, 0.0, 512), time.monotonic() - 2.5
        blocks, asked = earthd.Connection.load_map_blocks(c, 1)
        st = {(b[0] + b[2]) / 2: b[4] for b in blocks}
        self.assertEqual(st[768.0], earthd.BLOCK_ASKED)
        self.assertGreaterEqual(asked, 2.5)


if __name__ == "__main__":
    unittest.main()
