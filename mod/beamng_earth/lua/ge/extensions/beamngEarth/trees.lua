-- Trees, bushes and rocks from the server's vegetation tiles (server/vegetation.py, --trees on; the user,
-- 2026-10-13: "trees, rocks and other decoration"). Each tile file lists items
-- {group, x, y, z, yaw, scale, height, rank} in the anchor frame; they become Forest items of stock
-- East Coast USA shapes (they have LODs, billboards and collision meshes), the variant nearest the
-- item's height, scaled to it. A tile shows the items whose fixed rank is under its distance's threshold
-- (ROADMAP "deterministic generation": a level shows the same trees, thinned), made and removed a few
-- hundred per frame (~19 us each, 2026-10-13 probe).
local M = {}
local vienna = require('beamngEarth/vienna_convention_speed_signs')

M.PER_FRAME = 250       -- items made or removed per frame at most
M.ROWS_PER_FRAME = 12000 -- rows of changing tiles looked at per frame at most (Lua time: 30000 averaged 2.5 ms)
-- 2026-10-14: was { 300, 1 }, { 550, 0.5 }, { 900, 0.25 }, { 1e9, 0.12 }; the engine already draws far trees as
-- billboards (their LODs), so our extra thinning made far forest sparse. Columbia: 48k -> 78k items, no frame cost seen.
M.RINGS = { { 300, 1.0 }, { 700, 0.6 }, { 1500, 0.35 }, { 1e9, 0.2 } }   -- tile distance (m): the rank shown below
-- ... and at most this many items a tile (the lowest ranks: the hashed ranks thin it evenly). Dense forest has 10-20k
-- a tile: Bodø's 145 tiles made 1.66 M items (GitHub #66: 28 fps standing on a Steam Deck, 23 ms of each frame waiting
-- on the GPU, and the engine's tick hitched at every tile change while driving: 19.8 fps over a drive, 38.4 with trees
-- off; 2026-10-20). Cities and farmland stay under it (Columbia: ~540 a tile).
M.TILE_CAP = { { 300, 6000 }, { 700, 2400 }, { 1500, 900 }, { 1e9, 400 } }
-- small props only near the player (2026-10-14, Fells Point at street level: ~10 ms a frame for the Forest
-- with a door on every house out to 300 m): the tile distance (m) beyond which a group is not shown
-- rocks only over the fine ground (farfield.NEAR_RADII: L0 within 400 m): beyond it the coarser tiles round off the
-- ridges and a rock placed on the real ground floated over the drawn one (Stelvio pass, the user's report 2026-10-17)
M.GROUP_MAX_M = { shrub = 250, desert_shrub = 250, rock = 400, desert_rock = 400, cliff = 1500, door = 120, hydrant = 200, bin = 200, bench = 200, polelamp = 400,
  stop = 300, giveway = 300, signal = 500, fence = 150, wallsign_logistics = 800, wallsign_rentabox = 800, wallsign_lemon = 800, wallsign_turboburger = 500,
  speed30 = 300, speed40 = 300, speed50 = 300, speed60 = 300, speed70 = 300 }
M.FOREST_NAME = 'bngEarthForest'      -- a prefix: small Forests from a pool ('bngEarthForest_<n>')
-- A small Forest per tile, from a pool (2026-10-15, 100 mph drives: one shared Forest with ~240k items made and ~220k
-- removed over a session made frames with any tree change cost ~100 ms in the engine's tick, and a fresh game ran the
-- same drive at 71 fps instead of 30-42). A dropped tile's items are removed (cheap in its small Forest) and the empty
-- Forest goes back to the pool: deleting a Forest whose items are in the physics' static collision hung the game.
-- Only tiles this near the player get colliding items (their trees, rocks and street furniture), the rest are made of
-- item types that never collide, a tile's items re-made when it crosses (back beyond SOLID_M + SOLID_HYST_M).
-- Solid items made ask for a collision rebuild (roads.lua, throttled): a Forest item collides only after
-- be:reloadCollision, and where no road tile arrives (the Sahara's rocks, 2026-10-14) none came at all.
-- 2026-10-15 probes: with ~150k trees all colliding, a frame changing 10 items cost the engine's tick ~124 ms, ~68 ms
-- when the changed items' type did not collide: much of a Forest change's cost grew with the colliding items.
M.SOLID_M = 200       -- (was 300 + 100: up to ~20k solid trees in a forest, and every terrain block move rebuilt all their
                      -- collision, a ~30 ms physics frame; 200 + 50 is still 5 s ahead at 100 mph, 2026-10-15)
M.SOLID_HYST_M = 50
-- At speed only the items along the car's path collide (Felix's log, 2026-10-26, 191 mph through the A13's pine forest:
-- the physics' collision instances, nearly all of them solid trees, grew from 11k to 100k, and the frame after each
-- rebuild (every roads.RELOAD_EVERY_S) took 100-154 ms more physics; a replay of that drive, 2026-10-26, showed why: each
-- tile crossing SOLID_M re-made up to 6000 items, more than the budget at 85 m/s, and the nearest tiles came first, so
-- the ones left behind never turned back). The items collide by CELL_M cells, not whole tiles (a tile whose square came
-- within SOLID_M made items up to ~560 m away solid): a cell within SOLID_M of the car (SOLID_HYST_M farther once solid),
-- and from FAST_MS on one within SOLID_SIDE_M of the line ahead of the car, from SOLID_BEHIND_M behind it to
-- SOLID_LEAD_S of driving ahead (SOLID_M at least), or within SOLID_ROUND_M of it (CELL_HYST_M farther once solid).
-- Tiles with items to turn back are done first.
M.FAST_MS = 25
M.CELL_M = 32
M.SOLID_SIDE_M = 50
M.SOLID_BEHIND_M = 40
M.SOLID_LEAD_S = 8
M.SOLID_ROUND_M = 60
M.CELL_HYST_M = 16      -- a solid cell stays so this much farther out (the heading's jitter)
-- Solid items made within URGENT_S of driving of the car (URGENT_MIN_M at least) are rebuilt into the collision at once
-- (roads.lua: URGENT_GAP_S apart at most), not after RELOAD_EVERY_S: most of a tile's items only show (and so only
-- become solid) once it is within its first ring, ~3.5 s ahead at 85 m/s (the replay: half of the trees the car passed
-- within 12 m at 307 km/h were not in the collision yet).
M.URGENT_S = 6
M.URGENT_MIN_M = 60
M.NO_COLLIDE = { shrub = true, desert_shrub = true, door = true, wallsign_logistics = true, wallsign_rentabox = true,
  wallsign_lemon = true, wallsign_turboburger = true }
-- The 100 mph stutter (2026-10-15 probes): a frame that adds or removes Forest items costs the engine's tick ~0.5 us
-- for every item in the level of each SHAPE touched (whatever its item type or Forest), so a frame making 300 items of
-- ~20 shapes among ~130k items cost ~100 ms. A frame touches shapes whose items add up to SHAPE_ITEMS_PER_FRAME at most
-- (at least one shape), and bushes (half of all items, one shape alone 34k) show near the player only.
M.SHAPE_ITEMS_PER_FRAME = 4000
-- The engine tells shapes apart by their path, so a tile's items use one of SHAPE_VARIANTS spellings of each shape's
-- path ('dir/./name', 'dir/././name', ...: the same file, a shape of its own), which splits the big populations (one
-- beech ~7.7k items: a frame touching it cost ~4-9 ms). A variant's first items cost one ~12 ms frame (its load).
M.SHAPE_VARIANTS = 4
-- Pop-in at speed (Felix, 2026-10-26, 250 km/h: "about 150 m ahead the trees suddenly pop in"; the mod's replay of the
-- A13 forest at 307 km/h: 6 % of the trees ahead first showed under 150 m, 30 % under 300 m). The per-frame budget above
-- went to whatever was nearest, behind the car as much as ahead of it. From FAST_MS on (LOOKAHEAD on):
-- - a tile ahead shows what it will show LOOK_S of driving later (LOOK_MAX_M at most): its rank and group distance are
--   the smaller of its distance now and from that point on the car's line;
-- - the budget goes ahead first: a tile outside the view cone ahead (VIEW_COS) counts SIDE_K times as far, one behind
--   the car BEHIND_K times plus BEHIND_M (a tile left with rows of a collision change keeps that change's class).
-- (Holding back the removals of dropped tiles made it worse: the shapes' populations grew, so every frame touched fewer.)
M.LOOKAHEAD = true
M.LOOK_S = 4
M.LOOK_MAX_M = 400
M.VIEW_COS = 0.77       -- 40 deg either side of the car's heading
M.SIDE_K = 2
M.BEHIND_K = 4
M.BEHIND_M = 2000
-- First loads (Felix's drive on a5ca450, 2026-10-26: 13 engine "Ticking" hitches of 70-250 ms in the first 5 minutes,
-- tree tiles pending, the collision rebuilds round them a few ms, and one of 1.3 s at the start). The engine loads a
-- shape, its materials and textures in the tick after its first item; a replay of captured drives (A13, Munich) gives
-- the same picture if a shape it never loaded costs ~100 ms there (probe PF5, 2026-10-11: a tree's first TSStatic
-- 125 ms) and another spelling of a loaded one (SHAPE_VARIANTS) ~12 ms: ~35-40 shapes in the start's frames, then 6-11
-- more one or two at a time as the drive meets them. So while the car stands (slower than WARM_MAX_MS) every shape of
-- the groups the tiles name is loaded ahead, one a work frame (and then WARM_SPELLINGS spellings), by an item of it in
-- a Forest of its own WARM_DEPTH_M under the car for WARM_KEEP_FRAMES; and a shape never loaded comes alone in its
-- frame, at speed COLD_GAP_S after the last one (at most COLD_SPELLINGS new spellings a work frame).
M.FIRST_LOADS = true     -- false: none of this (every shape as its items come)
M.WARM_MAX_MS = 3
M.WARM_DEPTH_M = 500
M.WARM_KEEP_FRAMES = 30
M.WARM_SPELLINGS = 3
M.COLD_GAP_S = 0.5
M.COLD_SPELLINGS = 2

local ECA = '/levels/east_coast_usa/art/shapes/'
local WCA = '/levels/west_coast_usa/art/shapes/'
local UT = '/levels/Utah/art/shapes/'
local JV = '/levels/johnson_valley/art/shapes/'
local JRI = '/levels/jungle_rock_island/art/shapes/'
local IT = '/levels/italy/art/shapes/'
-- the stock shapes per group with their measured heights (m; TSStatic world boxes, 2026-10-13/14)
M.CATALOG = {
  -- rock faces along rural road cuts (server roads.cliff_rows: Italy's rock-face kit, as its own cut slopes are made)
  cliff = {
    { IT .. 'rocks/italy_rockface_short_wide', 2.9 }, { IT .. 'rocks/italy_rockface_tall_wide', 13.2 },
  },
  deciduous = {
    { ECA .. 'trees/trees_oak/tree_oak_sml_a', 8.6 }, { ECA .. 'trees/trees_oak/tree_oak_sml_b', 9.5 },
    { ECA .. 'trees/trees_aspen/tree_aspen_small_a', 8.3 }, { ECA .. 'trees/trees_aspen/tree_aspen_small_c', 9.7 },
    { ECA .. 'trees/trees_beech/tree_beech_small_c', 9.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_a', 13.2 },
    { ECA .. 'trees/trees_oak/tree_oak_large_b', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_large_c', 15.5 },
    { ECA .. 'trees/trees_oak/tree_oak_forest_a', 15.9 }, { ECA .. 'trees/trees_oak/tree_oak_forest_b', 16.0 },
    { ECA .. 'trees/trees_beech/tree_beech_large_c', 17.4 }, { ECA .. 'trees/trees_aspen/tree_aspen_forest_b', 18.5 },
    { ECA .. 'trees/trees_beech/tree_beech_forest_a', 20.2 }, { ECA .. 'trees/trees_beech/tree_beech_large_b', 21.1 },
  },
  conifer = {
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_b', 11.2 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_small_a', 13.4 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_c', 18.9 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_b', 21.4 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 },
  },
  shrub = {
    { ECA .. 'trees/ECA_coast_bush/generibush', 1.8 }, { ECA .. 'trees/trees_aspen/tree_aspen_bush_a', 2.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_bush_a', 2.7 }, { ECA .. 'trees/trees_oak/tree_oak_bush_c', 3.1 },
    { ECA .. 'trees/trees_beech/tree_beech_bush_b', 3.3 }, { ECA .. 'trees/trees_beech/tree_beech_bush_c', 3.9 },
    { ECA .. 'trees/trees_oak/tree_oak_bush_b', 4.1 },
  },
  rock = {
    { ECA .. 'rocks/eca_rock_small', 0.6 }, { ECA .. 'rocks/rock_01_a_large_a', 0.7 }, { ECA .. 'rocks/eca_rock_small_2', 0.9 },
    { ECA .. 'rocks/eca_rock_medium_1', 1.5 }, { ECA .. 'rocks/eca_rock_medium_2', 1.6 }, { ECA .. 'rocks/eca_rock_big_narrow', 1.9 },
    { ECA .. 'rocks/eca_rock_big_wide', 1.9 },
  },
  pine_west = {
    { WCA .. 'trees/trees_conifer/pine_radiata_small', 8.6 }, { WCA .. 'trees/trees_conifer/pine_radiata_forest', 13.3 },
    { WCA .. 'trees/trees_conifer/pine_radiata_large', 13.2 }, { WCA .. 'trees/trees_conifer/pine_radiata_weathered', 13.2 },
    { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_a', 19.6 }, { ECA .. 'trees/trees_douglasfir/tree_douglasfir_large_d', 21.7 },
    { IT .. 'trees/trees_italy/scots_pine', 18.9 }, { WCA .. 'trees/trees_conifer/redwood_tall', 38.5 },
    { WCA .. 'trees/trees_conifer/redwood_split', 38.1 }, { WCA .. 'trees/trees_conifer/redwood_broad', 38.5 },
  },
  oak_west = {
    { WCA .. 'trees/trees_oak/oak_d', 7.1 }, { WCA .. 'trees/trees_oak/oak_b', 8.2 }, { WCA .. 'trees/trees_oak/oak_c', 8.4 },
    { WCA .. 'trees/trees_oak/oak_a', 9.6 }, { WCA .. 'trees/trees_eucalyptus/eucalyptus_double', 14.7 },
    { WCA .. 'trees/trees_eucalyptus/eucalyptus_tall', 22.5 },
  },
  palm = {
    { JRI .. 'trees/trees_palm/palm_small', 8.9 }, { JRI .. 'trees/trees_palm/palm_short', 9.1 },
    { WCA .. 'trees/trees_palm/fanpalm_short', 10.6 }, { WCA .. 'trees/trees_palm/fanpalm_skirt', 11.7 },
    { WCA .. 'trees/trees_palm/fanpalm_full', 13.8 }, { JRI .. 'trees/trees_palm/palm_medium', 18.0 },
    { WCA .. 'trees/trees_palm/fanpalm_tall', 18.5 }, { WCA .. 'trees/trees_palm/fanpalm_slim', 20.8 },
    { JRI .. 'trees/trees_palm/palm_tall', 25.3 },
  },
  juniper = {
    { UT .. 'trees/Juniper/Juniper_bush_large_01', 2.6 }, { UT .. 'trees/Juniper/Juniper_tree_04', 4.5 },
    { UT .. 'trees/Juniper/Juniper_tree_03', 5.2 }, { UT .. 'trees/Juniper/Juniper_tree_01', 6.3 },
    { UT .. 'trees/Juniper/Juniper_tree_02', 7.4 }, { UT .. 'trees/Juniper/Juniper_ancient_01', 7.5 },
  },
  -- (2026-10-16: not JV's deserttree_bush_tiny: it ships no imposter, and the one the engine baked into its temp
  -- folder while the leaf material did not resolve is solid no-texture orange, kept for good: orange bushes at range)
  desert_shrub = {
    { UT .. 'groundcover/s_gc_utah_grey_bush_01', 0.7 }, { JV .. 'groundcover/s_gc_jv_dry_bush_01', 0.7 },
    { UT .. 'groundcover/s_gc_utah_grey_bush_03', 1.0 },
    { JV .. 'groundcover/s_gc_jv_dry_bush_03', 1.0 }, { UT .. 'trees/Juniper/Juniper_bush_small_01', 1.2 },
    { UT .. 'trees/Juniper/Juniper_bush_small_dry_01', 2.0 }, { UT .. 'trees/Juniper/Juniper_bush_medium_01', 2.4 },
    { JV .. 'trees/deserttrees/deserttree_bush_medium', 2.9 },
  },
  desert_rock = {
    { UT .. 'rocks/rock_01_a_small_a', 0.3 }, { UT .. 'rocks/utah_rock_flat_a', 0.5 }, { UT .. 'rocks/utah_boulder_5', 0.6 },
    { JV .. 'rocks/s_rocks_desert_small_01', 0.9 }, { JV .. 'rocks/s_rocks_desert_medium_03', 1.4 },
    { UT .. 'rocks/utah_boulder_3', 1.7 }, { UT .. 'rocks/utah_boulder_1', 1.9 }, { JV .. 'rocks/s_rocks_desert_medium_01', 2.1 },
  },
  door = {     -- the stock Building Architect kit (art_shapes.zip; global materials)
    { '/art/shapes/common/building_architect_modules/doors/R_HS_A_DR_01_100x210', 2.2 },
    { '/art/shapes/common/building_architect_modules/doors/R_HS_B_DR_03_100x210', 2.2 },
  },
  signal = { { '/art/shapes/objects/s_trafficlight_boom_sn', 9.4 } },
  stop = { { '/levels/east_coast_usa/art/shapes/signs_usa/sign_stop', 3.0 } },
  giveway = { { '/art/shapes/garage_and_dealership/Clutter/italy_traf_sign_give_way', 3.0 } },
  -- a low wooden yard fence, 5.4 m along its x (server buildings._front_fence)
  fence = { { ECA .. 'buildings/eca_bld_wood_fence_c', 1.3 } },
  -- company signs on big boxes, warehouses and restaurants (server buildings._wall_sign; West Coast USA's firms)
  wallsign_logistics = { { WCA .. 'objects/sign_logistics', 5.8 } }, wallsign_rentabox = { { WCA .. 'objects/sign_rentabox', 5.7 } },
  wallsign_lemon = { { WCA .. 'objects/sign_lemon', 3.6 } }, wallsign_turboburger = { { WCA .. 'objects/sign_turboburger', 2.4 } },
  -- speed-limit signs (server furniture.speed_signs: the road graph's limit, where a model exists)
  speed30 = { { UT .. 'ut_roadsigns/ut_30_speed_limit', 2.6 } }, speed40 = { { UT .. 'ut_roadsigns/ut_40_speed_limit', 2.6 } },
  speed50 = { { UT .. 'ut_roadsigns/ut_50_speed_limit', 2.6 } }, speed60 = { { UT .. 'ut_roadsigns/ut_60_speed_limit', 2.6 } },
  speed70 = { { UT .. 'ut_roadsigns/ut_70_speed_limit', 2.6 } },
  -- (the round km/h signs, speedk30 .. speedknone: vienna_convention_speed_signs.lua, installed below)
  hydrant = { { '/levels/west_coast_usa/art/shapes/objects/hydrant1', 1.5 } },
  -- a toll plaza's island with its booth (server buildings.toll_booth_rows; the user, 2026-10-21: "west coast has one,
  -- reference that"): 22 m along its local y, 1.6 m wide
  tollbooth = { { WCA .. 'objects/s_toll_booth_center', 4.96 } },
  bench = { { ECA .. 'clutter/clutter_city_bench_wood', 0.9 } },
  bin = { { '/art/shapes/garage_and_dealership/Clutter/clutter_city_bin_round', 1.1 }, { WCA .. 'objects/garbage_bin_a', 1.1 } },
  polelamp = { { ECA .. 'buildings/electrical_pole_lamp', 1.1 } },
  streetlight = { { '/art/shapes/garage_and_dealership/Clutter/eca_lamppost', 9.0 } },
  pole = { { '/levels/automation_test_track/art/shapes/objects/s_powerpole_3wire', 9.2 } },
  tropical = {
    { JRI .. 'trees/bush_tropical_1/tro_bush_huge', 5.5 }, { JRI .. 'trees/bush_tropical_2/tro_bush_2_small', 6.1 },
    { JRI .. 'trees/trees_tropical_2/tro_tree_2_filler', 20.2 }, { JRI .. 'trees/trees_tropical_1/tro_tree_1_huge', 20.6 },
    { JRI .. 'trees/trees_tropical_1/tro_tree_1_filler', 33.0 },
  },
}
-- the Vienna Convention's round km/h signs: their shapes and how far out they show (CATALOG, GROUP_MAX_M)
vienna.install(M)
local MATERIALS = {
  ECA .. 'trees/trees_oak', ECA .. 'trees/trees_beech', ECA .. 'trees/trees_aspen', ECA .. 'trees/trees_douglasfir',
  ECA .. 'trees/ECA_coast_bush', ECA .. 'trees/common', ECA .. 'trees', ECA .. 'rocks',
  WCA .. 'trees/trees_conifer', WCA .. 'trees/trees_oak', WCA .. 'trees/trees_eucalyptus', WCA .. 'trees/trees_palm',
  WCA .. 'trees', UT .. 'trees/Juniper', UT .. 'trees', UT .. 'groundcover', UT .. 'rocks', JV .. 'groundcover',
  JV .. 'rocks', JV .. 'trees/deserttrees', JV .. 'trees', JRI .. 'trees/trees_palm', JRI .. 'trees/trees_tropical_1',
  JRI .. 'trees/trees_tropical_2', JRI .. 'trees/bush_tropical_1', JRI .. 'trees/bush_tropical_2', JRI .. 'trees',
  JRI .. 'trees/vines_tropical',     -- tro_tree_1's vines and aerial roots (tro_vines_2d; orange without it)
  IT .. 'trees/trees_italy', IT .. 'trees', IT .. 'rocks', '/levels/automation_test_track/art/shapes/objects',
  '/levels/automation_test_track/art/shapes/buildings',     -- the utility pole's wood_trim / painted metal
  ECA .. 'signs_usa', '/art/shapes/objects', '/levels/east_coast_usa/art/shapes', WCA .. 'objects', ECA .. 'buildings',
  UT .. 'ut_roadsigns', '/levels/driver_training/art/shapes/race',
  -- the toll booth's (its kerbs, glass, trim and lamps)
  '/art/shapes/garage_and_dealership/garage', '/art/shapes/garage_and_dealership', '/art/shapes/race/dragstrip',
  '/art/shapes/objects/windows_doors_reflective',
}

local stopwatch = require('beamngEarth/stopwatch')
local tiles = {}        -- key "a:tx_ty" -> { anchor, cx, cy, flat, n, made = { i -> item }, shown = rank }

-- A tile's rows (the server's [group, x, y, z, yaw, -, height, rank] each) kept packed: ROW_N numbers per row in one
-- flat array, their defaults filled in (row i's k-th at flat[(i - 1) * ROW_N + k]). As a table each, the ~1M rows of
-- ~150 tiles made the game's Lua heap 260-273 MB, and its garbage collector walked every one of them: LuaGC hitches of
-- 90-273 ms at 200 mph (Felix's drive, 2026-10-26, "tree tiles 140 pending").
local ROW_N = 8
local ROW_DEFAULT = { 0, 0, 0, 0, 0, 0, 10, 0 }
local okNew, tableNew = pcall(require, 'table.new')
if not okNew then tableNew = function() return {} end end

local function pack(rows)
  local n = #rows
  local flat = tableNew(n * ROW_N, 0)
  local j = 0
  for i = 1, n do
    local row = rows[i]
    for k = 1, ROW_N do flat[j + k] = type(row) == 'table' and tonumber(row[k]) or ROW_DEFAULT[k] end
    j = j + ROW_N
  end
  return flat, n
end
M._pack = pack
local WEAK = { __mode = 'v' }
local foci = {}         -- other vehicles' collision foci { x, y, r, trees } (M.setFoci, collisionfoci.lua)
local ghostSince = nil               -- since the last rebase that left ghost tiles (M.transform)
local types = {}        -- "shape" / "shape#n" (not solid) -> TSForestItemData
local pop = {}          -- shape -> its items in the level
local loaded, loadedBase = {}, {}   -- shape (as spelled) / its plain path -> true once an item of it was made
local warmBases, warmSpells, warmQueued = {}, {}, {}   -- shapes to load ahead (M.WARM_MAX_MS)
local warmItems = {}    -- { item, frame } made to load a shape
local warmForestId = nil
local coldBase, coldSpell, coldLast = 0, 0, nil      -- this work frame's first loads; the clock of the last shape's
local slowNow = true    -- the car slower than WARM_MAX_MS (this work frame)
local clock = nil       -- ms (stopwatch.clock)
M.coldLoads, M.warmLoads, M.coldRecent = 0, 0, {}
local ready = false
M.made, M.removed, M.errors = 0, 0, 0
M.unknownGroups = {}      -- groups seen without a CATALOG entry (logged once each)

local pool = {}          -- ids of empty Forests to reuse
local forestCount = 0
M.forests = 0

-- Pure: the n-th pool Forest's name.
function M.forestName(n)
  return M.FOREST_NAME .. '_' .. tostring(n)
end

-- The tile's Forest: its own, else one from the pool, else a new one.
local function forest(t)
  local f = t.forestId and scenetree.findObjectById(t.forestId)
  while not f and #pool > 0 do
    f = scenetree.findObjectById(table.remove(pool))
    if f then f:setPosRot(0, 0, 0, 0, 0, 0, 1) end       -- a ghost's (M.transform) had the rebase's transform
  end
  if not f then
    forestCount = forestCount + 1
    f = createObject('Forest')
    f:registerObject(M.forestName(forestCount))
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
    M.forests = forestCount
  end
  t.forestId = f:getID()
  return f
end

-- An emptied tile's Forest back to the pool.
local function releaseForest(t)
  if t.forestId then pool[#pool + 1] = t.forestId end
  t.forestId = nil
end

local function setup()
  if ready then return true end
  for _, m in ipairs(MATERIALS) do
    local p = m .. '/main.materials.json'
    if not FS or FS:fileExists(p) then pcall(loadJsonMaterialsFile, p) end
  end
  ready = true
  return true
end

-- Other vehicles' collision foci (collisionfoci.lua, through core.lua): solid trees round the ones with trees.
function M.setFoci(f) foci = f or {} end

-- Pure: the item type's name of a shape, solid (collides) or not.
function M.typeName(shape, solid)
  return 'bngEarthTree_' .. shape:gsub('[^%w]', '_') .. (solid and '' or '_n')
end

local function itemType(shape, solid)
  local key = solid and shape or (shape .. '#n')
  local d = types[key]
  if d then return d end
  local name = M.typeName(shape, solid)
  d = scenetree.findObject(name)
  if not d then
    d = (worldEditorCppApi and worldEditorCppApi.createObject) and worldEditorCppApi.createObject('TSForestItemData')
      or createObject('TSForestItemData')
    if not d then return nil end
    d:setField('shapeFile', 0, shape .. '.dae')
    d:setField('collidable', 0, solid and '1' or '0')
    d:setField('windScale', 0, '0.4'); d:setField('branchAmp', 0, '0.1'); d:setField('detailAmp', 0, '0.2')
    d:registerObject(name)
    if Sim and Sim.getDataBlockSet then Sim.getDataBlockSet():addObject(d) end
  end
  types[key] = d
  return d
end

-- The variant of a group for an item of height h: those within 0.8 .. 1.25 of it (else 0.6 .. 1.6), picked by the
-- item's rank; else the nearest. Returns shape, scale. (No tables made: a call for every row of every new tile.)
local RANGES = { { 0.8, 1.25 }, { 0.6, 1.6 } }
local cands = {}
function M.pick(group, h, rank)
  local list = M.CATALOG[group] or M.CATALOG.deciduous
  local nc = 0
  for _, range in ipairs(RANGES) do      -- near its size first, then wider
    for _, v in ipairs(list) do
      if v[2] >= h * range[1] and v[2] <= h * range[2] then nc = nc + 1; cands[nc] = v end
    end
    if nc > 0 then break end
  end
  local v
  if nc == 0 then
    local bd
    for _, c in ipairs(list) do
      local d = math.abs(math.log(c[2] / math.max(h, 0.1)))
      if not bd or d < bd then v, bd = c, d end
    end
  else
    v = cands[math.floor(rank * 7919) % nc + 1]
  end
  return v[1], math.max(0.3, math.min(2.5, h / v[2]))
end

local GROUPS = { 'deciduous', 'conifer', 'shrub', 'rock' }
local releaseGhosts
local groupBand

-- Pure: the variant v's spelling of a shape path (0: itself).
local spelled = {}       -- shape -> { v -> its spelling } (no new string a row)
function M.variant(shape, v)
  if not v or v <= 0 then return shape end
  local c = spelled[shape]
  if not c then c = {}; spelled[shape] = c end
  local sp = c[v]
  if not sp then
    sp = shape:gsub('/([^/]+)$', '/' .. string.rep('./', v) .. '%1')
    c[v] = sp
  end
  return sp
end

-- Pure: a tile's shape variant from its key ("a:tx_ty" / "a:ptx_ty"): neighbours differ.
function M.tileVariant(key)
  local tx, ty = tostring(key):match('(%-?%d+)_(%-?%d+)$')
  if not tx then return 0 end
  return (tonumber(tx) * 3 + tonumber(ty) * 5) % math.max(M.SHAPE_VARIANTS, 1)
end

-- The shape (the tile's variant) and scale of a tile's row i (cached: M.pick makes tables).
local function shapeOf(t, i)
  local s = t.shapes[i]
  if s then return s, t.scales[i] end
  local sc
  local f, j = t.flat, (i - 1) * ROW_N
  s, sc = M.pick((t.groups or GROUPS)[f[j + 1] + 1] or 'deciduous', f[j + 7], f[j + 8])
  s = M.variant(s, t.variant)
  t.shapes[i], t.scales[i] = s, sc
  return s, sc
end

-- group: a sign's group (vienna_convention_speed_signs.lua) also makes a second item at the same spot and scale, its
-- body, and takes the collision. Only the first shape is counted in pop (allow's budget): the body is shared by every
-- sign, spelled in the tile's variant like the rest, so its population is split.
-- Pure: a shape's plain path (M.variant's spellings undone).
function M.baseOf(shape)
  local b, n = shape, 1
  while n > 0 do b, n = b:gsub('/%./', '/') end
  return b
end

local function now()
  clock = clock or stopwatch.clock()
  return clock()
end

local function markLoaded(shape)
  if loaded[shape] then return end
  loaded[shape] = true
  local b = M.baseOf(shape)
  if not loadedBase[b] then
    loadedBase[b] = true
    M.coldLoads = M.coldLoads + 1
    local r = M.coldRecent
    r[#r + 1] = now()
    if #r > 8 then table.remove(r, 1) end
  end
end

-- May the first item of a shape the engine has not loaded be made now? (see WARM_MAX_MS)
local function coldOk(shape)
  if loaded[shape] or not M.FIRST_LOADS then return true end
  if loadedBase[M.baseOf(shape)] then
    if coldSpell >= M.COLD_SPELLINGS then return false end
    coldSpell = coldSpell + 1
    return true
  end
  if coldBase > 0 then return false end
  if not slowNow and coldLast and now() - coldLast < M.COLD_GAP_S * 1000 then return false end
  coldBase, coldLast = coldBase + 1, now()
  return true
end

-- The shapes of a tile's groups to load ahead (each once).
local function queueWarm(groups)
  for _, g in ipairs(groups or {}) do
    local list = M.CATALOG[g] or {}
    local body = vienna.body(g)
    if body then list = { { body }, unpack(list) } end
    for _, v in ipairs(list) do
      local b = v[1]
      if not warmQueued[b] then
        warmQueued[b] = true
        warmBases[#warmBases + 1] = b
        for k = 1, M.SHAPE_VARIANTS - 1 do warmSpells[#warmSpells + 1] = M.variant(b, k) end
      end
    end
  end
end

local function warmForest()
  local f = warmForestId and scenetree.findObjectById(warmForestId)
  if not f then
    f = createObject('Forest')
    f:registerObject(M.FOREST_NAME .. '_warm')
    if scenetree.MissionGroup then scenetree.MissionGroup:addObject(f) end
    warmForestId = f:getID()
  end
  return f
end

-- Items made to load a shape go after M.WARM_KEEP_FRAMES (all: now).
local function expireWarm(frame, all)
  if #warmItems == 0 then return end
  local f = warmForestId and scenetree.findObjectById(warmForestId)
  local fd = f and f:getData()
  local keep = {}
  for _, w in ipairs(warmItems) do
    if all or frame - w[2] >= M.WARM_KEEP_FRAMES then
      if fd then pcall(function() fd:removeItem(w[1]) end) end
    else keep[#keep + 1] = w end
  end
  warmItems = keep
end

-- An item of a shape WARM_DEPTH_M under the car, for WARM_KEEP_FRAMES: the engine loads the shape.
local function warmShape(shape, player, frame)
  local d = itemType(shape, false)
  if d then
    local m = MatrixF(true)
    m:setPosition(vec3(player.x, player.y, (player.z or 0) - M.WARM_DEPTH_M))
    local fd = warmForest():getData()
    local ok, it = pcall(function() return fd:createNewItem(d, m, 1) end)
    if ok and it then warmItems[#warmItems + 1] = { it, frame } end
  end
  markLoaded(shape)
  M.warmLoads = M.warmLoads + 1
end

-- One shape loaded ahead (else up to WARM_SPELLINGS spellings of loaded ones).
local function warmStep(player, frame)
  local list, n = warmBases, 1
  while #warmBases > 0 and loadedBase[warmBases[1]] do table.remove(warmBases, 1) end
  if #warmBases == 0 then
    while #warmSpells > 0 and loaded[warmSpells[1]] do table.remove(warmSpells, 1) end
    if #warmSpells == 0 then return end
    list, n = warmSpells, M.WARM_SPELLINGS
  end
  for _ = 1, n do
    local shape = list[1]
    if not shape or not coldOk(shape) then return end
    table.remove(list, 1)
    warmShape(shape, player, frame)
  end
end

local function makeItem(fd, f, j, shape, sc, solid, group, variant)
  local d = itemType(shape, vienna.faceSolid(group, solid))
  if not d then M.errors = M.errors + 1; return nil end
  local m = MatrixF(true)
  -- setFromEuler turns clockwise (the engine's headings): a row's yaw (counter-clockwise from +x, the server's) is
  -- given negated. Positive, every prop was mirrored about the x axis: doors on north-south walls backwards, on
  -- diagonal walls askew, benches and lamps turned (read back in-game 2026-10-17: rows at 141.6 rendered at -141.6)
  m:setFromEuler(vec3(0, 0, -math.rad(f[j + 5])))
  m:setPosition(vec3(f[j + 2], f[j + 3], f[j + 4]))
  local ok, it = pcall(function() return fd:createNewItem(d, m, sc) end)
  if not ok then M.errors = M.errors + 1; return nil end
  local body = vienna.body(group)
  if body then
    local pair = vienna.addBody(fd, it, M.variant(body, variant), m, sc, solid, itemType)
    if pair then it = pair else M.errors = M.errors + 1 end
  end
  M.made = M.made + 1
  pop[shape] = (pop[shape] or 0) + 1
  markLoaded(shape)
  return it
end

local function removeItem(fd, it, shape)
  if not vienna.remove(fd, it) then pcall(function() fd:removeItem(it) end) end
  M.removed = M.removed + 1
  if shape then pop[shape] = math.max((pop[shape] or 1) - 1, 0) end
end

-- This frame's shapes: may an item of this shape change now? (see SHAPE_ITEMS_PER_FRAME)
local touched, touchedItems = {}, 0
local function allow(shape)
  if touched[shape] then return true end
  local n = pop[shape] or 0
  if next(touched) ~= nil and touchedItems + n > M.SHAPE_ITEMS_PER_FRAME then return false end
  if not loaded[shape] and not coldOk(shape) then return false end
  touched[shape], touchedItems = true, touchedItems + n
  return true
end

-- A 'veg_tile' message: { anchorId, key, path, n }.
function M.onTile(msg, currentAnchor)
  if msg.anchorId ~= currentAnchor or type(msg.path) ~= 'string' then return end
  -- the file's JSON in the message (server staging.inline_json: Windows' first read of a file the server just wrote took
  -- up to 0.5 s in the frame), else the file
  local ok, data = true, msg.data
  if type(data) ~= 'table' then ok, data = pcall(jsonDecode, readFile(msg.path) or '') end
  if not ok or type(data) ~= 'table' or type(data.items) ~= 'table' then
    M.errors = M.errors + 1
    return
  end
  M.onRows(msg.anchorId, msg.key, data.items, data.groups)
end

-- Rows of a tile from elsewhere (a road tile's street furniture: roads.lua, key "p<tx>_<ty>").
function M.onRows(anchorId, tileKey, rows, groups)
  local key = anchorId .. ':' .. tostring(tileKey)
  local old = tiles[key]
  local tx, ty = tostring(tileKey):match('(%-?%d+)_(%-?%d+)$')
  rows = type(rows) == 'table' and rows or {}
  local same = old and old.src[1] == rows      -- (the same rows again: a road tile's props sent once more)
  local flat, n
  if same then flat, n = old.flat, old.n else flat, n = pack(rows) end
  local t = { anchor = anchorId, flat = flat, n = n, src = setmetatable({ rows }, WEAK),
    groups = type(groups) == 'table' and groups or nil,
    made = old and old.made or {}, solid = old and old.solid or {}, near = old and old.near,
    shapes = same and old.shapes or {}, scales = same and old.scales or {},
    madeShape = old and old.madeShape or {}, shown = old and old.shown or 0, forestId = old and old.forestId or nil,
    variant = M.tileVariant(key), cx = (tonumber(tx) or 0) * 256 + 128, cy = (tonumber(ty) or 0) * 256 + 128, drop = false }
  tiles[key] = t
  queueWarm(t.groups or GROUPS)
end

-- The tile "anchorId:key" goes (roads.lua's furniture).
function M.dropKey(key)
  local t = tiles[key]
  if t then t.drop = true end
end

-- A 'veg_drop' message: the tile's items go.
function M.onDrop(msg)
  local t = tiles[msg.anchorId .. ':' .. tostring(msg.key)]
  if t then t.drop = true end
end

local function threshold(d)
  for _, r in ipairs(M.RINGS) do
    if d <= r[1] then return r[2] end
  end
  return 0
end

-- The rank tile t shows below at distance d: threshold(d), lowered to keep it within M.TILE_CAP (the cap-th lowest
-- of its rows' ranks: exact whatever share of the area's items the server sent, vegetation.TILE_ROWS_MAX).
local function tileThreshold(d, t)
  local want = threshold(d)
  local n, f = t.n or 0, t.flat
  for _, r in ipairs(M.TILE_CAP) do
    if d <= r[1] then
      if n <= r[2] then return want end
      if not t.ranks then
        local rk = {}
        for i = 1, n do rk[i] = f[(i - 1) * ROW_N + 8] end
        table.sort(rk)
        t.ranks = rk
      end
      return math.min(want, t.ranks[r[2] + 1])
    end
  end
  return want
end
M.tileThreshold = tileThreshold

-- The solid cells of tile t for the car f {x, y; moving at FAST_MS or more: ux, uy (its direction), lead (m)}: a
-- string of one '1' or '0' per CELL_M cell, row by row from the tile's south-west corner, or '' when none is.
local maskBuf = {}      -- (cellMask's, reused: every cell is written each call)
function M.cellMask(t, f)
  local n = math.floor(256 / M.CELL_M)
  local round, hyst = M.SOLID_M, M.SOLID_HYST_M
  if f.ux then round, hyst = M.SOLID_ROUND_M, M.CELL_HYST_M end
  local reach = 181 + hyst
  local dx, dy = t.cx - f.x, t.cy - f.y
  if dx * dx + dy * dy > (round + reach) ^ 2 then
    if not f.ux then return '' end
    local along, side = dx * f.ux + dy * f.uy, math.abs(dx * f.uy - dy * f.ux)
    if side > M.SOLID_SIDE_M + reach or along < -(M.SOLID_BEHIND_M + reach) or along > f.lead + reach then return '' end
  end
  local old, half = t.mask or '', M.CELL_M * 0.7072
  local x0, y0 = t.cx - 128 - f.x, t.cy - 128 - f.y
  local out, any = maskBuf, false
  for j = 0, n - 1 do
    for i = 0, n - 1 do
      local k = j * n + i + 1
      local r = half + (old:byte(k) == 49 and hyst or 0)
      local cx, cy = x0 + (i + 0.5) * M.CELL_M, y0 + (j + 0.5) * M.CELL_M
      local on = cx * cx + cy * cy <= (round + r) ^ 2
      if not on and f.ux then
        local a, s = cx * f.ux + cy * f.uy, math.abs(cx * f.uy - cy * f.ux)
        on = a >= -(M.SOLID_BEHIND_M + r) and a <= f.lead + r and s <= M.SOLID_SIDE_M + r
      end
      out[k] = on and '1' or '0'
      any = any or on
    end
  end
  return any and table.concat(out, '', 1, n * n) or ''
end

-- Does cell mask new turn off a cell solid in old?
function M.cellsOff(old, new)
  if old == '' or old == new then return false end
  for k = 1, #old do
    if old:byte(k) == 49 and new:byte(k) ~= 49 then return true end
  end
  return false
end

-- The cell (index into a cell mask) of tile t's row i (cached with the rows: t.cells, t.cellRows).
local function cellRows(t)
  if t.cells then return end
  local n = math.floor(256 / M.CELL_M)
  local x0, y0 = t.cx - 128, t.cy - 128
  t.cells, t.cellRows = {}, {}
  local f = t.flat
  for i = 1, t.n do
    local j = (i - 1) * ROW_N
    local ci = math.max(0, math.min(n - 1, math.floor((f[j + 2] - x0) / M.CELL_M)))
    local cj = math.max(0, math.min(n - 1, math.floor((f[j + 3] - y0) / M.CELL_M)))
    local c = cj * n + ci + 1
    t.cells[i] = c
    local l = t.cellRows[c]
    if not l then l = {}; t.cellRows[c] = l end
    l[#l + 1] = i
  end
end
function M.cellOf(t, i)
  cellRows(t)
  return t.cells[i]
end
-- Tile t's row -> cell table.
function M.cellsOf(t)
  cellRows(t)
  return t.cells
end

-- The rows of tile t in the cells whose bit differs between masks old and new and those in extra (left over from an
-- earlier frame): extra's first, then the changed cells' nearest the car {x, y} first (a frame's budget may end in the
-- list). Read only by the caller: extra itself when no cell changed. (No set of extra's rows, no scan of every row:
-- at 307 km/h that made ~3 MB of garbage a second, the replay 2026-10-26.)
local changedCells, cellOrder, cellDist = {}, {}, {}
function M.changedRows(t, old, new, extra, car)
  local n = math.floor(256 / M.CELL_M)
  local nc = 0
  if old ~= new then
    local x0, y0 = t.cx - 128 - (car and car.x or t.cx), t.cy - 128 - (car and car.y or t.cy)
    for k = 1, n * n do
      local c = (old:byte(k) == 49) ~= (new:byte(k) == 49)
      changedCells[k] = c
      if c then
        local dx, dy = x0 + ((k - 1) % n + 0.5) * M.CELL_M, y0 + (math.floor((k - 1) / n) + 0.5) * M.CELL_M
        nc = nc + 1
        cellOrder[nc], cellDist[k] = k, dx * dx + dy * dy
      end
    end
  end
  if nc == 0 then return extra or {} end
  for q = nc + 1, #cellOrder do cellOrder[q] = nil end
  table.sort(cellOrder, function(a, b) return cellDist[a] < cellDist[b] end)
  cellRows(t)
  local out, cr, cells = {}, t.cellRows, t.cells
  if extra then
    for q = 1, #extra do
      local i = extra[q]
      if not changedCells[cells[i]] then out[#out + 1] = i end
    end
  end
  for q = 1, nc do
    local l = cr[cellOrder[q]]
    if l then for r = 1, #l do out[#out + 1] = l[r] end end
  end
  return out
end

-- M.suppress(group, x, y) -> true hides that item (set by navgraph.lua); M.refresh() re-runs every tile's rows
-- after it changes.
M.suppress = nil
function M.refresh()
  for _, t in pairs(tiles) do t.pending = true end
end

-- Which of the capped groups a tile at distance d still shows (changes re-run its rows).
groupBand = function(t, d)
  local k = 0
  for _, cap in pairs(M.GROUP_MAX_M) do if d <= cap then k = k + cap end end
  return k
end

-- Origin rebase (core.lua finishRebase): each shown tile's Forest takes the rebase's transform (p_new = R*p + t), so
-- its items show where they belong in the new frame at once, and the tile stays as a "ghost" until the new anchor's
-- tiles over it are filled (or it is GHOST_FAR_M behind, or GHOST_MAX_S). Before, every tree went at the
-- rebase and came back over ~25 s (2026-10-17, the user: "trees take several seconds to regenerate after a rebase").
-- A Forest's transform moves its items' rendering but not their collision (probe 2026-10-17: castRayStatic still hit
-- the old spot), which stays in the old frame's place, ~15 km from the player after a rebase, until the ghost goes.
-- A ghost also goes once it is farther from the player than GHOST_FAR_M (the server's vegetation DROP_M: where its tile
-- would have been dropped anyway) or after GHOST_MAX_S. (Not after a quiet spell without new tiles: at 90 mph the new
-- anchor's first tiles took longer and every tree went, 2026-10-17 salt flats.)
M.GHOST_MAX_S = 120
M.GHOST_FAR_M = 1536
M.GHOST_FAR_WAIT_S = 2

function M.transform(xf, geodesy)
  local bx, by, bz, bw = xf:beamngQuat()
  local any = false
  for _, t in pairs(tiles) do
    if not t.drop then
      local q = t.fq or { 0, 0, 0, 1 }
      local x, y, z, w = geodesy.quatMul(q[1], q[2], q[3], q[4], bx, by, bz, bw)
      t.fq = { x, y, z, w }
      local o = t.ft or { 0, 0, 0 }
      local ox, oy, oz = xf:apply(o[1], o[2], o[3])
      t.ft = { ox, oy, oz }
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      if f then f:setPosRot(ox, oy, oz, x, y, z, w) end
      t.cx, t.cy = xf:apply(t.cx, t.cy, 0)
      t.ghost = true
      t.pending = false
      any = true
    end
  end
  if any then ghostSince = stopwatch.new() end
end

-- Ghost tiles whose square the current anchor's filled tiles cover go (emptied by update's first loop).
releaseGhosts = function(anchorId, player)
  if not ghostSince then return end
  local all = ghostSince() > M.GHOST_MAX_S * 1000
  local done = {}
  if not all then
    for _, t in pairs(tiles) do
      if t.anchor == anchorId and not t.ghost and not t.drop then
        local k = math.floor(t.cx / 256) .. '_' .. math.floor(t.cy / 256)
        done[k] = (done[k] ~= false) and not t.pending and (t.shown or 0) > 0
      end
    end
  end
  local left = false
  for _, t in pairs(tiles) do
    if t.ghost and not t.drop then
      local ok = all
      if not ok and player and ghostSince() > M.GHOST_FAR_WAIT_S * 1000 then     -- (roads.lua GHOST_FAR_WAIT_S)
        local dx = math.max(math.abs(player.x - t.cx) - 128, 0)
        local dy = math.max(math.abs(player.y - t.cy) - 128, 0)
        ok = math.max(dx, dy) > M.GHOST_FAR_M
      end
      if not ok then
        ok = true
        for gx = math.floor((t.cx - 128) / 256), math.floor((t.cx + 128 - 1e-6) / 256) do
          for gy = math.floor((t.cy - 128) / 256), math.floor((t.cy + 128 - 1e-6) / 256) do
            if not done[gx .. '_' .. gy] then ok = false end
          end
        end
      end
      if ok then t.drop = true else left = true end
    end
  end
  if not left then ghostSince = nil end
end

-- Every frame: tiles of other anchors and dropped ones emptied, the others filled / thinned to their
-- distance's rank, M.PER_FRAME items at most.
-- Tree work every EVERY_N-th frame, N times the items: a frame with any Forest change costs the engine's tick a few
-- ms whatever the count (2026-10-15, 100 mph forest drive: tick 3.5 -> 1.65 ms a frame on average, 50 -> 54 fps)
M.EVERY_N = 3
local frameN = 0
-- view: the camera's {x, y} when it is away from the car (core: VIEW_AWAY_M), else nil: what is shown follows what the
-- player looks at, the colliding items stay round the car (the user, 2026-10-20: "the LODs still all follow the car").
-- Trees give way to the governor's heavy ops (Felix 2026-10-26, "lehmig" at speed: no hitches, yet runs of 30-60 ms
-- frames): a terrain commit waits for two calm frames, and tree changes, outside the governor's budget, kept frames
-- from being calm while every far tile and road had already stopped; their engine cost also landed in the frames round
-- the commit. So no tree work while a heavy op waits, in its frame or in the quiet frames after it (gov: core's
-- governor; nil = never yield). trees.YIELD = false turns it off; M.yielded counts the frames given way.
M.YIELD = true
M.yielded = 0

function M.update(anchorId, player, view, gov)
  if not next(tiles) then return end
  if not setup() then return end
  frameN = frameN + 1
  if M.YIELD and gov and (gov.heavyPending or gov.blocked or (gov.isHeavyFrame and gov:isHeavyFrame())) then
    M.yielded = M.yielded + 1
    return
  end
  if frameN % M.EVERY_N ~= 0 then return end
  local budget = M.PER_FRAME * M.EVERY_N
  M.lastShapeItems = touchedItems        -- the previous frame's (diagnostics)
  touched, touchedItems = {}, 0
  coldBase, coldSpell = 0, 0
  expireWarm(frameN)
  releaseGhosts(anchorId, player)
  for key, t in pairs(tiles) do
    if (t.anchor ~= anchorId and not t.ghost) or t.drop then
      local f = t.forestId and scenetree.findObjectById(t.forestId)
      local fd = f and f:getData()
      local left = false
      for i, it in pairs(t.made) do
        if budget <= 0 then return end
        local sh = t.madeShape[i]
        if not sh or allow(sh) then
          if fd then removeItem(fd, it, sh) end
          t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
          budget = budget - 1
        else left = true end
      end
      if not left then
        releaseForest(t)
        tiles[key] = nil
      end
    end
  end
  if not player then return end
  -- the colliding items: by cells round the car, at speed along its path (M.cellMask)
  local car = { x = player.x, y = player.y }
  local veh = getPlayerVehicle and getPlayerVehicle(0)
  local vel = veh and veh.getVelocity and veh:getVelocity()
  local sp = vel and math.sqrt(vel.x * vel.x + vel.y * vel.y) or 0
  if sp >= M.FAST_MS then
    car.ux, car.uy, car.lead = vel.x / sp, vel.y / sp, math.max(M.SOLID_M, sp * M.SOLID_LEAD_S)
  end
  M.lastFast = car.ux ~= nil
  slowNow = sp < M.WARM_MAX_MS
  if slowNow and M.FIRST_LOADS then warmStep(player, frameN) end
  -- nearest tiles first (to the camera when it is away: view), those whose colliding items change before the rest;
  -- [3] the distance to the car (the colliding items), [4] every item solid, [5] the solid cells, [6] the class
  local order = {}
  local function boxDist(p, t)
    local dx = math.max(math.abs(p.x - t.cx) - 128, 0)
    local dy = math.max(math.abs(p.y - t.cy) - 128, 0)
    return math.sqrt(dx * dx + dy * dy)
  end
  -- solid round other players' cars too (collisionfoci.lua: the foci with trees; traffic has none)
  local function nearFocus(t)
    for _, f in ipairs(foci) do
      if f.trees and boxDist(f, t) <= M.SOLID_M + (t.near and M.SOLID_HYST_M or 0) then return true end
    end
    return false
  end
  -- LOOKAHEAD: the point LOOK_S ahead on the car's line, and the budget's order (pathKey) at speed
  local ahead = nil
  if M.LOOKAHEAD and car.ux and not view then
    local look = math.min(M.LOOK_MAX_M, sp * M.LOOK_S)
    ahead = { x = player.x + car.ux * look, y = player.y + car.uy * look }
  end
  local function pathKey(t, d)
    local k = d
    if ahead then
      local dx, dy = t.cx - player.x, t.cy - player.y
      local along = dx * car.ux + dy * car.uy
      local r = math.sqrt(dx * dx + dy * dy)
      if along < -128 then k = d * M.BEHIND_K + M.BEHIND_M
      elseif r > 181 and along < r * M.VIEW_COS then k = d * M.SIDE_K end
    end
    return k
  end
  for key, t in pairs(tiles) do
    if t.anchor ~= anchorId or t.drop then goto continue end     -- still being emptied
    local dCar = boxDist(player, t)
    local all, mask
    if not t.ghost then
      all, mask = nearFocus(t), M.cellMask(t, car)
    else                                -- (a ghost's rows are in the old frame: by its tile, as before)
      all = dCar <= M.SOLID_M or (t.near and dCar <= M.SOLID_M + M.SOLID_HYST_M) or nearFocus(t) or false
      mask = ''
    end
    -- what collides changes first: items to turn back, then new solid ones, then what is only drawn
    local class = 2
    if (all and not t.all) or (not all and M.cellsOff(mask, t.mask or '')) then class = 0
    elseif (t.all and not all) or (not all and M.cellsOff(t.mask or '', mask)) then class = 1 end
    local dShow = view and math.min(boxDist(view, t), dCar) or dCar
    if ahead and not t.ghost then dShow = math.min(dShow, boxDist(ahead, t)) end
    if t.pending and t.pendingClass and t.pendingClass < class then class = t.pendingClass end
    -- [7] the budget's order within a class: the distance shown, ahead first at speed (pathKey)
    order[#order + 1] = { key, dShow, dCar, all, mask, class, pathKey(t, dShow) }
    ::continue::
  end
  table.sort(order, function(a, b)
    if a[6] ~= b[6] then return a[6] < b[6] end
    return a[7] < b[7]
  end)
  local nearMade, urgentMade = false, false
  local urgentR2 = math.max(M.URGENT_MIN_M, sp * M.URGENT_S) ^ 2
  local rowsScanned = 0
  local function finish()
    if nearMade then
      local ok, roads = pcall(require, 'beamngEarth/roads')
      if ok and roads.requestCollision then roads.requestCollision(urgentMade) end
    end
  end
  for _, o in ipairs(order) do
    local t = tiles[o[1]]
    local fd = nil                          -- the tile's Forest data, made when it first needs an item
    local function data() fd = fd or forest(t):getData(); return fd end
    local want = tileThreshold(o[2], t)
    local band = groupBand(t, o[2])
    local all, mask = o[4], o[5]
    local near = all or mask ~= ''
    if want ~= t.shown or band ~= t.band or all ~= t.all or mask ~= (t.mask or '') or t.pending then
      -- only solid cells changed: those cells' rows and the ones left over (t.pendingRows), else every row
      local idxs = nil
      if want == t.shown and band == t.band and all == t.all and (not t.pending or t.pendingRows) then
        idxs = M.changedRows(t, t.mask or '', mask, t.pending and t.pendingRows or nil, car)
      end
      if rowsScanned > M.ROWS_PER_FRAME then finish(); return end
      -- (the tile's state is what it shows once its rows are done: the rows not done yet are its pendingRows)
      t.pending, t.pendingRows, t.pendingClass = false, nil, nil
      t.all, t.mask, t.shown, t.band, t.near = all, mask, want, band, near
      local left = {}
      local cells = mask ~= '' and not all and M.cellsOf(t) or nil
      local nrows = idxs and #idxs or t.n
      local f = t.flat
      rowsScanned = rowsScanned + nrows
      for k = 1, nrows do
        local i = idxs and idxs[k] or k
        if budget <= 0 then          -- (the rest another frame)
          for k2 = k, nrows do left[#left + 1] = idxs and idxs[k2] or k2 end
          break
        end
        local j = (i - 1) * ROW_N
        local r = f[j + 8]
        local g = (t.groups or GROUPS)[f[j + 1] + 1]
        local cap = g and M.GROUP_MAX_M[g]
        if cap and o[2] > cap then r = 2 end     -- this group is not shown this far out
        -- a group this mod has no shapes for (a newer server's): left out, not drawn as a tree
        if g and not M.CATALOG[g] then
          r = 2
          if not M.unknownGroups[g] then M.unknownGroups[g] = true; log('W', 'beamngEarth.trees', 'no shapes for group ' .. g) end
        end
        -- an item something else stands in for (navgraph.lua's switchable signal heads)
        if g and M.suppress and M.suppress(g, f[j + 2], f[j + 3]) then r = 2 end
        local solid = (all or (cells ~= nil and mask:byte(cells[i]) == 49)) and not M.NO_COLLIDE[g or '']
        if r < want and (not t.made[i] or (t.solid[i] == true) ~= solid) then
          local sh, sc = shapeOf(t, i)
          local body = M.FIRST_LOADS and vienna.body(g)
          body = body and M.variant(body, t.variant)
          if body and not loaded[body] then     -- (a sign's body first, alone: the face another frame)
            if coldOk(body) then warmShape(body, player, frameN) end
            left[#left + 1] = i
          elseif allow(sh) then
            if t.made[i] then removeItem(data(), t.made[i], t.madeShape[i]); budget = budget - 1 end  -- solid <-> not
            t.made[i] = makeItem(data(), f, j, sh, sc, solid, g, t.variant)
            t.solid[i] = (t.made[i] and solid) or nil      -- true or nil
            t.madeShape[i] = t.made[i] and sh or nil
            if solid then
              nearMade = true
              local dx, dy = f[j + 2] - car.x, f[j + 3] - car.y
              if not urgentMade and dx * dx + dy * dy <= urgentR2 then urgentMade = true end
            end
            budget = budget - 1
          else left[#left + 1] = i end            -- another frame (this one's shapes are full)
        elseif r >= want and t.made[i] then
          if allow(t.madeShape[i] or '') then
            removeItem(data(), t.made[i], t.madeShape[i])
            t.made[i], t.solid[i], t.madeShape[i] = nil, nil, nil
            budget = budget - 1
          else left[#left + 1] = i end
        end
      end
      if #left > 0 then t.pending, t.pendingRows, t.pendingClass = true, left, math.min(o[6], t.pendingClass or 2) end
      if budget <= 0 then finish(); return end
    end
  end
  finish()
end

-- Disconnect, a new server session, mod unload: every item removed.
function M.clear()
  for _, t in pairs(tiles) do
    local f = t.forestId and scenetree.findObjectById(t.forestId)
    local fd = f and f:getData()
    for i, it in pairs(t.made) do if fd then removeItem(fd, it, t.madeShape[i]) end end
    releaseForest(t)
  end
  expireWarm(frameN, true)
  tiles, frameN = {}, 0
end

-- Level end: the level deletes its objects itself.
function M.reset()
  tiles, types, ready, pool, forestCount, pop = {}, {}, false, {}, 0, {}
  loaded, loadedBase, warmBases, warmSpells, warmQueued, warmItems, warmForestId = {}, {}, {}, {}, {}, {}, nil
  coldLast, M.coldRecent, clock, M.coldLoads, M.warmLoads = nil, {}, nil, 0, 0
end

function M.status()
  local n, items, pending = 0, 0, 0
  for _, t in pairs(tiles) do
    n = n + 1
    if t.pending or t.shown == 0 then pending = pending + 1 end
    for _ in pairs(t.made) do items = items + 1 end
  end
  -- fresh: shapes loaded for the first time within the last 0.5 s (their cost lands in the engine's next ticks)
  local fresh, t0 = 0, now()
  for _, at in ipairs(M.coldRecent) do if t0 - at <= 500 then fresh = fresh + 1 end end
  return { tiles = n, items = items, pending = pending, made = M.made, removed = M.removed, errors = M.errors,
    fresh = fresh, shapes = M.coldLoads, warmLeft = #warmBases + #warmSpells }
end

return M
