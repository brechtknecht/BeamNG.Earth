-- luajit mod/tests/test_terrainpatch.lua  (ground under other vehicles' collision islands, terrainpatch.lua): the boxes
-- snapped as the server's terrainpatch.snap_box, the islands that need a patch (not the active car's, not well inside the
-- terrain block), and update asking the server for them (at most MAX_INFLIGHT at a time) and letting them go.
local dir = (arg[0]:match('^(.*)[/\\]') or '.')
local T = dofile(dir .. '/testlib.lua')
dofile(dir .. '/stubs.lua')
local tp = require('beamngEarth/terrainpatch')
local cf = require('beamngEarth/collisionfoci')

-- snap: out to the 64 m grid, clipped to MAX_SIDE_M round the centre (as the server)
local x0, y0, x1, y1 = tp.snap(10, -70, 650, 570)
T.check(x0 == 0 and y0 == -128 and x1 == 704 and y1 == 576, 'snapped out: ' .. table.concat({ x0, y0, x1, y1 }, ' '))
x0, y0, x1, y1 = tp.snap(0, 0, 4000, 100)
T.check(x1 - x0 <= tp.MAX_SIDE_M + 2 * tp.SNAP_M and x1 - x0 >= tp.MAX_SIDE_M, 'clipped to the max side (then snapped out)')

-- wanted: the active car's island never; an island well inside the block never; a far one yes
local block = { -512, -512, 512, 512 }
local foci = cf.compute({ { id = 1, x = 0, y = 0, kind = 'player' }, { id = 2, x = 2000, y = 0, kind = 'player' },
  { id = 3, x = 200, y = 100, kind = 'traffic' } }, 1, 0, 0)
local isl = cf.islands(foci, { x = 0, y = 0, r = cf.PLAYER_R, active = true })
local w = tp.wanted(isl, block)
local keys = {}
for k in pairs(w) do keys[#keys + 1] = k end
T.check(#keys == 1, 'one patch (the far car): ' .. table.concat(keys, ','))
local box = w[keys[1]]
T.check(box[1] <= 2000 - cf.PLAYER_R and box[3] >= 2000 + cf.PLAYER_R, 'the patch covers the far car\'s island')
T.check(not keys[1]:find('-', 1, true), 'keys carry no minus sign (the server\'s key pattern)')
-- a lone traffic car well inside the block: none
local inside = cf.islands(cf.compute({ { id = 3, x = 200, y = 100, kind = 'traffic' } }, nil), nil)
T.check(next(tp.wanted(inside, block)) == nil, 'well inside the block: no patch')
-- no block (the ground not in yet): every island off the active car's
T.check(next(tp.wanted(isl, nil)) ~= nil, 'no block: the far car still gets one')

-- update: asks the server, at most MAX_INFLIGHT at a time; a reply's error retries later; unwanted ones go
local sent = {}
local send = function(m) sent[#sent + 1] = m end
local many = {}
for i = 1, 5 do many[#many + 1] = { id = 10 + i, x = i * 3000, y = 0, kind = 'player' } end
many[#many + 1] = { id = 1, x = 0, y = 0, kind = 'player' }
local f = cf.compute(many, 1, 0, 0)
tp.update(10, 7, f, { x = 0, y = 0 }, block, send)
T.check(#sent == tp.MAX_INFLIGHT, 'at most MAX_INFLIGHT asked at once: ' .. #sent)
T.check(sent[1].type == 'terrain_patch' and sent[1].anchorId == 7 and sent[1].x1 > sent[1].x0, 'the request\'s fields')
tp.onPatch({ anchorId = 7, key = sent[1].key, error = 'offline' })
tp.update(1.5, 7, f, { x = 0, y = 0 }, block, send)
T.check(#sent == tp.MAX_INFLIGHT + 1, 'one slot freed by the error: one more asked: ' .. #sent)
local errored = tp.state()[sent[1].key]
T.check(errored and not errored.pending and errored.retryAt, 'the failed one waits before asking again')
-- all far cars gone: their patches go after KEEP_S
tp.update(1.5, 7, {}, { x = 0, y = 0 }, block, send)
T.check(next(tp.state()) ~= nil, 'kept a while')
tp.update(tp.KEEP_S + 2, 7, {}, { x = 0, y = 0 }, block, send)
T.check(next(tp.state()) == nil, 'let go after KEEP_S')
-- an anchor change clears everything
tp.update(10, 7, f, { x = 0, y = 0 }, block, send)
tp.update(10, 8, {}, { x = 0, y = 0 }, block, send)
T.check(next(tp.state()) == nil, 'anchor change: cleared')

T.done('test_terrainpatch')
