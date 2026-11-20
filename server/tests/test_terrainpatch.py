"""terrainpatch.py: the ground's collision patch under a collision island off the terrain block (BeamMP support)."""
import os
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import geodesy  # noqa: E402
import terrainpatch  # noqa: E402


class TerrainPatchTest(unittest.TestCase):
    def test_snap_box(self):
        self.assertEqual(terrainpatch.snap_box(10, -70, 650, 570), (0.0, -128.0, 704.0, 576.0))
        x0, y0, x1, y1 = terrainpatch.snap_box(0, 0, 4000, 100)
        self.assertLessEqual(x1 - x0, terrainpatch.MAX_SIDE_M + 2 * terrainpatch.SNAP_M, "clipped, then snapped out")
        self.assertTrue(all(v % terrainpatch.SNAP_M == 0 for v in (x0, y0, x1, y1)))

    def test_mesh_grid_and_winding(self):
        xs, ys = terrainpatch.grid((0.0, 0.0, 16.0, 8.0))
        self.assertEqual((len(xs), len(ys)), (5, 3))
        z = np.add.outer(ys, xs) * 0.1
        pos, tris = terrainpatch.mesh(xs, ys, z, (8.0, 4.0))
        self.assertEqual(pos.shape, (15, 3))
        self.assertEqual(len(tris), 2 * 4 * 2)
        self.assertEqual(tuple(pos[0]), (-8.0, -4.0, 0.0), "relative to the origin")
        n = np.cross(pos[tris[:, 1]] - pos[tris[:, 0]], pos[tris[:, 2]] - pos[tris[:, 0]])
        self.assertTrue((n[:, 2] > 0).all(), "every triangle faces up")

    def test_build_writes_a_collision_shape(self):
        frame = geodesy.EnuFrame(39.28, -76.59)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "p.cdae")
            out, centre, n = terrainpatch.build(frame, lambda la, lo: np.full(np.shape(la), 12.5), (0, 0, 64, 64), path)
            self.assertEqual(out, path)
            self.assertEqual(centre, (32.0, 32.0))
            self.assertEqual(n, 2 * 16 * 16)
            self.assertGreater(os.path.getsize(path), 0)


if __name__ == "__main__":
    unittest.main()
