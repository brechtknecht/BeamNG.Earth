"""Overture Maps GeoParquet per cell, through the STAC per-file index and DuckDB (mapdata.py).

docs/research/2026-10-10-world-data-sources.md §2.1: a release is ~1,000 zstd GeoParquet files
on S3 (Azure mirror), spatially sorted. Querying the whole release glob makes DuckDB read every
file's footer (35 s for a 2.4 km box); the release's STAC collection lists each file's bbox, so
only the 1-4 files that intersect the cell are opened, and inside them DuckDB skips the row groups
whose bbox statistics miss the cell. Every byte comes through rangeread (netfetch's limits, the
block cache): DuckDB runs with no extensions loaded or installed and opens no socket itself; the
geometry arrives as WKB (DuckDB core's ST_AsWKB) and is decoded in mapgeom.

STAC files (stac.overturemaps.org/<release>/<theme>/<type>/collection.json and one item per file
for its URLs and size) are cached under <raw>/overture/<release>/stac/; parquet blocks under
<raw>/overture/<release>/<theme>/<type>/<file>/. A release is immutable, so both are kept forever.

Layers (LAYERS: name -> theme, type, columns): roads (transportation/segment: class, subclass,
surface / flags / width / level / speed / subclass rules along the segment, connector ids with
their position), connectors (transportation/connector), buildings (buildings/building: height,
min_height, floors, roof and facade fields, class), water and land_use (base: subtype, class,
tags), infrastructure (base: power lines and plants, towers, barriers, bridges...). Rows are
deduplicated by id (highest version) and sorted by id, so a cell's content does not depend on
which file or row group a feature came from, or in which order they were read. Water and land-use
polygons are clipped to the cell (an ocean or a national park would otherwise repeat its whole
outline in every cell); everything else is whole (a building or road segment that crosses the
cell edge is listed in every cell it touches, `own` marks the one cell that owns it).
"""
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor

import numpy as np

import mapgeom as mg
import netfetch
import rangeread
import staging
import xproc

log = logging.getLogger("earthd.mapdata")

# The pinned Overture release (update path: docs/ARCHITECTURE.md "Map data"; `mapdata.py --latest`
# prints the newest one). Overture removes old releases from the buckets after some months: a
# pinned release that is gone raises ReleaseGone for cells not cached yet.
RELEASE = "2026-09-23.1"
STAC_ROOT = "https://stac.overturemaps.org"
# DuckDB worker threads per query (the server is niced; the game comes first). 1: a cell's scans are small, and a second
# thread spun more than it worked (6 Munich cells' layers: 10.2 CPU-s at 2 threads, 7.0 at 1, the same wall time,
# 2026-10-26); the cells are read in parallel by mapdata's pool anyway. BNGE_DUCKDB_THREADS=2: as before.
DUCKDB_THREADS = int(os.environ.get("BNGE_DUCKDB_THREADS", "1"))
# The geometry read as the file's own WKB (GeoParquet conversion off): DuckDB decoded every geometry to its GEOMETRY type
# only for ST_AsWKB to encode it back (~15 % of a cell's scan). BNGE_GEO_CONVERSION=1: as before.
RAW_WKB = os.environ.get("BNGE_GEO_CONVERSION", "0") != "1"
REMOTE_FILES = 32               # open RemoteFiles kept (their blocks share rangeread's memory LRU)
# DuckDB's own prefetching reads whole row groups one after the other; Reader._plan fetches what the
# query needs in parallel first, so DuckDB reads lazily from the block cache
PARQUET_CONFIG = {"disable_parquet_prefetching": True}
FETCH_THREADS = 4               # parallel range requests per Reader (the hosts allow 4 at once)
# bytes read from a file's end first for its footer: the release's footers are 0.16-2.05 MB (2026-09-23.1), so one guess
# of a block took a second round trip to Oregon for most files (the I/O audit, 2026-10-26). The guess is learnt per
# theme/type: a footer grows with its file's row groups, so the largest footer-to-file ratio seen there (x FOOTER_PAD)
# times the file's size, at least FOOTER_MIN; FOOTER_GUESS before the first. BNGE_FOOTER_GUESS=262144 (and no
# learning, BNGE_FOOTER_LEARN=0): as before
FOOTER_GUESS = int(os.environ.get("BNGE_FOOTER_GUESS", str(1024 * 1024)))
FOOTER_MIN = 256 * 1024
FOOTER_MAX = 4 * 1024 * 1024
FOOTER_PAD = 1.3
FOOTER_LEARN = os.environ.get("BNGE_FOOTER_LEARN", "1") != "0"
MERGE_GAP = 64 * 1024           # column chunks closer than this are fetched in one range
BASE_COLUMNS = {"id", "geometry", "bbox", "sources", "version"}
# Row-group shards (2026-10-26, first visit): a source row group holds ~20-120 k features over 10-50 km, so the cells of a
# drive along it read the same few row groups again and again (Berlin's 8 km profiler route: 87 road cells from 6 Overture
# row groups and 2 Layercake ones, each decompressed and decoded 22-86 times, most of a cell's build). A row group a cell
# needs is therefore decoded once per machine into a local Parquet file of just the columns the layer reads ("shard",
# rg<N>.<tag>.parquet beside the source file's blocks, its rows ordered by place in small row groups), which every
# later cell, in every process, filters natively (no fsspec, no GIL); a row group's feature boxes (rg<N>.bbox.npy) say
# which candidates hold a feature of the cell at all. BNGE_MD_SHARDS=0: every cell reads the source row groups itself.
SHARDS = os.environ.get("BNGE_MD_SHARDS", "1") != "0"
SHARD_VERSION = 1               # bump when a shard's layout changes (its file name changes with it)
SHARD_ROWS = 1024               # rows per row group of a shard (its bbox statistics skip the rest)
SHARD_BAND = 50.0               # rows ordered by band of 1/SHARD_BAND degree of their box centre (y, then x)
SHARD_WAIT_S = 120.0            # a shard another process is writing is waited for this long (xproc.once)
SHARD_FAILURES = 3              # shard reads that failed in a process before it reads the sources again (shard_failed)
BOX_PAD = 1e-6                  # degrees the cell is grown by for the box test (float32 boxes: never a feature missed)

# Overture's attribution (https://docs.overturemaps.org/attribution/, read 2026-10-11): the line its
# own viewer shows, and each theme's licence with the sources that ask for credit, whole (the CC BY
# sources at the end need their credit as much as the first ones; re-read it when RELEASE changes).
ATTRIBUTION = "© OpenStreetMap contributors, Overture Maps Foundation"
THEME_LICENCES = {
    "transportation": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. "
                      "Data from TomTom.",
    "buildings": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Esri "
                 "Community Maps contributors. Available under CC BY 4.0. Global ML Building Footprints. "
                 "Licensed by Microsoft under the Open Database License. Google Open Buildings. Available "
                 "under CC BY 4.0. USGS 3D Elevation Program Digital Elevation Program. Qian Shi, et al. A "
                 "First High-quality Vector Data of Buildings in East Asian Countries Based on a Comprehensive "
                 "Large-scale Mapping Framework. Zenodo, 22 July 2023, doi:10.5281/zenodo.8174931. Available "
                 "under CC BY 4.0. Work derived from BTN 2024 ign.es. Available under CC BY 4.0",
    "base": "ODbL. © OpenStreetMap contributors. Available under the Open Database License. Data from the "
            "Daylight Map Distribution. ESA WorldCover. © ESA WorldCover project 2020 / Contains modified "
            "Copernicus Sentinel data (2020) processed by ESA WorldCover consortium. Available under CC BY "
            "4.0 DEED. Data products from ETOPO1. Available under Open Data Commons Public Domain Dedication "
            "and License. Data from GLOBathy. Available under CC0 1.0 (assumed).",
    # (read 2026-10-23)
    "places": "Data from Meta. Available under CDLA Permissive 2.0. Data from Microsoft. Available under CDLA "
              "Permissive 2.0. Data from PinMeTo. Available under CDLA Permissive 2.0. Data from Krick. Available "
              "under CDLA Permissive 2.0. Data from RenderSEO. Available under CDLA Permissive 2.0. Data from DAC. "
              "Available under CDLA Permissive 2.0. Data from BrightQuery. Available under CDLA Permissive 2.0. Data "
              "from Birdeye. Available under CDLA Permissive 2.0. Data from Uberall. Available under CDLA Permissive "
              "2.0. Data from OpenTravel Alliance. Available under CDLA Permissive 2.0. Data from Foursquare. "
              "Copyright 2024 Foursquare Labs, Inc. All rights reserved. Available under Apache 2.0. Foursquare data "
              "was transformed to the Overture schema. Changed: 2026-03-18. NOTICE.txt. Data from AllThePlaces. "
              "Available under CC0 1.0.",
}

_SRC = "sources[1].dataset AS src_dataset, sources[1].record_id AS src_record"
_NAME = "names.\"primary\" AS name"
_TAGS = "CAST(to_json(source_tags) AS VARCHAR) AS tags"
LAYERS = {
    "roads": dict(theme="transportation", type="segment", clip=False,
                  cols=f"subtype, class, subclass, {_NAME}, {_SRC}",
                  text=("subtype", "class", "subclass", "name", "src_dataset", "src_record"),
                  extra="road_flags, rail_flags, speed_limits, connectors, road_surface, width_rules, level_rules, "
                        "subclass_rules, access_restrictions",
                  rules={"surface": ("road_surface", "value", "text"),
                         "width": ("width_rules", "value", "float"),
                         "level": ("level_rules", "value", "float"),
                         "subclass": ("subclass_rules", "value", "text")}),
    "connectors": dict(theme="transportation", type="connector", clip=False, cols=_SRC,
                       text=("src_dataset", "src_record")),
    "buildings": dict(theme="buildings", type="building", clip=False,
                      cols=f"subtype, class, {_NAME}, {_SRC}, height, min_height, num_floors, "
                           "num_floors_underground, min_floor, level, is_underground, has_parts, roof_shape, "
                           "roof_height, roof_direction, roof_orientation, roof_material, roof_color, "
                           "facade_material, facade_color, "
                           "COALESCE(list_filter(sources, lambda s: s.property = '/properties/height')[1].dataset, "
                           "CASE WHEN height IS NOT NULL THEN sources[1].dataset END) AS height_src",
                      text=("subtype", "class", "name", "src_dataset", "src_record", "roof_shape",
                            "roof_orientation", "roof_material", "roof_color", "facade_material",
                            "facade_color", "height_src"),
                      float=("height", "min_height", "num_floors", "num_floors_underground", "min_floor",
                             "level", "roof_height", "roof_direction"),
                      bool=("is_underground", "has_parts")),
    # parts of buildings (towers on podiums, setbacks): each its own height range; buildings.py draws a
    # building's parts in place of its footprint (has_parts)
    "building_parts": dict(theme="buildings", type="building_part", clip=False,
                           cols=f"building_id, {_SRC}, height, min_height, num_floors, min_floor, roof_shape, "
                                "roof_height, facade_color, roof_color",
                           text=("building_id", "src_dataset", "src_record", "roof_shape", "facade_color",
                                 "roof_color"),
                           float=("height", "min_height", "num_floors", "min_floor", "roof_height")),
    "water": dict(theme="base", type="water", clip=True,
                  cols=f"subtype, class, {_NAME}, {_SRC}, level, is_salt, is_intermittent, {_TAGS}",
                  text=("subtype", "class", "name", "src_dataset", "src_record", "tags"),
                  float=("level",), bool=("is_salt", "is_intermittent")),
    "land_use": dict(theme="base", type="land_use", clip=True,
                     cols=f"subtype, class, {_NAME}, {_SRC}, level, surface, elevation, {_TAGS}",
                     text=("subtype", "class", "name", "src_dataset", "src_record", "surface", "tags"),
                     float=("level", "elevation")),
    "infrastructure": dict(theme="base", type="infrastructure", clip=False,
                           cols=f"subtype, class, {_NAME}, {_SRC}, level, height, surface, {_TAGS}",
                           text=("subtype", "class", "name", "src_dataset", "src_record", "surface", "tags"),
                           float=("level", "height")),
    # points of interest; read for the gas stations (gasstations.py, the user 2026-10-23: "we can already refuel in
    # beam so I want to include it on this impressive map mod"). Schema of 2026-09-23.1: basic_category and
    # taxonomy.primary (the old categories column is gone), brand.wikidata / brand.names.primary
    "places": dict(theme="places", type="place", clip=False,
                   cols=f"basic_category, taxonomy.\"primary\" AS taxonomy, brand.wikidata AS brand_wikidata, "
                        f"brand.names.\"primary\" AS brand_name, {_NAME}, {_SRC}, operating_status, confidence",
                   text=("basic_category", "taxonomy", "brand_wikidata", "brand_name", "name", "src_dataset",
                         "src_record", "operating_status"),
                   float=("confidence",)),
}
# road_flags: one row per flag (is_bridge, is_tunnel, is_link, is_covered, ...)
# speed_limits: max speed in km/h (mph converted) per range
MPH = 1.609344


class OvertureError(IOError):
    pass


class ReleaseGone(OvertureError):
    """The pinned release's STAC index is no longer published (bump overture.RELEASE)."""


class DuckDbMissing(OvertureError):
    """duckdb / fsspec are not installed (pip install -r server/requirements.txt)."""


def _duckdb():
    try:
        import duckdb
    except ImportError as e:
        raise DuckDbMissing("map data needs duckdb (pip install -r server/requirements.txt)") from e
    if rangeread.NetfetchFS is None:
        raise DuckDbMissing("map data needs fsspec (pip install -r server/requirements.txt)")
    return duckdb


def _is_duckdb_error(e):
    try:
        import duckdb
    except ImportError:
        return False
    return isinstance(e, duckdb.Error)


def _crc(text):
    import zlib
    return zlib.crc32(text.encode())


def intersects(b, bounds):
    """STAC bbox [xmin, ymin, xmax, ymax] against (lon0, lat0, lon1, lat1), edges inclusive."""
    lon0, lat0, lon1, lat1 = bounds
    return b[0] <= lon1 and b[2] >= lon0 and b[1] <= lat1 and b[3] >= lat0


class StacIndex:
    """The release's per-file index: which parquet files of a theme/type intersect a box."""

    def __init__(self, release, raw_dir, fetcher):
        self.release, self.raw_dir, self.fetcher = release, raw_dir, fetcher
        self._coll = {}
        self._lock = threading.Lock()

    def _json(self, url, path, kind):
        try:
            with open(path, "rb") as f:
                return json.loads(f.read())
        except (OSError, ValueError):
            pass
        try:
            body = self.fetcher.get(url, kind=kind).body
        except netfetch.NotFound as e:
            raise ReleaseGone(f"Overture release {self.release} is not published any more ({url}); "
                              "bump overture.RELEASE (mapdata.py --latest)") from e
        data = json.loads(body)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "wb") as f:
            f.write(body)
        staging.replace_cached(tmp, path)
        return data

    def _dir(self, theme, typ):
        return os.path.join(self.raw_dir, "overture", self.release, "stac", theme, typ)

    def collection(self, theme, typ, kind="demand"):
        key = (theme, typ)
        with self._lock:
            c = self._coll.get(key)
        if c is None:
            raw = self._json(f"{STAC_ROOT}/{self.release}/{theme}/{typ}/collection.json",
                             os.path.join(self._dir(theme, typ), "collection.json"), kind)
            items = [link["href"] for link in raw["links"] if link.get("rel") == "item"]
            bboxes = raw["extent"]["spatial"]["bbox"][1:]
            if len(items) != len(bboxes):
                raise OvertureError(f"STAC {theme}/{typ}: {len(items)} items but {len(bboxes)} file boxes")
            c = {"items": items, "bboxes": bboxes, "license": raw.get("license")}
            with self._lock:
                self._coll[key] = c
        return c

    def files(self, theme, typ, bounds, kind="demand"):
        """[(name, [aws url, azure url], size)] of the files whose bbox intersects `bounds`, in
        index order."""
        c = self.collection(theme, typ, kind)
        out = []
        for i, (href, bb) in enumerate(zip(c["items"], c["bboxes"])):
            if not intersects(bb, bounds):
                continue
            name = href.rstrip("/").rsplit("/", 1)[-1]
            item = self._json(href, os.path.join(self._dir(theme, typ), "items", name), kind)
            a = item["assets"]
            urls = [a["aws"]["href"]] + ([a["azure"]["href"]] if "azure" in a else [])
            out.append((urls[0].rsplit("/", 1)[-1], urls, int(a["aws"]["file:size"])))
        return out


META_FILES = 2048        # files whose parsed Parquet metadata Reader keeps (a few hundred row groups each)
BOX_BYTES = 64 * 1024 * 1024    # row groups' feature boxes kept in memory per process (Reader.boxes)


def _lit(text):
    """text as a SQL string literal (a cache path may hold a quote, a user named O'Neil)."""
    return "'" + str(text).replace("'", "''") + "'"


class RowGroup:
    """One row group of a source file: key (the caller's), remote (its RemoteFile), uri (as the connection's filesystem
    reads it, quoted), path (the files key of Reader._fetch), rg (its index), first row and n rows, cols ({top-level
    column: [byte ranges]})."""
    __slots__ = ("key", "remote", "uri", "path", "rg", "first", "n", "cols")

    def __init__(self, key, remote, uri, path, rg, first, n, cols):
        self.key, self.remote, self.uri, self.path, self.rg = key, remote, uri, path, rg
        self.first, self.n, self.cols = first, n, cols


def box_hit(boxes, bounds):
    """Whether any feature box (n, 4: xmin, ymin, xmax, ymax) touches bounds (grown by BOX_PAD); None: unknown, True."""
    if boxes is None:
        return True
    lon0, lat0, lon1, lat1 = bounds
    return bool(np.any((boxes[:, 0] <= lon1 + BOX_PAD) & (boxes[:, 2] >= lon0 - BOX_PAD)
                       & (boxes[:, 1] <= lat1 + BOX_PAD) & (boxes[:, 3] >= lat0 - BOX_PAD)))


def parse_metadata(meta):
    """{path: {rg: {"first", "n", "bbox": {leaf: (min, max)}, "cols": {top: [range]}}}} from parquet_metadata rows
    (file_name, row_group_id, row_group_num_rows, path_in_schema, stats_min, stats_max, dictionary_page_offset,
    data_page_offset, total_compressed_size), ordered by file, row group, column."""
    parsed, next_row = {}, {}
    for fname, rg, nrows, col, smin, smax, dict_off, data_off, size in meta:
        path = fname.split("://", 1)[-1]
        pg = parsed.setdefault(path, {})
        g = pg.get(rg)
        if g is None:
            first = next_row.get(path, 0)
            next_row[path] = first + nrows
            g = pg[rg] = {"first": first, "n": nrows, "bbox": {}, "cols": {}}
        offs = [o for o in (dict_off, data_off) if o]
        if not offs:
            continue
        top, leaf = col.split(", ")[0], col.split(", ")[-1]
        g["cols"].setdefault(top, []).append((min(offs), min(offs) + size))
        if top == "bbox":
            g["bbox"][leaf] = (smin, smax)
    return parsed


def save_metadata(remote, groups):
    """A file's parsed row groups (parse_metadata) kept beside its blocks: every server process parsed the same
    footers again (0.2-0.6 s a file, ~10 processes; a release file never changes, nor a pinned version)."""
    path = os.path.join(remote.cache_dir, "rgmeta.json")
    try:
        os.makedirs(remote.cache_dir, exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        with open(tmp, "w") as f:
            json.dump({"size": remote.size, "groups": {str(rg): g for rg, g in groups.items()}}, f)
        staging.replace_cached(tmp, path)
    except OSError as e:
        log.debug("mapdata: %s: %s", path, e)


def load_metadata(remote):
    """The row groups save_metadata kept for this file, None when there are none (or of another size)."""
    try:
        with open(os.path.join(remote.cache_dir, "rgmeta.json")) as f:
            d = json.load(f)
        if d.get("size") != remote.size:
            return None
        return {int(rg): {"first": g["first"], "n": g["n"], "bbox": {k: tuple(v) for k, v in g["bbox"].items()},
                          "cols": {k: [tuple(r) for r in v] for k, v in g["cols"].items()}}
                for rg, g in d["groups"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class LazyConnection:
    """Reader._connect's connection for files and kind, opened by its first statement: a cell whose metadata, boxes
    and shards are all there already needs none (each one is a DuckDB database of its own, 10-20 ms to open)."""

    def __init__(self, reader, files, kind):
        self.reader, self.files, self.kind = reader, files, kind
        self.proto = rangeread.PROTOCOLS[kind]
        self.errors = []
        self._con = None

    def execute(self, sql):
        if self._con is None:
            self._con, _, _ = self.reader._connect(self.files, self.kind, self.errors)
        return self._con.execute(sql)

    def close(self):
        if self._con is not None:
            self._con.close()
            self._con = None


class Reader:
    """Overture layers per cell (see the module docstring). threads: DuckDB threads per query."""

    def __init__(self, raw_dir, fetcher, release=RELEASE, threads=DUCKDB_THREADS):
        self.raw_dir, self.fetcher, self.release, self.threads = raw_dir, fetcher, release, threads
        self.index = StacIndex(release, raw_dir, fetcher)
        self._remotes = OrderedDict()
        self._lock = threading.Lock()
        self.pool = ThreadPoolExecutor(FETCH_THREADS, thread_name_prefix="mapdata-fetch")
        # path -> [(row group, {"first", "n", "bbox", "cols"})]: a file's parsed Parquet metadata (_plan; a release's
        # files never change). Re-reading it for every cell was most of a map cell's CPU (the Deck's mapdata-layer
        # thread, 36 CPU-s of a teleport, 2026-10-23); osmtags._groups keeps its file's the same way.
        self._meta = OrderedDict()
        self._boxes = OrderedDict()         # box file -> (n, 4) array (boxes())
        self._boxes_bytes = 0
        self._local = threading.local()     # each thread's DuckDB connection for the shards (local_con)
        self._shard_locks = {}
        self.shards_ok = SHARDS             # False after SHARD_FAILURES shard reads failed (then the sources are read)
        self._shard_failures = 0
        self._footer_ratio = {}         # theme/type -> the largest footer / file size seen (_plan's footer guess)
        self._footer_len = {}           # path -> its footer's length (+ 8)

    def close(self):
        self.pool.shutdown(wait=False)

    def _remote(self, theme, typ, name, urls, size):
        key = (theme, typ, name)
        with self._lock:
            r = self._remotes.get(key)
            if r is None:
                r = rangeread.RemoteFile(urls, size, os.path.join(self.raw_dir, "overture", self.release, theme,
                                                                  typ, name), self.fetcher)
                self._remotes[key] = r
                while len(self._remotes) > REMOTE_FILES:
                    self._remotes.popitem(last=False)
            self._remotes.move_to_end(key)
            return r

    def _connect(self, files, kind, errors=None):
        """A DuckDB connection whose filesystem serves `files` ({path: RemoteFile}) with `kind`; errors: the list the
        fetch errors DuckDB swallows are put in."""
        duckdb = _duckdb()
        errors = [] if errors is None else errors

        def resolve(path):
            return files.get(path)

        fs = rangeread.NetfetchFS(resolve, kind)
        orig = fs._open

        def _open(path, mode="rb", **kw):
            f = orig(path, mode, **kw)
            fetch = f._fetch_range

            def guarded(start, end):
                try:
                    return fetch(start, end)
                except Exception as e:      # DuckDB turns it into a generic error: keep the real one
                    errors.append(e)
                    raise
            f._fetch_range = guarded
            return f
        fs._open = _open
        con = duckdb.connect(config={"threads": self.threads, "autoinstall_known_extensions": False,
                                     "autoload_known_extensions": False})
        for k, v in PARQUET_CONFIG.items():
            con.execute(f"SET {k} = {str(v).lower()}")
        global RAW_WKB
        if RAW_WKB:
            try:
                con.execute("SET enable_geoparquet_conversion = false")
            except Exception:       # (a DuckDB without the setting: ST_AsWKB as before)
                RAW_WKB = False
        con.register_filesystem(fs)
        return con, fs.protocol, errors

    def local_con(self):
        """This thread's DuckDB connection for local files (the shards): no filesystem of ours, kept for the thread's
        life (a connection is a database of its own: 10-15 ms to open, every cell opened one or two)."""
        con = getattr(self._local, "con", None)
        if con is None:
            duckdb = _duckdb()
            con = duckdb.connect(config={"threads": self.threads, "autoinstall_known_extensions": False,
                                         "autoload_known_extensions": False})
            con.execute("SET enable_geoparquet_conversion = false")
            self._local.con = con
        return con

    def _real_error(self, e, errors):
        """The error to raise for e: the fetch error DuckDB swallowed, else e (Missing as ReleaseGone)."""
        real = next((x for x in errors if isinstance(x, (netfetch.FetchError, rangeread.RangeError))), None)
        if real is None or isinstance(e, (netfetch.FetchError, rangeread.RangeError)):
            real = e
        if isinstance(real, rangeread.Missing):
            # a file the (cached) STAC index lists answers 404: Overture removed the release
            return ReleaseGone(f"Overture release {self.release}: {real}; bump overture.RELEASE "
                               "(mapdata.py --latest)")
        return real

    def query(self, layer, cell, kind="demand"):
        """The layer's Features of one cell (lon/lat only; mapdata localises and marks `own`), and
        {(dataset, license): count} of the sources of its features."""
        spec = LAYERS[layer]
        bounds = cell.bounds()
        t0 = time.perf_counter()
        hits = self.index.files(spec["theme"], spec["type"], bounds, kind)
        t1 = time.perf_counter()
        if not hits:
            return empty(layer), {}
        files = {f"{spec['theme']}/{spec['type']}/{name}": self._remote(spec["theme"], spec["type"], name, urls, size)
                 for name, urls, size in hits}
        if self.shards_ok:
            con = LazyConnection(self, files, kind)
            try:
                local = self._shard_plan(con, con.proto, sorted(files), files, spec, bounds, kind)
            except Exception as e:
                real = self._real_error(e, con.errors)
                if real is not e:
                    raise real from e
                raise
            finally:
                con.close()
            if not local:
                return empty(layer), {}
            t2 = time.perf_counter()
            try:
                out = self._run_local(layer, spec, bounds, local)
                log.debug("mapdata %s %s: index %.2f s, shards %.2f s (%d), query %.2f s", layer, cell, t1 - t0,
                          t2 - t1, len(local), time.perf_counter() - t2)
                return out
            except Exception as e:
                if not _is_duckdb_error(e):
                    raise
                self.shard_failed(local, e)
        con, proto, errors = self._connect(files, kind)
        try:
            paths = sorted(files)
            rows = self._plan(con, proto, paths, files, spec, bounds, kind)
            if not rows:
                return empty(layer), {}         # no row of these files lies in the cell
            t2 = time.perf_counter()
            src = ("read_parquet([" + ", ".join(f"'{proto}://{p}'" for p in paths) + "], filename = true, "
                   "file_row_number = true)")
            # only the row groups the plan fetched (DuckDB would otherwise read every row group whose
            # statistics it cannot rule out, lazily, one request each)
            only = " OR ".join(f"(filename = '{u}' AND file_row_number BETWEEN {a} AND {b})" for u, a, b in rows)
            out = self._run(con, src, only, "geometry" if RAW_WKB else "ST_AsWKB(geometry)", layer, spec, bounds)
            log.debug("mapdata %s %s: index %.2f s, plan %.2f s, query %.2f s", layer, cell, t1 - t0, t2 - t1,
                      time.perf_counter() - t2)
            return out
        except Exception as e:
            real = self._real_error(e, errors)
            if real is not e:
                raise real from e
            raise
        finally:
            con.close()

    def shard_failed(self, local, e):
        """A shard query failed in DuckDB (a damaged file, one the cache cap evicted under it, a path DuckDB cannot
        open): the shards are removed (written again by the next cell) and this cell reads the sources; after
        SHARD_FAILURES of them this process reads the sources itself from now on."""
        with self._lock:
            self._shard_failures += 1
            off = self._shard_failures >= SHARD_FAILURES
            if off:
                self.shards_ok = False
        log.warning("mapdata: reading the local row-group shards failed (%s: %s); reading the sources%s",
                    type(e).__name__, e, " from now on" if off else " for this cell")
        for p in local:
            try:
                os.remove(p)
            except OSError:
                pass

    def _run_local(self, layer, spec, bounds, local):
        con = self.local_con()
        try:
            return self._run(con, "read_parquet([" + ", ".join(_lit(p) for p in local) + "])", None, "geometry",
                             layer, spec, bounds)
        finally:
            for t in ("t", "s"):
                try:
                    con.execute(f"DROP TABLE IF EXISTS {t}")
                except Exception:   # (the connection is unusable: a new one next time)
                    self._local.con = None

    def fetch_only(self, layer, cell, kind="prefetch"):
        """Only the network part of query(): the bytes it will read (footers, bbox columns, the columns of the row
        groups that hold the cell's rows) into the block cache, no rows read, nothing decoded or stored. The map data
        ahead of the player on a machine that builds none ahead (mapdata.WARM_THREADS 0: 8 CPUs or fewer) waited on
        its downloads (Oregon's buckets: 3-4 round trips a new file) in the block's own build (the I/O audit,
        2026-10-26). Raises what the fetches raised (netfetch.Deferred for a refused prefetch)."""
        spec = LAYERS[layer]
        bounds = cell.bounds()
        hits = self.index.files(spec["theme"], spec["type"], bounds, kind)
        if not hits:
            return
        files = {f"{spec['theme']}/{spec['type']}/{name}": self._remote(spec["theme"], spec["type"], name, urls, size)
                 for name, urls, size in hits}
        if self.shards_ok:          # (the row groups' boxes, kept: no per-cell bbox query, and none for a shard)
            con = LazyConnection(self, files, kind)
            try:
                self._shard_plan(con, con.proto, sorted(files), files, spec, bounds, kind, write=False)
            except Exception as e:
                real = self._real_error(e, con.errors)
                if real is not e:
                    raise real from e
                raise
            finally:
                con.close()
            return
        con, proto, errors = self._connect(files, kind)
        try:
            self._plan(con, proto, sorted(files), files, spec, bounds, kind)
        except Exception as e:
            real = next((x for x in errors if isinstance(x, (netfetch.FetchError, rangeread.RangeError))), None)
            if real is not None and real is not e:
                raise real from e
            raise
        finally:
            con.close()

    # --- fetch planning ------------------------------------------------------------------------
    def _parallel(self, tasks):
        """Run callables on the fetch pool; the first error is raised once all have finished."""
        futs = [self.pool.submit(t) for t in tasks]
        err = None
        for f in futs:
            try:
                f.result()
            except Exception as e:      # re-raised below, after the others finished
                err = err or e
        if err is not None:
            raise err

    @staticmethod
    def _ranges(chunks):
        """Byte ranges [start, end) merged where they touch or lie within MERGE_GAP."""
        out = []
        for a, b in sorted(chunks):
            if out and a - out[-1][1] <= MERGE_GAP:
                out[-1][1] = max(out[-1][1], b)
            else:
                out.append([a, b])
        return out

    def _fetch(self, files, wanted, kind):
        """Fetch {path: [(start, end)]} in parallel (one task per merged range) into the block cache."""
        tasks = []
        for path, chunks in sorted(wanted.items()):
            r = files[path]
            for a, b in self._ranges(chunks):
                tasks.append(lambda r=r, a=a, b=b: r.blocks(a // rangeread.BLOCK, (b - 1) // rangeread.BLOCK + 1,
                                                            kind))
        self._parallel(tasks)

    def _groups(self, con, proto, paths, files, kind):
        """{(path, rg): {"first": row, "n", "bbox": {leaf: (min, max)}, "cols": {top: [range]}}} of the files: from
        memory, from the copy beside the file's blocks (save_metadata), or read from the footers."""
        with self._lock:
            new = [p for p in paths if p not in self._meta]
        disk = {}
        for p in new:
            got = load_metadata(files[p])
            if got is not None:
                disk[p] = got
        new = [p for p in new if p not in disk]
        parsed = {}
        if new:
            def footer(path, r):            # the last 8 bytes: footer length + "PAR1"
                kind_dir = path.rsplit("/", 1)[0]
                guess = FOOTER_GUESS
                if FOOTER_LEARN:
                    with self._lock:
                        known, ratio = self._footer_len.get(path), self._footer_ratio.get(kind_dir)
                    if known is not None:       # (its exact footer: a learnt guess never reads more of a file it read)
                        guess = known
                    elif ratio is not None:
                        guess = int(min(FOOTER_MAX, max(FOOTER_MIN, ratio * FOOTER_PAD * r.size)))
                tail = r.read(max(0, r.size - guess), r.size, kind)
                n = int.from_bytes(tail[-8:-4], "little")
                if FOOTER_LEARN and r.size:
                    with self._lock:
                        self._footer_ratio[kind_dir] = max(self._footer_ratio.get(kind_dir, 0.0), (n + 8) / r.size)
                        self._footer_len[path] = n + 8
                if n + 8 > len(tail):
                    r.read(r.size - n - 8, r.size, kind)
            self._parallel([lambda p=p: footer(p, files[p]) for p in new])
            meta = con.execute(f"""SELECT file_name, row_group_id, row_group_num_rows, path_in_schema, stats_min,
                stats_max, dictionary_page_offset, data_page_offset, total_compressed_size
                FROM parquet_metadata([{", ".join(f"'{proto}://{p}'" for p in new)}])
                ORDER BY file_name, row_group_id, column_id""").fetchall()
            parsed = parse_metadata(meta)
            for p in new:
                save_metadata(files[p], parsed.get(p, {}))
        if disk or parsed:
            with self._lock:
                for p in new:
                    self._meta[p] = parsed.get(p, {})
                for p, g in disk.items():
                    self._meta[p] = g
                while len(self._meta) > META_FILES:
                    self._meta.popitem(last=False)
        with self._lock:
            groups = {}
            for p in paths:
                pg = self._meta.get(p)
                if pg is None:          # (evicted meanwhile by another thread's files: read again next time)
                    continue
                self._meta.move_to_end(p)
                for rg, g in pg.items():
                    groups[(p, rg)] = g
        return groups

    @staticmethod
    def _candidates(groups, bounds):
        """The keys of the row groups whose bbox statistics touch bounds."""
        lon0, lat0, lon1, lat1 = bounds

        def stat(g, leaf, i, default):
            try:
                return float(g["bbox"][leaf][i])
            except (KeyError, TypeError, ValueError):
                return default
        return [k for k, g in sorted(groups.items())
                if stat(g, "xmin", 0, -1e9) <= lon1 and stat(g, "xmax", 1, 1e9) >= lon0
                and stat(g, "ymin", 0, -1e9) <= lat1 and stat(g, "ymax", 1, 1e9) >= lat0]

    @staticmethod
    def _used(spec):
        return set(re.findall(r"[A-Za-z_]+", spec["cols"] + " " + spec.get("extra", ""))) | BASE_COLUMNS

    def _shard_plan(self, con, proto, paths, files, spec, bounds, kind, write=True):
        """The local shards of the row groups that hold a feature of the cell (built here when missing), [] when none
        does. write=False: only the bytes of the missing ones fetched (fetch_only)."""
        groups = self._groups(con, proto, paths, files, kind)
        rgs = [RowGroup((p, rg), files[p], f"'{proto}://{p}'", p, rg, groups[(p, rg)]["first"], groups[(p, rg)]["n"],
                        groups[(p, rg)]["cols"]) for p, rg in self._candidates(groups, bounds)]
        if not rgs:
            return []
        boxes = self.boxes(con, files, rgs, kind)
        rgs = [g for g in rgs if box_hit(boxes.get(g.key), bounds)]
        if not rgs:
            return []
        present = {c for g in rgs for c in g.cols}
        shards = self.shards(con, files, rgs, sorted(self._used(spec) & present), kind, write=write)
        return [shards[g.key] for g in rgs if g.key in shards]

    # --- row-group shards (SHARDS) --------------------------------------------------------------
    @staticmethod
    def _rg_dir(g):
        return os.path.join(g.remote.cache_dir, "rg")

    def _keep_boxes(self, path, b):
        with self._lock:
            old = self._boxes.pop(path, None)
            if old is not None:
                self._boxes_bytes -= old.nbytes
            self._boxes[path] = b
            self._boxes_bytes += b.nbytes
            while self._boxes_bytes > BOX_BYTES and len(self._boxes) > 1:
                self._boxes_bytes -= self._boxes.popitem(last=False)[1].nbytes

    def boxes(self, con, files, rgs, kind):
        """{key: (n, 4) xmin, ymin, xmax, ymax} of every feature of the row groups (RowGroup list), in the source's
        own precision: kept in memory and beside the file's blocks (rg<N>.bbox.npy); the missing ones read from their
        bbox columns in one query per file. A row group whose boxes cannot be told is left out (box_hit: True)."""
        out, todo = {}, []
        for g in rgs:
            path = os.path.join(self._rg_dir(g), f"rg{g.rg}.bbox.npy")
            with self._lock:
                b = self._boxes.get(path)
                if b is not None:
                    self._boxes.move_to_end(path)
            if b is None:
                try:
                    b = np.load(path, allow_pickle=False)
                except (OSError, ValueError, EOFError):
                    b = None
                if b is not None and b.shape != (g.n, 4):
                    b = None
                if b is not None:
                    self._keep_boxes(path, b)
            if b is None:
                todo.append((g, path))
            else:
                out[g.key] = b
        if not todo:
            return out
        want = {}
        for g, _ in todo:
            want.setdefault(g.path, []).extend(g.cols.get("bbox", []))
        self._fetch(files, want, kind)
        by_uri = {}
        for g, path in todo:
            by_uri.setdefault(g.uri, []).append((g, path))
        for uri, gs in by_uri.items():
            only = " OR ".join(f"file_row_number BETWEEN {g.first} AND {g.first + g.n - 1}" for g, _ in gs)
            got = con.execute(f"""SELECT file_row_number AS r, bbox.xmin AS x0, bbox.ymin AS y0, bbox.xmax AS x1,
                bbox.ymax AS y1 FROM read_parquet({uri}, file_row_number = true) WHERE {only}
                ORDER BY file_row_number""").fetchnumpy()
            rows = np.asarray(got["r"])
            cols = [np.ma.filled(got[c], np.nan) if np.ma.isMaskedArray(got[c]) else np.asarray(got[c])
                    for c in ("x0", "y0", "x1", "y1")]
            arr = np.stack(cols, axis=1)
            for g, path in gs:
                i0, i1 = np.searchsorted(rows, [g.first, g.first + g.n])
                b = np.ascontiguousarray(arr[i0:i1])
                if b.shape != (g.n, 4):         # (not the whole row group: unknown, every cell takes it)
                    continue
                out[g.key] = b
                self._keep_boxes(path, b)
                try:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp.npy"
                    np.save(tmp, b, allow_pickle=False)
                    staging.replace_cached(tmp, path)
                except OSError as e:
                    log.debug("mapdata: %s: %s", path, e)
        return out

    def _shard_lock(self, path):
        with self._lock:
            lk = self._shard_locks.get(path)
            if lk is None:
                if len(self._shard_locks) > 4096:
                    self._shard_locks.clear()
                lk = self._shard_locks[path] = threading.Lock()
            return lk

    def shards(self, con, files, rgs, columns, kind, src_row=False, write=True):
        """{key: local shard path} of the row groups (RowGroup list), with these top-level columns (and the source's
        file_row_number as src_row): the missing ones fetched (in parallel) and written here, once per machine
        (xproc: another process writing one is waited for). write=False: the missing ones only fetched (and left
        out)."""
        tag = "%d-%08x" % (SHARD_VERSION, _crc(json.dumps([sorted(columns), bool(src_row)])))
        out, todo = {}, []
        for g in rgs:
            path = os.path.join(self._rg_dir(g), f"rg{g.rg}.{tag}.parquet")
            if os.path.exists(path):
                out[g.key] = path
            else:
                todo.append((g, path))
        if not todo:
            return out
        want = {}
        for g, _ in todo:
            for c in columns:
                want.setdefault(g.path, []).extend(g.cols.get(c, []))
        self._fetch(files, want, kind)
        if not write:
            return out
        con.execute("SET enable_geoparquet_conversion = false")    # (the geometry as its WKB bytes)
        for g, path in todo:
            with self._shard_lock(path):
                got, mine = xproc.once(path + ".lock", lambda p=path: p if os.path.exists(p) else None,
                                       SHARD_WAIT_S, kind)
                if got is None:
                    try:
                        self._write_shard(con, g, path, columns, src_row)
                    finally:
                        if mine:
                            xproc.release(path + ".lock")
            out[g.key] = path
        return out

    @staticmethod
    def _write_shard(con, g, path, columns, src_row):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
        sel = ", ".join(f'"{c}"' for c in columns) + (", file_row_number AS src_row" if src_row else "")
        order = "file_row_number"
        if "bbox" in columns:           # (by place: the shard's small row groups each cover a small area)
            order = (f"floor((bbox.ymin + bbox.ymax) * {SHARD_BAND / 2}), "
                     f"floor((bbox.xmin + bbox.xmax) * {SHARD_BAND / 2}), file_row_number")
        con.execute(f"""COPY (SELECT {sel} FROM read_parquet({g.uri}, file_row_number = true)
            WHERE file_row_number BETWEEN {g.first} AND {g.first + g.n - 1} ORDER BY {order})
            TO {_lit(tmp)} (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {SHARD_ROWS})""")
        staging.replace_cached(tmp, path)

    def _plan(self, con, proto, paths, files, spec, bounds, kind):
        """Fetch, in parallel and before the real query, what it will read: the footers, then the
        bbox columns of the row groups whose statistics touch the cell, then every column the query
        uses of the row groups that hold a matching row; False when no row matches. DuckDB (its own sequential prefetching
        off) then reads from the block cache. A row group whose bbox statistics touch the cell but
        that holds no matching row costs only its bbox columns: in sparse areas row groups span
        hundreds of km (a desert cell's roads and water: 16 MB instead of 27 MB, 4 s instead of
        13 s with DuckDB reading lazily on its own). (Without SHARDS.)"""
        lon0, lat0, lon1, lat1 = bounds
        t0 = time.perf_counter()
        uris = ", ".join(f"'{proto}://{p}'" for p in paths)
        groups = self._groups(con, proto, paths, files, kind)
        cand = self._candidates(groups, bounds)
        t1 = time.perf_counter()
        if not cand:
            return []
        wanted = {}
        for path, rg in cand:
            wanted.setdefault(path, []).extend(groups[(path, rg)]["cols"].get("bbox", []))
        self._fetch(files, wanted, kind)
        bbox = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND "
                f"bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}")
        hits = con.execute(f"""SELECT DISTINCT filename, file_row_number FROM read_parquet([{uris}],
            filename = true, file_row_number = true) WHERE {bbox}""").fetchall()
        firsts = {}
        for (path, rg), g in sorted(groups.items()):
            firsts.setdefault(path, []).append((g["first"], rg))
        rows_of = {}
        for fname, row in hits:
            rows_of.setdefault(fname, []).append(row)
        matched = set()
        for fname, rows in rows_of.items():     # (each file's rows at once: one search per hit was ~0.1 s a cell)
            path = fname.split("://", 1)[-1]
            rgs = firsts[path]
            ks = np.searchsorted(np.array([f for f, _ in rgs]), np.asarray(rows), side="right") - 1
            matched.update((path, rgs[int(k)][1]) for k in np.unique(ks))
        used = self._used(spec)
        wanted = {}
        for path, rg in sorted(matched):
            for top, chunks in groups[(path, rg)]["cols"].items():
                if top in used:
                    wanted.setdefault(path, []).extend(chunks)
        t2 = time.perf_counter()
        self._fetch(files, wanted, kind)
        log.debug("mapdata plan %s/%s: %d files, %d row groups, %d candidates, %d matched; footers %.2f s, "
                  "bbox %.2f s, data %.2f s (%.1f MB)", spec["theme"], spec["type"], len(paths), len(groups),
                  len(cand), len(matched), t1 - t0, t2 - t1, time.perf_counter() - t2,
                  sum(b - a for c in wanted.values() for a, b in self._ranges(c)) / 1e6)
        rows = []
        for path, rg in sorted(matched):
            g = groups[(path, rg)]
            rows.append((f"{proto}://{path}", g["first"], g["first"] + g["n"] - 1))
        return rows

    def _run(self, con, src, only, wkb, layer, spec, bounds):
        """The layer's (Features, sources) from src (a read_parquet(...) of the rows; only: a further WHERE on them,
        or None; wkb: the geometry's WKB bytes as SQL)."""
        lon0, lat0, lon1, lat1 = bounds
        bbox = (f"bbox.xmin <= {lon1!r} AND bbox.xmax >= {lon0!r} AND "
                f"bbox.ymin <= {lat1!r} AND bbox.ymax >= {lat0!r}")
        where = f"({only}) AND {bbox}" if only else bbox
        extra = f", {spec['extra']}" if spec.get("extra") else ""
        # The scan first, the dedup (by id, highest version) and numbering from the copy: with the
        # window in the same statement DuckDB 1.5 stopped skipping row groups by their statistics and
        # read every row group of the file between the matches (12 MB more, 9 s, for a city cell).
        con.execute(f"""CREATE OR REPLACE TEMP TABLE s AS
              SELECT id, version, {spec['cols']}, {wkb} AS wkb,
                     (bbox.xmin + bbox.xmax) / 2 AS cx, (bbox.ymin + bbox.ymax) / 2 AS cy,
                     sources{extra}
              FROM {src} WHERE {where}""")
        con.execute("""CREATE OR REPLACE TEMP TABLE t AS
            SELECT row_number() OVER (ORDER BY id) - 1 AS fi, * FROM (
              SELECT * EXCLUDE (version) FROM s
              QUALIFY row_number() OVER (PARTITION BY id ORDER BY version DESC, wkb) = 1)""")
        names = ["id", "wkb", "cx", "cy"] + [c for k in ("text", "float", "bool") for c in spec.get(k, ())]
        rows = con.execute(f"SELECT {', '.join(names)} FROM t ORDER BY fi").fetchall()
        at = {c: i for i, c in enumerate(names)}
        ids = mg.text_column([r[at["id"]] for r in rows])
        geoms = [mg.parse_wkb(r[at["wkb"]]) for r in rows]
        clipped = np.zeros(len(rows), bool)
        if spec["clip"]:
            keep = []
            for i, (kind, parts) in enumerate(geoms):
                if kind == mg.POLYGON:
                    parts, changed = mg.clip_polygon_parts(parts, bounds)
                    geoms[i] = (kind, parts)
                    clipped[i] = changed
                keep.append(bool(parts))
            keep = np.array(keep, bool)
        else:
            keep = np.ones(len(rows), bool)
        feats = mg.build(layer, ids[keep], [g for g, k in zip(geoms, keep) if k])
        feats.clipped = clipped[keep]
        fmap = np.cumsum(keep) - 1          # old feature index -> new (rows dropped by clipping: unused)
        sel = [r for r, k in zip(rows, keep) if k]
        feats.attrs["cx"] = np.array([r[at["cx"]] for r in sel], np.float64)
        feats.attrs["cy"] = np.array([r[at["cy"]] for r in sel], np.float64)
        for c in spec.get("text", ()):
            feats.attrs[c] = mg.text_column([r[at[c]] for r in sel])
        for c in spec.get("float", ()):
            feats.attrs[c] = mg.float_column([r[at[c]] for r in sel])
        for c in spec.get("bool", ()):
            feats.attrs[c] = mg.bool_column([r[at[c]] for r in sel])
        if layer == "roads":
            self._road_rules(con, feats, keep, fmap)
        sources = {}
        for ds, lic, n in con.execute("""SELECT s.dataset, s.license, count(*) FROM
                (SELECT unnest(sources) AS s FROM t) GROUP BY ALL ORDER BY 1, 2""").fetchall():
            sources[(ds or "", lic or "")] = int(n)
        return feats, sources

    @staticmethod
    def _road_rules(con, feats, keep, fmap):
        def ranged(column, field_, kind):
            rows = con.execute(f"""SELECT fi, r.{field_}, r."between" FROM
                (SELECT fi, unnest({column}) AS r, generate_subscripts({column}, 1) AS k FROM t)
                ORDER BY fi, k""").fetchall()
            return mg.rule_table([(int(fmap[fi]), v, b) for fi, v, b in rows if keep[fi]], kind)
        for name, (column, field_, kind) in LAYERS["roads"]["rules"].items():
            feats.rules[name] = ranged(column, field_, kind)
        # (a rail segment's is_bridge / is_tunnel are in rail_flags: railways.py's bridges)
        rows = con.execute("""SELECT fi, f, r."between" FROM
            (SELECT fi, r, unnest(r."values") AS f, generate_subscripts(r."values", 1) AS j, k FROM
              (SELECT fi, unnest(road_flags) AS r, generate_subscripts(road_flags, 1) AS k FROM t
               UNION ALL
               SELECT fi, unnest(rail_flags) AS r, 1000 + generate_subscripts(rail_flags, 1) AS k FROM t))
            ORDER BY fi, k, j""").fetchall()
        feats.rules["flags"] = mg.rule_table([(int(fmap[fi]), f, b) for fi, f, b in rows if keep[fi]], "text")
        rows = con.execute("""SELECT fi, r.max_speed."value", r.max_speed.unit, r."between", r."when" IS NULL FROM
            (SELECT fi, unnest(speed_limits) AS r, generate_subscripts(speed_limits, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        feats.rules["max_speed_kmh"] = mg.rule_table(
            [(int(fmap[fi]), v * (MPH if (u or "").lower() == "mph" else 1.0), b)
             for fi, v, u, b, uncond in rows if keep[fi] and v is not None and uncond], "float")
        # one-way: motor traffic denied in one heading (no time, mode or vehicle conditions):
        # +1 = one-way along the geometry (backward denied), -1 = against it
        rows = con.execute("""SELECT fi, r.access_type, r."when".heading, r."when"."mode", r."when".during,
                r."when"."using", r."when".recognized, r."when".vehicle, r."between" FROM
            (SELECT fi, unnest(access_restrictions) AS r, generate_subscripts(access_restrictions, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        ow = []
        for fi, typ, heading, mode, during, using, recognized, vehicle, b in rows:
            if not keep[fi] or typ != "denied" or heading not in ("forward", "backward"):
                continue
            if during or using or recognized or vehicle:
                continue
            if mode and not any(m in ("motor_vehicle", "vehicle", "car") for m in mode):
                continue
            ow.append((int(fmap[fi]), 1.0 if heading == "backward" else -1.0, b))
        feats.rules["oneway"] = mg.rule_table(ow, "float")
        rows = con.execute("""SELECT fi, c.connector_id, c."at" FROM
            (SELECT fi, unnest(connectors) AS c, generate_subscripts(connectors, 1) AS k FROM t)
            ORDER BY fi, k""").fetchall()
        feats.rules["connectors"] = mg.rule_table(
            [(int(fmap[fi]), cid, [a, a]) for fi, cid, a in rows if keep[fi]], "text")


def empty(layer):
    """A layer without features, with the layer's columns and rule tables (same schema as a full one)."""
    spec = LAYERS[layer]
    f = mg.empty(layer)
    f.clipped = np.zeros(0, bool)
    f.attrs["cx"] = np.zeros(0, np.float64)
    f.attrs["cy"] = np.zeros(0, np.float64)
    for c in spec.get("text", ()):
        f.attrs[c] = mg.text_column([])
    for c in spec.get("float", ()):
        f.attrs[c] = mg.float_column([])
    for c in spec.get("bool", ()):
        f.attrs[c] = mg.bool_column([])
    if layer == "roads":
        for name, (_, _, kind) in spec["rules"].items():
            f.rules[name] = mg.rule_table([], kind)
        f.rules["flags"] = mg.rule_table([], "text")
        f.rules["max_speed_kmh"] = mg.rule_table([], "float")
        f.rules["connectors"] = mg.rule_table([], "text")
        f.rules["oneway"] = mg.rule_table([], "float")
    return f


def latest_release(fetcher, kind="demand"):
    """The newest release id in the STAC catalog (for the update path)."""
    return json.loads(fetcher.get(f"{STAC_ROOT}/catalog.json", kind=kind).body)["latest"]


def sources_provenance(theme, release, sources):
    """The provenance record of one theme's features in a cell."""
    return {"id": f"overture:{theme}", "name": "Overture Maps Foundation", "release": release,
            "attribution": ATTRIBUTION, "licence": THEME_LICENCES.get(theme, "ODbL"),
            "url": "https://docs.overturemaps.org/attribution/",
            "datasets": [{"dataset": d, "license": l, "features": n} for (d, l), n in sorted(sources.items())]}
