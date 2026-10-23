"""photoreal.py (personal fork): glb decode, the tileset walk, clipping, chunks, placing on the drive ground. Offline:
a fake opener serves a synthetic tileset of plain (not Draco) glTF quads."""
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import geodesy  # noqa: E402
import photoreal as pr  # noqa: E402

LAT, LON = 48.1585, 11.5856
GEOID = 47.0            # the fake photogrammetry sits this far above the drive ground


def png(rgb):
    b = io.BytesIO()
    Image.new("RGB", (8, 8), rgb).save(b, "PNG")
    return b.getvalue()


def glb(frame, x0, y0, size, h, n=4, rgb=(200, 0, 0)):
    """A flat n x n quad grid over (x0, y0)-(x0 + size, y0 + size) at height h in frame, as a glb whose positions are
    ECEF relative to CESIUM_RTC (y-up glTF: the inverse of the z-up rotation applied)."""
    xs = np.linspace(x0, x0 + size, n + 1)
    X, Y = np.meshgrid(xs, np.linspace(y0, y0 + size, n + 1))
    ecef = np.stack(frame.enu_to_ecef(X.ravel(), Y.ravel(), np.full(X.size, h)), -1)
    centre = ecef.mean(0)
    p = ecef - centre
    p = np.c_[p[:, 0], p[:, 2], -p[:, 1]].astype(np.float32)          # z-up -> y-up
    uv = np.c_[(X.ravel() - x0) / size, 1 - (Y.ravel() - y0) / size].astype(np.float32)
    idx = np.arange(X.size).reshape(X.shape)
    a, b, c, d = idx[:-1, :-1].ravel(), idx[:-1, 1:].ravel(), idx[1:, :-1].ravel(), idx[1:, 1:].ravel()
    tris = np.r_[np.c_[a, b, d], np.c_[a, d, c]].astype(np.uint32)
    img = png(rgb)
    blobs = [p.tobytes(), uv.tobytes(), tris.tobytes(), img]
    views, off = [], 0
    for bl in blobs:
        views.append({"buffer": 0, "byteOffset": off, "byteLength": len(bl)})
        off += (len(bl) + 3) // 4 * 4
    binc = b"".join(bl + b"\0" * ((-len(bl)) % 4) for bl in blobs)
    gl = {"asset": {"version": "2.0"}, "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
          "extensions": {"CESIUM_RTC": {"center": centre.tolist()}},
          "meshes": [{"primitives": [{"attributes": {"POSITION": 0, "TEXCOORD_0": 1}, "indices": 2, "material": 0}]}],
          "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
          "textures": [{"source": 0}], "images": [{"bufferView": 3, "mimeType": "image/png"}],
          "buffers": [{"byteLength": len(binc)}], "bufferViews": views,
          "accessors": [{"bufferView": 0, "componentType": 5126, "count": len(p), "type": "VEC3"},
                        {"bufferView": 1, "componentType": 5126, "count": len(uv), "type": "VEC2"},
                        {"bufferView": 2, "componentType": 5125, "count": tris.size, "type": "SCALAR"}]}
    js = json.dumps(gl).encode()
    js += b" " * ((-len(js)) % 4)
    body = struct.pack("<2I", len(js), pr.CHUNK_JSON) + js + struct.pack("<2I", len(binc), pr.CHUNK_BIN) + binc
    return struct.pack("<3I", pr.GLB_MAGIC, 2, 12 + len(body)) + body


def box_of(frame, x0, y0, size, h):
    c = np.array(frame.enu_to_ecef(x0 + size / 2, y0 + size / 2, h))
    r = frame.rot
    return list(c) + list(r[0] * size / 2) + list(r[1] * size / 2) + list(r[2] * 5.0)


class FakeServer:
    """root.json: a coarse 256 m node (error 8) refined into four 128 m leaves, one of them through a subtree."""

    def __init__(self, frame):
        f = frame
        self.files = {"/root.json": {"root": {
            "boundingVolume": {"box": box_of(f, -128, -128, 256, GEOID)}, "geometricError": 8.0,
            "content": {"uri": "coarse.glb"},
            "children": [
                {"boundingVolume": {"box": box_of(f, -128, -128, 128, GEOID)}, "geometricError": 0.5,
                 "content": {"uri": "sw.glb"}},
                {"boundingVolume": {"box": box_of(f, 0, -128, 128, GEOID)}, "geometricError": 0.5,
                 "content": {"uri": "se.glb"}},
                {"boundingVolume": {"box": box_of(f, -128, 0, 128, GEOID)}, "geometricError": 0.5,
                 "content": {"uri": "sub/nw.json?session=abc"}},
                {"boundingVolume": {"box": box_of(f, 0, 0, 128, GEOID)}, "geometricError": 0.5,
                 "content": {"uri": "ne.glb"}}]}},
            "/sub/nw.json": {"root": {"boundingVolume": {"box": box_of(f, -128, 0, 128, GEOID)},
                                      "geometricError": 0.5, "content": {"uri": "nw.glb"}}},
        }
        self.glbs = {"/coarse.glb": glb(f, -128, -128, 256, GEOID, 2, (0, 0, 200)),
                     "/sw.glb": glb(f, -128, -128, 128, GEOID), "/se.glb": glb(f, 0, -128, 128, GEOID),
                     "/sub/nw.glb": glb(f, -128, 0, 128, GEOID), "/ne.glb": glb(f, 0, 0, 128, GEOID)}
        self.urls = []

    def __call__(self, url):
        import urllib.parse
        self.urls.append(url)
        path = urllib.parse.urlsplit(url).path.replace("/v1/3dtiles", "", 1)
        if path.endswith(".json"):
            return json.dumps(self.files[path]).encode()
        return self.glbs[path]


class PhotorealTest(unittest.TestCase):
    def setUp(self):
        self.frame = geodesy.EnuFrame(LAT, LON)
        self.srv = FakeServer(self.frame)
        self.tmp = tempfile.TemporaryDirectory()
        self.p = pr.Photoreal(self.tmp.name, key="k", opener=self.srv)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fine_mesh_placed_in_enu(self):
        m = self.p.mesh(self.frame, (-100, -100, 100, 100), target_err=1.0)
        self.assertIsNotNone(m)
        self.assertEqual(len(m.textures), 4)                    # the four leaves, not the coarse node
        np.testing.assert_allclose(m.positions[:, 2], GEOID, atol=0.01)
        c = m.positions[m.tris].mean(1)
        self.assertTrue(((c[:, :2] >= -100) & (c[:, :2] < 100)).all())
        self.assertTrue(all("key=k" in u for u in self.srv.urls))
        self.assertTrue(any("session=abc" in u for u in self.srv.urls if u.endswith(".glb") or ".glb?" in u))

    def test_coarse_and_cache(self):
        m = self.p.mesh(self.frame, (-100, -100, 100, 100), target_err=10.0)
        self.assertEqual(len(m.textures), 1)
        n = len(self.srv.urls)
        q = pr.Photoreal(self.tmp.name, key="k", opener=self.srv)
        q.mesh(self.frame, (-100, -100, 100, 100), target_err=10.0)
        self.assertEqual(len(self.srv.urls), n + 1)             # only the root again: the rest from the disk cache

    def test_only_overlapping_leaves(self):
        self.p.mesh(self.frame, (10, 10, 50, 50), target_err=1.0)
        glbs = [u for u in self.srv.urls if ".glb" in u]
        self.assertEqual(len(glbs), 1)
        self.assertIn("/ne.glb", glbs[0])

    def test_ground_offset_and_tile_mesh(self):
        box = (-64, -64, 64, 64)
        m = self.p.mesh(self.frame, box, 1.0)
        drive = np.zeros((64, 64))
        off = pr.ground_offset(m, box, drive, 2.0)
        self.assertAlmostEqual(off, -GEOID, places=1)
        t, err = pr.tile_mesh(self.p, self.frame, box, off, 1.0, verts_max=10 ** 6)
        np.testing.assert_allclose(t.positions[:, 2], 0.0, atol=0.02)
        self.assertEqual(err, 1.0)
        t, err = pr.tile_mesh(self.p, self.frame, box, off, 1.0, verts_max=20)     # coarser until it fits
        self.assertEqual(err, 8.0)
        self.assertLessEqual(len(t.positions), 20)

    def test_atlas_chunks_normals(self):
        m = self.p.mesh(self.frame, (-128, -128, 128, 128), 1.0)
        rgb, uv = m.atlas(64)
        self.assertEqual(rgb.shape[2], 3)
        self.assertTrue(((uv >= 0) & (uv <= 1)).all())
        parts = m.chunks(30)
        self.assertEqual(sum(len(p.tris) for p in parts), len(m.tris))
        self.assertTrue(all(len(p.positions) <= 30 for p in parts))
        n = pr.normals(m.positions, m.tris)
        self.assertTrue((n[:, 2] > 0.99).all())                 # flat ground, counter-clockwise from above


if __name__ == "__main__":
    unittest.main()
