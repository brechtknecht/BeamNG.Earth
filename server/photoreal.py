"""Photorealistic ground: Google's Photorealistic 3D Tiles as the render-only near ground (personal fork spike,
2026-10-23). The physics stays the TerrainBlock; this module only fetches, decodes and places the photogrammetry.

    pr = Photoreal(cache_dir)                       # GOOGLE_MAPS_API_KEY, or CESIUM_ION_TOKEN (asset 2275207), or none: ion through an authenticating proxy
    mesh = pr.mesh(frame, (x0, y0, x1, y1), target_err=1.0)   # Mesh in frame's ENU metres, or None
    mesh.chunks(VERTS_MAX)                          # pieces a 16-bit-index shape can hold
    mesh.atlas(4096)                                # one RGB image for all its source textures, UVs remapped

Tileset (3D Tiles 1.0/1.1, the API's): nodes with an oriented box in ECEF, content .json (a subtree) or .glb
(Draco-compressed triangles, one JPEG/WebP texture per primitive). Child URIs are relative; every request carries
the key and the root's session parameter. Refinement is REPLACE: a node whose geometricError is at most target_err,
or a leaf, is taken whole; otherwise its children (only those whose box reaches the query box).

Heights: the tiles are on the WGS84 ellipsoid, the DEM (dem.py, used as ellipsoidal h by EnuFrame) is orthometric,
so the photogrammetry sits the geoid undulation N above the drive ground (~+47 m in Germany, -30 m in California).
N varies by centimetres over a few km: one offset per anchor, measured (ground_offset), is enough.

The tiles' terms of use forbid caching and derived data beyond a session; this fork is for the owner's own use and
nothing it writes is shared (compute sharing / peerwork stays off with --near-visual photoreal).
Lessons from mapng (docs/google-3d-tiles.md there): BeamNG shapes use 16-bit indices (chunks <= 65535 vertices);
a shape's object name must not end in digits (read as a detail size, the mesh never draws).
"""
import hashlib
import io
import json
import logging
import os
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

log = logging.getLogger("earthd.photoreal")

GOOGLE_ROOT = "https://tile.googleapis.com/v1/3dtiles/root.json"
ION_ASSET = 2275207                 # Cesium ion's "Google Photorealistic 3D Tiles" asset (EEA accounts: no direct API)
ION_ENDPOINT = "https://api.cesium.com/v1/assets/{}/endpoint"
TIMEOUT_S = 30
# requests at once (one tileset level's subtrees, one box's contents): one at a time a 64 m tile was ~90 requests and
# ~20 s (2026-10-23, Felix: "in mapng we loaded the tiles much faster": the browser fetched in parallel)
FETCH_THREADS = int(os.environ.get("BNGE_PHOTOREAL_FETCH", "16"))
MAX_DEPTH = 40
# glTF is y-up; 3D Tiles' content frame is z-up (3D Tiles spec, "glTF transforms")
Y_UP_TO_Z_UP = np.array([[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]], np.float64)

GLB_MAGIC, CHUNK_JSON, CHUNK_BIN = 0x46546C67, 0x4E4F534A, 0x004E4942
COMPONENT = {5120: np.int8, 5121: np.uint8, 5122: np.int16, 5123: np.uint16, 5125: np.uint32, 5126: np.float32}
NCOMP = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class PhotorealError(IOError):
    pass


_pool = None
_pool_lock = threading.Lock()


def _fetch_pool():
    """The module's fetch threads (shared by every Photoreal: the server's tile workers all fetch through it)."""
    global _pool
    with _pool_lock:
        if _pool is None:
            from concurrent.futures import ThreadPoolExecutor
            _pool = ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="photoreal-fetch")
        return _pool


# --- geometry -----------------------------------------------------------------------------------------------------

class Mesh:
    """Triangles in a local frame: positions (n, 3) float64, uvs (n, 2) glTF convention (v down), tris (m, 3),
    tri_tex (m,) index into textures (encoded image bytes, one per source primitive)."""

    def __init__(self, positions, uvs, tris, tri_tex, textures):
        self.positions = np.asarray(positions, np.float64).reshape(-1, 3)
        self.uvs = np.asarray(uvs, np.float64).reshape(-1, 2)
        self.tris = np.asarray(tris, np.int64).reshape(-1, 3)
        self.tri_tex = np.asarray(tri_tex, np.int64).reshape(-1)
        self.textures = list(textures)

    @staticmethod
    def concat(meshes):
        meshes = [m for m in meshes if m is not None and len(m.tris)]
        if not meshes:
            return None
        P, UV, T, TT, tex, nv = [], [], [], [], [], 0
        for m in meshes:
            P.append(m.positions), UV.append(m.uvs), T.append(m.tris + nv), TT.append(m.tri_tex + len(tex))
            tex += m.textures
            nv += len(m.positions)
        return Mesh(np.concatenate(P), np.concatenate(UV), np.concatenate(T), np.concatenate(TT), tex)

    def select(self, keep):
        """The triangles where keep (m,) is true, with only the vertices and textures they use."""
        T, TT = self.tris[keep], self.tri_tex[keep]
        used = np.unique(T)
        new_of = np.full(len(self.positions), -1, np.int64)
        new_of[used] = np.arange(len(used))
        tu = np.unique(TT)
        tex_of = np.full(max(len(self.textures), 1), -1, np.int64)
        tex_of[tu] = np.arange(len(tu))
        return Mesh(self.positions[used], self.uvs[used], new_of[T], tex_of[TT], [self.textures[k] for k in tu])

    def clip(self, box):
        """The triangles whose centroid lies in box (x0, y0, x1, y1): neighbouring boxes get disjoint sets, no seam
        and no overlap between tiles of one level (the edges are the photogrammetry's own, not straight)."""
        x0, y0, x1, y1 = box
        c = self.positions[self.tris].mean(axis=1)
        keep = (c[:, 0] >= x0) & (c[:, 0] < x1) & (c[:, 1] >= y0) & (c[:, 1] < y1)
        return self.select(keep) if keep.any() else None

    def chunks(self, verts_max=65535):
        """Pieces of at most verts_max vertices (triangles in order; a vertex shared across a cut is duplicated)."""
        if len(self.positions) <= verts_max:
            return [self]
        out, start = [], 0
        m = len(self.tris)
        while start < m:
            lo, hi = start + 1, m          # the most triangles from start whose vertices fit
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if len(np.unique(self.tris[start:mid])) <= verts_max:
                    lo = mid
                else:
                    hi = mid - 1
            keep = np.zeros(m, bool)
            keep[start:lo] = True
            out.append(self.select(keep))
            start = lo
        return out

    def atlas(self, max_px=4096):
        """(rgb uint8 [h, w, 3] power-of-two sides, uvs (n, 2) Collada convention v up): every texture shelf-packed
        into one image, scaled down evenly until it fits max_px. Vertices shared by triangles of two textures are not
        expected (one glTF primitive per texture); if they are, the last texture's placement wins."""
        from PIL import Image
        ims = [Image.open(io.BytesIO(b)).convert("RGB") for b in self.textures]
        scale = 1.0
        while True:
            sizes = [(max(1, int(im.width * scale)), max(1, int(im.height * scale))) for im in ims]
            place, w, h = _shelf(sizes, max_px)
            if place is not None:
                break
            scale *= 0.8
        W, H = _pow2(w), _pow2(h)
        sheet = Image.new("RGB", (W, H))
        for im, (sw, sh), (px, py) in zip(ims, sizes, place):
            sheet.paste(im.resize((sw, sh), Image.BILINEAR) if (sw, sh) != im.size else im, (px, py))
        uv = np.zeros((len(self.positions), 2))
        for k, ((sw, sh), (px, py)) in enumerate(zip(sizes, place)):
            v = np.unique(self.tris[self.tri_tex == k])
            # half a texel in from the cell's edge: bilinear filtering never reads the neighbour
            u_ = np.clip(self.uvs[v, 0], 0.0, 1.0) * (sw - 1) + px + 0.5
            v_ = np.clip(self.uvs[v, 1], 0.0, 1.0) * (sh - 1) + py + 0.5
            uv[v, 0] = u_ / W
            uv[v, 1] = 1.0 - v_ / H
        return np.asarray(sheet, np.uint8), uv

    def height_grid(self, box, step, stat="min"):
        """Per-cell height of the mesh over box on a step grid (cells x cells, [j, i], j north), NaN where no triangle:
        every triangle rasterised at its vertices and a few interior points (step-sized), so a cell gets the lowest
        (stat "min") or highest ("max") surface point in it."""
        x0, y0, x1, y1 = box
        nx, ny = int(round((x1 - x0) / step)), int(round((y1 - y0) / step))
        out = np.full(ny * nx, np.inf if stat == "min" else -np.inf)
        V = self.positions[self.tris]                       # (m, 3, 3)
        # barycentric samples: enough that a triangle up to ~4 cells across lands a point in every cell it covers
        edge = np.linalg.norm(V[:, [1, 2, 0]] - V, axis=2).max(axis=1)
        for n in (1, 2, 4, 8, 16):
            sel = (edge <= n * step) if n < 16 else np.ones(len(V), bool)
            sel &= ~np.isnan(edge) if n == 1 else (edge > n / 2 * step) | (n == 16) & (edge > 8 * step)
            if not sel.any():
                continue
            a, b = np.meshgrid(np.arange(n + 1), np.arange(n + 1))
            ok = a + b <= n
            w = np.stack([a[ok], b[ok]], -1) / n            # (k, 2)
            Vs = V[sel]
            pts = (Vs[:, None, 0] * (1 - w.sum(1))[None, :, None] + Vs[:, None, 1] * w[None, :, 0, None]
                   + Vs[:, None, 2] * w[None, :, 1, None]).reshape(-1, 3)
            i = np.floor((pts[:, 0] - x0) / step).astype(np.int64)
            j = np.floor((pts[:, 1] - y0) / step).astype(np.int64)
            inb = (i >= 0) & (i < nx) & (j >= 0) & (j < ny)
            idx = j[inb] * nx + i[inb]
            (np.minimum if stat == "min" else np.maximum).at(out, idx, pts[inb, 2])
        out[~np.isfinite(out)] = np.nan
        return out.reshape(ny, nx)


def _pow2(n):
    return 1 << max(0, int(np.ceil(np.log2(max(1, n)))))


def _shelf(sizes, max_px):
    """Shelf packing, tallest first: ([(x, y)] in input order, width, height) or (None, 0, 0) if it overflows."""
    order = sorted(range(len(sizes)), key=lambda k: -sizes[k][1])
    place = [None] * len(sizes)
    x = y = shelf_h = w = 0
    for k in order:
        sw, sh = sizes[k]
        if sw > max_px:
            return None, 0, 0
        if x + sw > max_px:
            y, x, shelf_h = y + shelf_h, 0, 0
        place[k] = (x, y)
        x += sw
        shelf_h = max(shelf_h, sh)
        w = max(w, x)
        if y + shelf_h > max_px:
            return None, 0, 0
    return place, w, y + shelf_h


# --- glTF ---------------------------------------------------------------------------------------------------------

def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], np.float64).reshape(4, 4).T     # glTF: column-major
    M = np.eye(4)
    if "scale" in node:
        M = np.diag(list(node["scale"]) + [1.0]) @ M
    if "rotation" in node:
        x, y, z, w = node["rotation"]
        R = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                      [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                      [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
        Rm = np.eye(4)
        Rm[:3, :3] = R
        M = Rm @ M
    if "translation" in node:
        T = np.eye(4)
        T[:3, 3] = node["translation"]
        M = T @ M
    return M


def _accessor(gl, binc, k):
    acc = gl["accessors"][k]
    bv = gl["bufferViews"][acc["bufferView"]]
    dt = np.dtype(COMPONENT[acc["componentType"]])
    nc = NCOMP[acc["type"]]
    off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or dt.itemsize * nc
    count = acc["count"]
    raw = np.frombuffer(binc, np.uint8, count=(count - 1) * stride + dt.itemsize * nc, offset=off)
    a = np.lib.stride_tricks.as_strided(raw, (count, dt.itemsize * nc), (stride, 1)).copy()
    a = a.view(dt).reshape(count, nc).astype(np.float64)
    if acc.get("normalized"):
        a /= float(np.iinfo(dt).max)
    return a


def decode_glb(data, tile_transform=None):
    """Mesh in ECEF metres from one tile's .glb (Draco or plain triangles, KHR_mesh_quantization's node scales,
    CESIUM_RTC), tile_transform: the tileset's accumulated 4x4 transform (None: identity)."""
    magic, _ver, _length = struct.unpack_from("<3I", data, 0)
    if magic != GLB_MAGIC:
        raise PhotorealError("not a glb")
    off, gl, binc = 12, None, b""
    while off < len(data):
        clen, ctype = struct.unpack_from("<2I", data, off)
        chunk = data[off + 8:off + 8 + clen]
        if ctype == CHUNK_JSON:
            gl = json.loads(chunk)
        elif ctype == CHUNK_BIN:
            binc = bytes(chunk)
        off += 8 + clen
    if gl is None:
        raise PhotorealError("glb without JSON")
    base = np.eye(4) if tile_transform is None else np.asarray(tile_transform, np.float64)
    rtc = gl.get("extensions", {}).get("CESIUM_RTC", {}).get("center")
    if rtc is not None:
        T = np.eye(4)
        T[:3, 3] = rtc
        base = base @ T
    base = base @ Y_UP_TO_Z_UP
    textures, out = [], []
    tex_index = {}

    def image_bytes(mat_k):
        mat = gl["materials"][mat_k]
        t = mat.get("pbrMetallicRoughness", {}).get("baseColorTexture", {}).get("index")
        if t is None:
            return None
        tex = gl["textures"][t]
        src = tex.get("source")
        if src is None:
            for ext in tex.get("extensions", {}).values():          # EXT_texture_webp / KHR_texture_basisu
                src = ext.get("source", src)
        img = gl["images"][src]
        bv = gl["bufferViews"][img["bufferView"]]
        o = bv.get("byteOffset", 0)
        return binc[o:o + bv["byteLength"]]

    def visit(node_k, parent):
        node = gl["nodes"][node_k]
        M = parent @ _node_matrix(node)
        if "mesh" in node:
            for prim in gl["meshes"][node["mesh"]]["primitives"]:
                if prim.get("mode", 4) != 4:
                    continue
                pos, uv, idx = _primitive(gl, binc, prim)
                P = (np.c_[pos, np.ones(len(pos))] @ M.T)[:, :3]
                mk = prim.get("material")
                tk = 0
                if mk is not None:
                    if mk not in tex_index:
                        b = image_bytes(mk)
                        tex_index[mk] = len(textures) if b is not None else -1
                        if b is not None:
                            textures.append(b)
                    tk = tex_index[mk]
                if tk < 0 or uv is None:
                    continue                 # untextured: nothing to see (the API has none)
                out.append((P, uv, idx.reshape(-1, 3), tk))
        for c in node.get("children", []):
            visit(c, M)

    scene = gl["scenes"][gl.get("scene", 0)]
    for n in scene["nodes"]:
        visit(n, base)
    if not out:
        return None
    P, UV, T, TT, nv = [], [], [], [], 0
    for p, uv, t, tk in out:
        P.append(p), UV.append(uv), T.append(t + nv), TT.append(np.full(len(t), tk))
        nv += len(p)
    return Mesh(np.concatenate(P), np.concatenate(UV), np.concatenate(T), np.concatenate(TT), textures)


def _primitive(gl, binc, prim):
    """(positions (n, 3), uvs (n, 2) or None, indices (3m,)) of one triangle primitive."""
    draco = prim.get("extensions", {}).get("KHR_draco_mesh_compression")
    if draco is not None:
        import DracoPy
        bv = gl["bufferViews"][draco["bufferView"]]
        o = bv.get("byteOffset", 0)
        dm = DracoPy.decode(binc[o:o + bv["byteLength"]])
        pos = np.asarray(dm.points, np.float64).reshape(-1, 3)
        idx = np.asarray(dm.faces, np.int64).reshape(-1)
        uv = None
        uid = draco["attributes"].get("TEXCOORD_0")
        if uid is not None:
            uv = _draco_attr(dm, uid, len(pos))
            if uv is not None and "TEXCOORD_0" in prim["attributes"]:
                acc = gl["accessors"][prim["attributes"]["TEXCOORD_0"]]
                if acc.get("normalized") and uv.max() > 1.0:      # quantised ints Draco left as-is
                    uv = uv / float(np.iinfo(COMPONENT[acc["componentType"]]).max)
        return pos, uv, idx
    pos = _accessor(gl, binc, prim["attributes"]["POSITION"])
    uv = _accessor(gl, binc, prim["attributes"]["TEXCOORD_0"]) if "TEXCOORD_0" in prim["attributes"] else None
    idx = (_accessor(gl, binc, prim["indices"]).astype(np.int64).reshape(-1) if "indices" in prim
           else np.arange(len(pos)))
    return pos, uv, idx


def _draco_attr(dm, unique_id, n):
    """A Draco attribute's values by its unique id (DracoPy >= 1.4: dm.attributes, list of dicts with 'unique_id'
    and 'data'; older: tex_coord for the first texture coordinate)."""
    for a in getattr(dm, "attributes", None) or []:
        if a.get("unique_id") == unique_id:
            return np.asarray(a["data"], np.float64).reshape(n, -1)[:, :2]
    tc = getattr(dm, "tex_coord", None)
    if tc is not None and len(tc):
        return np.asarray(tc, np.float64).reshape(n, -1)[:, :2]
    return None


# --- the tileset --------------------------------------------------------------------------------------------------

def _box_enu_bbox(box, frame):
    """The ENU (x0, y0, x1, y1) a 3D Tiles box (12 numbers, ECEF center + three half axes) covers."""
    c = np.array(box[:3], np.float64)
    ax = np.array(box[3:12], np.float64).reshape(3, 3)
    ce = np.array(frame.ecef_to_enu(*c))
    r = frame.rot                                         # rows: E, N, U in ECEF
    ext = np.abs(ax @ r.T).sum(axis=0)                    # half extents along E, N, U
    return ce[0] - ext[0], ce[1] - ext[1], ce[0] + ext[0], ce[1] + ext[1]


def _region_enu_bbox(region, frame):
    w, s, e, n = region[:4]
    lats = np.degrees([s, s, n, n, (s + n) / 2])
    lons = np.degrees([w, e, w, e, (w + e) / 2])
    x, y, _ = frame.geodetic_to_enu(lats, lons, np.zeros(5))
    return min(x), min(y), max(x), max(y)


def _overlaps(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


_conns = threading.local()


def _keepalive_open(url):
    """GET url over this thread's kept-alive HTTPS connection to its host (urllib opens a new TCP + TLS connection
    per request: ~3 round trips more for each of a tile's 20-90 files). Through HTTPS_PROXY when set (CONNECT)."""
    import http.client
    import ssl
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    pool = getattr(_conns, "pool", None)
    if pool is None:
        pool = _conns.pool = {}
    path = parts.path + ("?" + parts.query if parts.query else "")
    for attempt in (0, 1, 2):
        conn = pool.get(host)
        if conn is None:
            ctx = getattr(_conns, "ctx", None) or ssl.create_default_context()
            proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
            if proxy:
                pp = urllib.parse.urlsplit(proxy if "://" in proxy else "http://" + proxy)
                conn = http.client.HTTPSConnection(pp.hostname, pp.port or 80, timeout=TIMEOUT_S, context=ctx)
                conn.set_tunnel(host, parts.port or 443)
            else:
                conn = http.client.HTTPSConnection(host, parts.port or 443, timeout=TIMEOUT_S, context=ctx)
            pool[host] = conn
        t0 = time.monotonic()
        try:
            conn.request("GET", path, headers={"Accept-Encoding": "identity"})
            r = conn.getresponse()
            data = r.read()
        except ssl.SSLCertVerificationError:
            import certifi     # the packaged app's CA certificates where the system has none it finds (launcher.py)
            _conns.ctx = ssl.create_default_context(cafile=certifi.where())
            pool.pop(host, None).close()
            continue
        except (http.client.HTTPException, OSError) as e:
            pool.pop(host, None).close()        # the server closed the idle connection: once more on a new one
            if time.monotonic() - t0 > 2.0:
                log.info("photoreal: %s failed after %.1f s: %r", host, time.monotonic() - t0, e)
            if attempt == 2:
                raise
            continue
        if time.monotonic() - t0 > 2.0:
            log.info("photoreal: %s took %.1f s (%d bytes)", host, time.monotonic() - t0, len(data))
        if r.status >= 400:
            raise urllib.error.HTTPError(url, r.status, r.reason, r.headers, io.BytesIO(data))
        if r.status in (301, 302, 303, 307, 308) and r.getheader("Location"):
            return _keepalive_open(urllib.parse.urljoin(url, r.getheader("Location")))
        return data
    raise OSError(f"photoreal: {url}: no connection")


def _urlopen(url):
    import ssl
    try:
        return urllib.request.urlopen(url, timeout=TIMEOUT_S).read()
    except urllib.error.URLError as e:
        if not isinstance(e.reason, ssl.SSLCertVerificationError):
            raise
        import certifi     # the packaged app's CA certificates where the system has none it finds (launcher.py)
        ctx = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(url, timeout=TIMEOUT_S, context=ctx).read()


class Photoreal:
    """The tileset, fetched lazily, its subtrees and contents cached on disk (by URL path, without key/session)."""

    def __init__(self, cache_dir, key=None, ion_token=None, opener=None):
        self.key = key or os.environ.get("GOOGLE_MAPS_API_KEY")
        self.ion_token = ion_token or os.environ.get("CESIUM_ION_TOKEN")
        # neither: Cesium ion without a token of ours (a proxy that adds the Authorization header, the cloud sessions)
        self.cache_dir = os.path.join(cache_dir, "photoreal")
        os.makedirs(self.cache_dir, exist_ok=True)
        self._open = opener or _keepalive_open
        self._lock = threading.Lock()
        self._root = None
        self._params = {}               # query parameters every request carries (key, session)
        self._json = {}                 # url path -> parsed subtree
        self._file_locks = {}           # cache path -> Lock (one fetch / write per file)
        self.fetched = self.cached = 0
        self.on_file = None             # (path, bytes) of every cache file read or written: DiskBudget's LRU

    # urls
    def _root_url(self):
        if self.key:
            return GOOGLE_ROOT, {"key": self.key}
        url = ION_ENDPOINT.format(ION_ASSET)
        if self.ion_token:
            url += "?" + urllib.parse.urlencode({"access_token": self.ion_token})
        try:
            ep = json.loads(self._open(url))
        except urllib.error.HTTPError as e:
            raise PhotorealError(f"Cesium ion: {e.code} {e.read()[:200]!r} (CESIUM_ION_TOKEN, scope assets:read, "
                                 f"asset {ION_ASSET} in My Assets)") from e
        opts = ep.get("options", {})
        url = opts.get("url") or ep.get("url")
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "key" not in q and opts.get("key"):
            q["key"] = opts["key"]
        return url.split("?")[0], q

    def _get(self, url):
        """Bytes of url (absolute, with its own query), from the disk cache when there. One fetch per file at a time:
        parallel tiles share contents, and on Windows two threads replacing (or one reading while another replaces)
        the same cache file fail with PermissionError (Felix's PC, 2026-10-24: 16 tiles fell back to the DEM)."""
        parts = urllib.parse.urlsplit(url)
        name = hashlib.sha1(parts.path.encode()).hexdigest()
        path = os.path.join(self.cache_dir, name[:2], name)
        with self._lock:
            flock = self._file_locks.setdefault(path, threading.Lock())
        with flock:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        data = f.read()
                    with self._lock:
                        self.cached += 1
                    if self.on_file is not None:
                        self.on_file(path, len(data))
                    return data
                except OSError:         # being replaced by another process (two servers): fetch it again
                    pass
            q = dict(urllib.parse.parse_qsl(parts.query))
            q.update({k: v for k, v in self._params.items() if k not in q})
            data = self._open(urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(q))))
            with self._lock:
                self.fetched += 1
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                tmp = path + ".tmp%d" % threading.get_ident()
                with open(tmp, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
                if self.on_file is not None:
                    self.on_file(path, len(data))
            except OSError as e:        # the cache is only a cache: the bytes are in hand
                log.debug("photoreal: cache write %s: %s", path, e)
                try:
                    os.remove(tmp)
                except OSError:
                    pass
            return data

    def root(self):
        with self._lock:
            if self._root is None:
                url, params = self._root_url()
                self._params = dict(params)
                q = urllib.parse.urlencode(params)
                ts = json.loads(self._open(url + "?" + q))
                self.fetched += 1
                self._root = (url, ts)
            return self._root

    def _child_url(self, base, uri):
        url = urllib.parse.urljoin(base, uri)
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "session" in q and "session" not in self._params:
            self._params["session"] = q["session"]      # the root's first children name the session
        return url

    def _subtree(self, url):
        p = urllib.parse.urlsplit(url).path
        ts = self._json.get(p)
        if ts is None:
            ts = json.loads(self._get(url))
            self._json[p] = ts
        return ts

    def contents(self, frame, box, target_err):
        """[(glb url, 4x4 transform)] of the nodes that cover box at target_err (REPLACE refinement). The tree is
        walked a subtree level at a time, each level's subtree files fetched at once."""
        url, ts = self.root()
        out = []
        frontier = [(ts["root"], url, np.eye(4), 0)]
        while frontier:
            subs = []           # (subtree url, transform, depth) to fetch for the next level
            for node, base, T, depth in frontier:
                self._walk(node, base, T, frame, box, target_err, out, depth, subs)
            got = list(_fetch_pool().map(lambda sub: self._subtree(sub[0]), subs)) if subs else []
            frontier = [(g["root"], u, T, d + 1) for g, (u, T, d) in zip(got, subs)]
        return out

    def _walk(self, node, base, parent_T, frame, box, target_err, out, depth, subs):
        if depth > MAX_DEPTH:
            return
        T = parent_T @ (np.array(node["transform"], np.float64).reshape(4, 4).T if "transform" in node else np.eye(4))
        bv = node.get("boundingVolume", {})
        if "box" in bv:
            if not _overlaps(_box_enu_bbox(np.r_[(T @ np.r_[bv["box"][:3], 1.0])[:3],
                                                 (T[:3, :3] @ np.array(bv["box"][3:]).reshape(3, 3).T).T.ravel()],
                                           frame), box):
                return
        elif "region" in bv and not _overlaps(_region_enu_bbox(bv["region"], frame), box):
            return
        content = node.get("content") or (node.get("contents") or [None])[0]
        uri = content and (content.get("uri") or content.get("url"))
        children = node.get("children") or []
        if uri is not None and uri.split("?")[0].endswith(".json"):
            subs.append((self._child_url(base, uri), T, depth))
            return
        fine_enough = node.get("geometricError", 0.0) <= target_err
        if uri is not None and (fine_enough or not children):
            out.append((self._child_url(base, uri), T))
            return
        for c in children:
            self._walk(c, base, T, frame, box, target_err, out, depth + 1, subs)

    def prefetch(self, frame, box, target_err):
        """Fetch into the disk cache what mesh(frame, box, target_err) will read (no decoding): the server, for
        the tiles on the car's way, so their builds find everything on disk. -> files fetched."""
        n0 = self.fetched
        for _ in _fetch_pool().map(lambda c: self._get(c[0]), self.contents(frame, box, target_err)):
            pass
        return self.fetched - n0

    def mesh(self, frame, box, target_err=1.0, margin=0.0):
        """Mesh of the photogrammetry over box (ENU of frame), clipped to it, or None (no coverage)."""
        x0, y0, x1, y1 = box
        q = (x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        def part(c):
            m = decode_glb(self._get(c[0]), c[1])
            if m is None:
                return None
            x, y, z = frame.ecef_to_enu(m.positions[:, 0], m.positions[:, 1], m.positions[:, 2])
            m.positions = np.stack([x, y, z], -1)
            return m.clip(box)
        return Mesh.concat(list(_fetch_pool().map(part, self.contents(frame, q, target_err))))


# --- placing it on the drive ground -------------------------------------------------------------------------------

def ground_offset(mesh, box, drive_u, step=2.0, pct=10.0):
    """The vertical offset (m) to add to the photogrammetry so it sits on the drive ground: the median, over the
    cells where both exist, of drive ground minus the mesh's lowest point per cell, taken over the cells in the
    lowest pct percent of that difference's spread around its median (buildings and trees only raise the mesh: the
    open ground is where the mesh is lowest relative to the DEM). drive_u: (ny, nx) drive-ground heights on the same
    step grid as mesh.height_grid(box, step)."""
    low = mesh.height_grid(box, step, "min")
    d = np.asarray(drive_u) - low
    d = d[np.isfinite(d)]
    if not len(d):
        return None
    # mesh above ground (roofs, canopies) makes d more negative: the open ground is the upper part of d
    cut = np.percentile(d, 100.0 - pct)
    return float(np.median(d[d >= cut - 1.0]))


# --- the near tiles (earthd --near-visual photoreal) ----------------------------------------------------------------

def normals(positions, tris):
    """Area-weighted vertex normals (n, 3), unit length (straight up where a vertex has no area)."""
    V = positions[tris]
    fn = np.cross(V[:, 1] - V[:, 0], V[:, 2] - V[:, 0])
    out = np.zeros_like(positions)
    for k in range(3):
        np.add.at(out, tris[:, k], fn)
    ln = np.linalg.norm(out, axis=1)
    out[ln == 0] = (0.0, 0.0, 1.0)
    ln[ln == 0] = 1.0
    return out / ln[:, None]


def tile_mesh(pr, frame, box, offset, target_err, verts_max=65535, coarsest=64.0, start_err=None):
    """(Mesh shifted up by offset, the geometric error used) over box, the finest from target_err (doubled) whose
    vertices fit one 16-bit-index shape; (None, err) where the tileset has nothing there or nothing fits.
    start_err: begin there instead (what fitted the last tile of this size: a finer try is a download thrown away)."""
    err = max(target_err, start_err or 0.0)
    while err <= coarsest:
        m = pr.mesh(frame, box, err)
        if m is None:
            return None, err
        if len(m.positions) <= verts_max:
            m.positions = m.positions + (0.0, 0.0, offset)
            return m, err
        err *= 2.0
    return None, err / 2.0


# --- worker processes (earthd: the near tiles built off the server's interpreter lock) -----------------------------

_worker = None


def worker_init(cache_dir, level=logging.INFO, fetch_threads=None):
    """ProcessPoolExecutor initializer: this process's Photoreal (its own fetch threads, fetch_threads of them when
    given; the disk cache is shared)."""
    global _worker, FETCH_THREADS
    if fetch_threads:
        FETCH_THREADS = fetch_threads
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _worker = Photoreal(cache_dir)
    # exit with the server (as blockworker's): a stopped server left these processes running, ~200 MB each, 71 of
    # them after a day of simulated drives (2026-10-25)
    import multiprocessing
    parent = multiprocessing.parent_process()       # (its sentinel: a process handle on Windows, where getppid stays)
    if parent is not None:
        threading.Thread(target=_watch_parent, args=(parent,), daemon=True, name="parent-watch").start()


def _watch_parent(parent):
    parent.join()
    os._exit(0)


def build_tile(lat0, lon0, bbox, offset, target_err, verts_max, start_err, tex_px, dds_path, cells=32):
    """Worker process: the photoreal tile over bbox (ENU of the frame at lat0, lon0) with its atlas written to
    dds_path. -> dict: err, and when there is a mesh: positions (ENU, shifted by offset), tris, uv, normals,
    tex_bytes, px (w, h), grid ((cells + 1)² heights of the mesh at the layout's vertices, for the swap deltas: the
    tile needs no DEM geometry), fetched, seconds."""
    import time
    import geodesy
    import imagery
    t0 = time.perf_counter()
    n0 = _worker.fetched
    files = []                          # (path, bytes): the server's DiskBudget (one task at a time per process)
    _worker.on_file = lambda path, n: files.append((path, n))
    mesh, err = tile_mesh(_worker, geodesy.EnuFrame(lat0, lon0), bbox, offset, target_err, verts_max,
                          start_err=start_err)
    if mesh is None:
        return {"err": err, "files": files}
    rgb, uv = mesh.atlas(tex_px)
    tris = mesh.tris.astype(np.int64)
    x0, y0, x1, y1 = bbox
    sp = (x1 - x0) / cells
    grid = mesh.height_grid((x0 - sp / 2, y0 - sp / 2, x1 + sp / 2, y1 + sp / 2), sp, "max")
    if np.isfinite(grid).any():
        grid = np.where(np.isfinite(grid), grid, np.nanmean(np.where(np.isfinite(grid), grid, np.nan)))
    else:
        grid = np.full((cells + 1, cells + 1), float(np.mean(mesh.positions[:, 2])))
    return {"err": err, "positions": mesh.positions, "tris": tris, "uv": uv, "normals": normals(mesh.positions, tris),
            "grid": grid.astype(np.float32),
            "tex_bytes": imagery.write_dds(dds_path, rgb), "px": (rgb.shape[1], rgb.shape[0]),
            "fetched": _worker.fetched - n0, "seconds": time.perf_counter() - t0, "files": files}


def prefetch_tiles(lat0, lon0, boxes):
    """Worker process: Photoreal.prefetch of each (bbox, target_err) in boxes (ENU of the frame at lat0, lon0), all
    at once (each walk is a chain of round trips). -> dict: fetched, files."""
    import geodesy
    from concurrent.futures import ThreadPoolExecutor
    frame = geodesy.EnuFrame(lat0, lon0)
    files = []
    _worker.on_file = lambda path, n: files.append((path, n))

    def one(b):
        try:
            return _worker.prefetch(frame, *b)
        except Exception as e:      # the build fetches what is missing
            log.debug("photoreal: prefetch %s: %s", b[0], e)
            return 0
    with ThreadPoolExecutor(max(1, len(boxes))) as ex:
        n = sum(ex.map(one, boxes))
    return {"fetched": n, "files": files}


class DiskBudget:
    """LRU cap on the photoreal disk cache (Felix's PC, 2026-10-24: ~10 GB right away; the photogrammetry is
    ~1 GB per km driven, its own budget rather than the shared cache's 20 GB, whose files are protected for an
    hour). touch() marks files used (the server's offsets, the worker processes' results); over the cap, the
    least recently used files not touched within protect_s go, in a thread, down to LOW of the cap."""

    LOW = 0.8
    RETRY_S = 30.0

    def __init__(self, root, cap_bytes, protect_s=300.0, now=None, start=True):
        import time
        self.root, self.cap, self.protect_s = root, cap_bytes, protect_s
        self.now = now or time.time
        self._lock = threading.Lock()
        self._files = {}                # path -> [bytes, last use]
        self.used = 0
        self._busy = False
        self._retry_at = 0.0
        self.evicted = 0
        if start:
            threading.Thread(target=self._start, name="photoreal-budget", daemon=True).start()

    def scan(self):
        """The files already there (an earlier run's), their mtime as the last use."""
        found = {}
        try:
            subs = [d.path for d in os.scandir(self.root) if d.is_dir()]
        except OSError:
            subs = []
        for d in subs:
            try:
                with os.scandir(d) as it:
                    for e in it:
                        if e.is_file() and ".tmp" not in e.name:
                            st = e.stat()
                            found[e.path] = [st.st_size, st.st_mtime]
            except OSError:
                continue
        with self._lock:
            for path, e in found.items():
                if path not in self._files:
                    self._files[path] = e
                    self.used += e[0]

    def _start(self):
        self.scan()
        log.info("photoreal: disk cache %.1f GB of %.1f GB", self.used / 1e9, self.cap / 1e9)
        self.check()

    def touch(self, path, size):
        t = self.now()
        with self._lock:
            e = self._files.get(path)
            if e is None:
                self._files[path] = [size, t]
                self.used += size
            else:
                self.used += size - e[0]
                e[0], e[1] = size, t
        self.check()

    def check(self, wait=False):
        with self._lock:
            if not self.cap or self.used <= self.cap or self._busy or time.monotonic() < self._retry_at:
                return
            self._busy = True
        if wait:
            self.evict()
        else:
            threading.Thread(target=self.evict, name="photoreal-evict", daemon=True).start()

    def evict(self):
        try:
            floor = self.now() - self.protect_s
            with self._lock:
                order = sorted((e[1], p) for p, e in self._files.items() if e[1] < floor)
            target = int(self.cap * self.LOW)
            n = b = 0
            for t, path in order:
                if self.used <= target:
                    break
                with self._lock:
                    e = self._files.get(path)
                    if e is None or e[1] != t:      # used again since
                        continue
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass
                except OSError:                     # open in a worker (Windows): next time
                    continue
                with self._lock:
                    if self._files.pop(path, None) is not None:
                        self.used -= e[0]
                n, b = n + 1, b + e[0]
            self.evicted += n
            if self.used > target:
                # all the rest used within protect_s: not again for a while (every touch sorted every file again, in
                # a new thread each: 10% of the server's interpreter in a simulated drive, 2026-10-25)
                self._retry_at = time.monotonic() + self.RETRY_S
            if n:
                log.info("photoreal: disk cache: %d least recently used files removed (%.2f GB), %.1f GB of %.1f GB",
                         n, b / 1e9, self.used / 1e9, self.cap / 1e9)
        finally:
            with self._lock:
                self._busy = False
