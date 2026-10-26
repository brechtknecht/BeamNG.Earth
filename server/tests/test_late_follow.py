"""A late follow block (2026-10-26): when the graded block would come after the car reaches its block's stop line, the
server sends a provisional one first (Server.make_follow_provisional: no road grading) and the graded one as
terrain_refresh once the mod has the block in use (Connection.late_provisional, refine_follow), so the edge guard
never holds the car for the road plans."""
import asyncio
import json
import os
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import earthd  # noqa: E402
import farfield  # noqa: E402


class _Writer:
    def __init__(self):
        self.msgs = []

    def write(self, data):
        self.msgs.append(json.loads(data[4:].decode()))

    def is_closing(self):
        return False

    def get_extra_info(self, name):
        return ("127.0.0.1", 0)


class StopInTest(unittest.TestCase):
    def conn(self, focus, vel):
        c = SimpleNamespace(block=farfield.Block(0.0, 0.0, 512), focus=focus, vel=vel)     # collidable [-512, 510]
        c.edge_at = lambda *a: earthd.Connection.edge_at(c, *a)
        return c

    def test_slow_cars_too(self):
        # (edge_at alone is None below BASE_BUDGET_MIN_V: a 30 km/h car's block is late as well)
        left = earthd.Connection.stop_in(self.conn((0.0, 0.0), (8.0, 0.0)))
        self.assertAlmostEqual(left, (510 - earthd.BASE_EDGE_STOP_M) / 8.0 - earthd.BASE_EDGE_LEAD_S, delta=0.05)

    def test_standing_at_the_stop_line_is_now(self):
        self.assertEqual(earthd.Connection.stop_in(self.conn((480.0, 0.0), (0.0, 0.0))), 0.0)
        self.assertIsNone(earthd.Connection.stop_in(self.conn((0.0, 0.0), (0.0, 0.0))))
        self.assertIsNone(earthd.Connection.stop_in(SimpleNamespace(block=None, focus=(0.0, 0.0), vel=(0.0, 0.0),
                                                                    edge_at=lambda *a: None)))


class LateFollowTest(unittest.TestCase):
    def server(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512,
                          mapdata=types.SimpleNamespace())
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        self.addCleanup(s.prov_pool.shutdown)
        s.grading = True
        s.prov_est = 0.05
        a = s.new_anchor(40.0, -80.0)
        s.anchors[a.id] = a
        return s, a

    def drive(self, graded_s, x, vx, then=None, setup=None):
        """A follow request for the block east of (0, 0) with the car at x, vx; the graded build takes graded_s.
        then(c, msgs): after the first answer (the mod's moves). Returns the messages and the builds."""
        s, a = self.server()
        built = []
        size = s.layout.block * 2.0

        async def terrain(anchor, cx, cy, *args, **kw):
            built.append(("graded", cx, cy))
            await asyncio.sleep(graded_s)
            return {"ter": "graded.ter"}
        s.terrain = terrain

        def provisional(anchor, cx, cy):
            built.append(("provisional", cx, cy))
            time.sleep(0.05)
            return {"ter": "prov.ter"}
        s.make_follow_provisional = provisional
        s.drop_build = lambda key: built.append(("dropped", key[1], key[2])) or True

        async def go():
            w = _Writer()
            c = earthd.Connection(s, None, w)
            c.terrain_msg = lambda k, info: {"cx": k[1], "cy": k[2], "size": 512, "ter": info["ter"]}
            c.active = a.id
            c._set_block(a.id, 0.0, 0.0)
            c.focus, c.vel = (x, 0.0), (vx, 0.0)
            if setup is not None:
                setup(c)
            c.dispatch({"type": "prepare_terrain", "requestId": 7, "anchorId": a.id, "cx": size / 2, "cy": 0.0})
            for _ in range(100):
                await asyncio.sleep(0.02)
                if w.msgs:
                    break
            if then is not None:
                then(c, w.msgs, a)
            await asyncio.sleep(graded_s + 0.5)
            return w.msgs
        return asyncio.run(go()), built, size

    def test_late_block_goes_provisional_then_graded(self):
        def commit(c, msgs, a):     # the mod commits the provisional block
            self.assertEqual(msgs[0]["type"], "terrain_ready")
            c.dispatch({"type": "commit_terrain", "anchorId": a.id, "cx": msgs[0]["cx"], "cy": msgs[0]["cy"]})
        msgs, built, size = self.drive(1.0, 440.0, 40.0, commit)    # 0.9 s to the stop line
        self.assertEqual([m["type"] for m in msgs], ["terrain_ready", "terrain_refresh"])
        self.assertTrue(msgs[0]["provisional"])
        self.assertEqual((msgs[0]["requestId"], msgs[0]["ter"]), (7, "prov.ter"))
        self.assertEqual((msgs[1]["cx"], msgs[1]["ter"]), (size / 2, "graded.ter"))

    def test_graded_in_time_is_sent_as_ever(self):
        msgs, built, _ = self.drive(0.2, 0.0, 25.0)                 # 19 s to the stop line
        self.assertEqual([m["type"] for m in msgs], ["terrain_ready"])
        self.assertNotIn("provisional", msgs[0])
        self.assertEqual([b[0] for b in built], ["graded"])

    def test_after_a_stand_in_the_next_one_starts_on_time(self):
        # 2 s to the stop line, a stand-in takes 1 s, the graded one 0.8 s but build_s says 0.1 s
        def setup(c, streak):
            c.s.prov_est, c.s.quality.build_s, c.prov_streak = 1.0, 0.1, streak
        msgs, built, _ = self.drive(0.8, 396.0, 40.0, setup=lambda c: setup(c, 0))
        self.assertNotIn("provisional", msgs[0], "build_s trusted: the graded one waited for")
        msgs, built, _ = self.drive(0.8, 396.0, 40.0, setup=lambda c: setup(c, 1))
        self.assertTrue(msgs[0].get("provisional"), "the last block stood in for: build_s not trusted")

    def test_standing_at_the_edge(self):
        # the edge guard holds the car (no speed): the provisional one at once
        msgs, built, _ = self.drive(0.6, 480.0, 0.0)
        self.assertTrue(msgs[0].get("provisional"))

    def test_no_refresh_once_the_mod_moved_on(self):
        def away(c, msgs, a):       # the mod asks for another block before it commits this one
            c.pending_block = c.s.layout.block_at(0.0, c.s.layout.block * 2.0)
        msgs, built, size = self.drive(0.6, 440.0, 40.0, away)
        self.assertEqual([m["type"] for m in msgs], ["terrain_ready"])
        self.assertIn(("dropped", size / 2, 0.0), built, "its graded build not wanted: dropped")


class DropBuildTest(unittest.TestCase):
    """Server.drop_build: a graded build nobody waits for is not started (a queue of them held the next blocks up)."""

    def server(self, pool=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        s = earthd.Server(tmp.name, os.path.join(tmp.name, "cache"), imagery=None, cells=64, block=512,
                          block_pools=[(pool,), (pool,)] if pool is not None else ())
        self.addCleanup(s.hm_pool.shutdown)
        self.addCleanup(s.tile_pool.shutdown)
        return s

    def test_queued_job_cancelled(self):
        import concurrent.futures as cf
        s = self.server()
        job = s.hm_proc[(1, 0.0, 0.0)] = cf.Future()
        self.assertTrue(s.drop_build((1, 0.0, 0.0)))
        self.assertTrue(job.cancelled())
        running = s.hm_proc[(1, 1.0, 0.0)] = cf.Future()
        running.set_running_or_notify_cancel()
        self.assertFalse(s.drop_build((1, 1.0, 0.0)), "a build the process has taken runs on")
        self.assertFalse(s.drop_build((1, 2.0, 0.0)), "nothing to drop")

    def test_build_raises_dropped(self):
        import concurrent.futures as cf

        class Pool:
            def submit(self, *a, **kw):
                f = cf.Future()
                f.cancel()          # (drop_build while it was queued)
                return f
        s = self.server(Pool())
        a = s.new_anchor(40.0, -80.0)
        with self.assertRaises(earthd.BuildDropped):
            s.make_heightmap(a, 0.0, 0.0, None)
        self.assertEqual(s.hm_proc, {})
        s.hm_dropped.add((a.id, 0.0, 0.0))      # dropped before it got to its block process
        with self.assertRaises(earthd.BuildDropped):
            s.make_heightmap(a, 0.0, 0.0, None)
        self.assertEqual(s.hm_dropped, set())


class StandInTest(unittest.TestCase):
    """blockworker.stand_in: the stand-in built in a process of its own, its heights the donors' where they overlap."""

    def test_heights_from_dem_and_donor(self):
        import numpy as np
        import blockworker
        import heightmap
        size = 512

        class Dem:
            def sample_local(self, la, lo, zoom):
                return np.full(np.shape(la), 12.0), 0
        old = blockworker._s
        blockworker._s = SimpleNamespace(dem=Dem(), airports=None, surface=None, anchor=lambda *a: (None, None))
        self.addCleanup(setattr, blockworker, "_s", old)
        with tempfile.TemporaryDirectory() as d:
            xs, ys = heightmap.sample_coords(0.0, 0.0, size)
            u = 30.0 + 0.01 * xs[None, :] + 0.02 * ys[:, None]
            q, zb, mh, qgrid = heightmap.write_png(os.path.join(d, "hm.png"), u)
            heightmap.write_ter(os.path.join(d, "old.ter"), qgrid, None, ["dirt"])
            info = blockworker.stand_in(1, 52.2, 13.5, 14, 0.01, 0.0, 384.0, size, d, "hm_1_0_384_p.png", "new.ter",
                                        [(os.path.join(d, "old.ter"), zb, mh, 0.0, 0.0)])
            self.assertEqual(info["hm"], "hm_1_0_384_p.png")
            self.assertEqual(info["stages"]["donorChunks"], 40)
            self.assertTrue(os.path.exists(os.path.join(d, info["ter"])))
            with open(os.path.join(d, info["ter"]), "rb") as f:
                f.read(5)
                got = heightmap.heights(np.frombuffer(f.read(2 * size * size), "<u2").reshape(size, size),
                                        info["zBase"], info["maxHeight"])
            ch = heightmap.CHUNK
            stored = heightmap.heights(qgrid, zb, mh)
            np.testing.assert_allclose(got[0:5 * ch], stored[3 * ch:8 * ch], atol=0.1)    # the old block's rows 3-7
            np.testing.assert_allclose(got[5 * ch + 1:], 12.0, atol=0.1)                  # north of it: the DEM


if __name__ == "__main__":
    unittest.main()


class DonorChunksTest(unittest.TestCase):
    """Server.donor_chunks: a stand-in's heights from the graded blocks around (their .ter), chunk for chunk."""

    def test_overlap_from_the_graded_block(self):
        import numpy as np
        import heightmap
        from collections import OrderedDict
        size = 512
        with tempfile.TemporaryDirectory() as d:
            xs, ys = heightmap.sample_coords(0.0, 0.0, size)
            u = 30.0 + 0.01 * xs[None, :] + 0.02 * ys[:, None]       # [gy, gx], row 0 south
            q, zb, mh, qgrid = heightmap.write_png(os.path.join(d, "hm.png"), u)
            import surface as sf
            idx = np.full((size, size), sf.DIRT, np.uint8)
            idx[:, 256:] = sf.ROCK
            idx[300:304, 300:304] = heightmap.TER_HOLE           # a tunnel mouth
            sf.write_ter(d, "old.ter", qgrid, np.where(idx == heightmap.TER_HOLE, sf.DIRT, idx))
            raw = bytearray(open(os.path.join(d, "old.ter"), "rb").read())     # (write_ter keeps no holes inside)
            lay = np.frombuffer(raw, np.uint8, size * size, 5 + 2 * size * size).reshape(size, size).copy()
            lay[300:304, 300:304] = heightmap.TER_HOLE
            raw[5 + 2 * size * size:5 + 3 * size * size] = lay.tobytes()
            open(os.path.join(d, "old.ter"), "wb").write(bytes(raw))
            s = SimpleNamespace(layout=SimpleNamespace(block=size), out_dir=d, hm=OrderedDict(
                {(1, 0.0, 0.0): {"ter": "old.ter", "q": q, "zBase": zb, "maxHeight": mh},
                 (2, 0.0, 0.0): {"ter": "other-anchor.ter", "q": q, "zBase": zb, "maxHeight": mh}}))
            import geodesy
            anchor = SimpleNamespace(id=1, frame=geodesy.EnuFrame(52.2, 13.5))
            s.donors = lambda *a: earthd.Server.donors(s, *a)
            s.prov_done = OrderedDict()
            got = earthd.Server.donor_chunks(s, anchor, 0.0, 384.0)     # moved 384 m north: 5 of 8 chunk rows shared
            self.assertEqual(got.donated, 8 * 5)
            ch = heightmap.CHUNK
            stored = heightmap.heights(qgrid, zb, mh)
            k0x, k0y = heightmap.lattice_index(0.0, size) // ch, heightmap.lattice_index(384.0, size) // ch
            c = got.get((k0x + 2, k0y + 1))            # the new block's chunk row 1 = the old one's row 4
            np.testing.assert_array_equal(c, stored[4 * ch:5 * ch, 2 * ch:3 * ch])
            self.assertIsNone(got.get((k0x, k0y + 5)), "north of the old block: computed")
            tag = sf.frame_tag(anchor.frame)
            lc = got.landcover.get((tag, k0x + 5, k0y + 1))      # old chunk (5, 4): rock
            self.assertTrue((lc == sf.ROCK).all())
            self.assertTrue((got.landcover.get((tag, k0x + 2, k0y + 1)) == sf.DIRT).all())
            self.assertIsNone(got.landcover.get((tag, k0x + 0, k0y + 1)), "the old edge ring's holes: computed")
            self.assertIsNone(got.landcover.get((tag, k0x + 4, k0y + 1)), "a tunnel mouth's holes: computed")
            self.assertEqual(len(got.landcover.chunks), 4 * 6 - 1)     # not the edge ring (side columns, top row), not the mouth
            self.assertIsNone(earthd.Server.donor_chunks(s, anchor, 0.0, 1024.0), "no overlap: none")
            s.prov_done[(1, 0.0, 768.0)] = s.hm.pop((1, 0.0, 0.0))        # a stand-in's .ter: a donor too
            self.assertEqual(earthd.Server.donor_chunks(s, anchor, 0.0, 1024.0).donated, 8 * 6)
