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

    def drive(self, graded_s, x, vx, then=None):
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


if __name__ == "__main__":
    unittest.main()
