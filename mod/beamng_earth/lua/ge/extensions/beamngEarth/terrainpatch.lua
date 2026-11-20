-- Ground under other vehicles' collision islands off the terrain block (BeamMP support, the user, 2026-10-22: "As cars
-- spread apart they can have their own island of collision"). The engine collides with one TerrainBlock only, and the
-- block follows the active car, so a car far from it had no ground. For each island of collisionfoci.lua that is not
-- the active car's and does not lie well inside the block, the server builds a collision-only shape of the same ground
-- (server terrainpatch.py, 'terrain_patch'); it goes in as a TSStatic that draws nothing, and out KEEP_S after no
-- island wants it.
local M = {}

M.MARGIN_M = 64         -- an island whose box lies this far inside the block needs no patch
M.SNAP_M = 64           -- as the server's terrainpatch.SNAP_M / MAX_SIDE_M: the keys match its boxes
M.MAX_SIDE_M = 1280
M.KEEP_S = 10
M.MAX_INFLIGHT = 2
M.RETRY_S = 5
M.UPDATE_S = 1

local patches = {}      -- key -> { box, id, pending, wantedAt, retryAt }
local anchorId = nil
local since = math.huge
local clock = 0

-- Pure: the island box snapped as the server does (terrainpatch.snap_box): clipped to MAX_SIDE_M round its centre,
-- out to SNAP_M. x0, y0, x1, y1
function M.snap(x0, y0, x1, y1)
  local cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
  local hw, hh = math.min((x1 - x0) / 2, M.MAX_SIDE_M / 2), math.min((y1 - y0) / 2, M.MAX_SIDE_M / 2)
  return math.floor((cx - hw) / M.SNAP_M) * M.SNAP_M, math.floor((cy - hh) / M.SNAP_M) * M.SNAP_M,
    math.ceil((cx + hw) / M.SNAP_M) * M.SNAP_M, math.ceil((cy + hh) / M.SNAP_M) * M.SNAP_M
end

-- Pure: key -> box of the patches wanted: islands (collisionfoci.islands with the active car as its first member,
-- marked active) that are not the active car's and not well inside the block box b {x0, y0, x1, y1} (nil: none).
function M.wanted(islands, b)
  local out = {}
  for _, isl in ipairs(islands) do
    local hasActive = false
    for _, m in ipairs(isl.members) do if m.active then hasActive = true end end
    local inside = b and isl.x0 >= b[1] + M.MARGIN_M and isl.y0 >= b[2] + M.MARGIN_M
      and isl.x1 <= b[3] - M.MARGIN_M and isl.y1 <= b[4] - M.MARGIN_M
    if not hasActive and not inside then
      local x0, y0, x1, y1 = M.snap(isl.x0, isl.y0, isl.x1, isl.y1)
      local key = (string.format('%d_%d_%d_%d', x0, y0, x1, y1):gsub('-', 'm'))
      out[key] = { x0, y0, x1, y1 }
    end
  end
  return out
end

local function deletePatch(p)
  if p.id then
    local o = scenetree.findObjectById(p.id)
    if o then o:delete() end
    p.id = nil
    local ok, roads = pcall(require, 'beamngEarth/roads')
    if ok and roads.requestCollision then roads.requestCollision() end
  end
end

-- Everything out (an anchor change: the old frame's patches are in the wrong place; the level's end).
function M.clear()
  for _, p in pairs(patches) do deletePatch(p) end
  patches = {}
end

-- Each frame (core.lua): aid the active anchor, foci (collisionfoci.last), active { x, y } (the active car), block the
-- terrain block's box {x0, y0, x1, y1} or nil; send(msg) to the server.
function M.update(dt, aid, foci, active, block, send)
  clock = clock + (dt or 0)
  since = since + (dt or 0)
  if since < M.UPDATE_S then return end
  since = 0
  if aid ~= anchorId then M.clear(); anchorId = aid end
  if not aid then return end
  local cf = require('beamngEarth/collisionfoci')
  local act = active and { x = active.x, y = active.y, r = cf.PLAYER_R, active = true } or nil
  local want = M.wanted(cf.islands(foci or {}, act), block)
  local inflight = 0
  for _, p in pairs(patches) do if p.pending then inflight = inflight + 1 end end
  for key, box in pairs(want) do
    local p = patches[key]
    if not p then p = { box = box }; patches[key] = p end
    p.wantedAt = clock
    if not p.id and not p.pending and inflight < M.MAX_INFLIGHT and clock >= (p.retryAt or 0) then
      p.pending = true
      inflight = inflight + 1
      send({ type = 'terrain_patch', anchorId = aid, key = key, x0 = box[1], y0 = box[2], x1 = box[3], y1 = box[4] })
    end
  end
  for key, p in pairs(patches) do
    if not want[key] and clock - (p.wantedAt or 0) > M.KEEP_S then
      deletePatch(p)
      patches[key] = nil
    end
  end
end

-- The server's 'terrain_patch': { anchorId, key, path, x, y } or { anchorId, key, error }.
function M.onPatch(msg)
  local p = patches[msg.key]
  if not p or msg.anchorId ~= anchorId then return end
  p.pending = false
  if type(msg.path) ~= 'string' then
    p.retryAt = clock + M.RETRY_S
    return
  end
  deletePatch(p)
  local o = createObject('TSStatic')
  o:setField('shapeName', 0, msg.path)
  o:setField('collisionType', 0, 'Collision Mesh')
  o:setField('decalType', 0, 'None')
  o:setField('dynamic', 0, 'true')        -- (no redraw of the cached sun shadow map: farfield.lua spawn)
  o.canSave = false
  o:setPosition(vec3(tonumber(msg.x) or 0, tonumber(msg.y) or 0, 0))
  o:registerObject('')
  scenetree.MissionGroup:addObject(o)
  p.id = o:getID()
  local ok, roads = pcall(require, 'beamngEarth/roads')
  if ok and roads.requestCollision then roads.requestCollision() end
end

-- (tests, the dev panel) key -> { box, id, pending }
function M.state() return patches end

return M
