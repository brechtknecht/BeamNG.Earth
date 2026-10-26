-- luajit mod/tests/test_perflog.lua  (the performance log's hitches, drops and minute lines)
local T = dofile((arg[0]:match('^(.*)[/\\]') or '.') .. '/testlib.lua')
local P = require('beamngEarth/perflog')
local now, lines = 0, {}
local ctx = function() return { lat = 39.28345, lon = -76.59341, mph = 52, block = '1024,512', doing = 'far tiles 3 queued' } end
local function run(seconds, ms, hitch, mod)
  local t_end = now + seconds
  while now < t_end do
    now = now + ms / 1000
    for _, l in ipairs(P.frame(ms, now, ctx, hitch, mod, function() return { Render = ms * 0.5, Lua = 2 } end)) do
      lines[#lines + 1] = l
    end
  end
end
local function find(prefix)
  local out = {}
  for _, l in ipairs(lines) do if l:sub(1, #prefix) == prefix then out[#out + 1] = l end end
  return out
end
run(30, 1000 / 60)
T.check(#lines == 0, 'steady 60 fps: nothing logged')
run(10, 50, nil, { roads = 20 })
local d = find('perf drop')
T.check(#d == 1, 'a drop to 20 fps: one line')
T.check(d[1] and d[1]:find('^perf drop 20 fps') and d[1]:find('usual 60', 1, true) and d[1]:find('at 39.28345,-76.59341', 1, true)
  and d[1]:find('52 mph', 1, true) and d[1]:find('roads', 1, true) and d[1]:find('Render', 1, true)
  and d[1]:find('doing: far tiles', 1, true), 'its fps, the usual rate, where, the mod and engine parts, what ran')
run(10, 1000 / 60)
local b = find('perf back')
T.check(#b == 1 and b[1]:find('^perf back to 60 fps after') and b[1]:find('lowest 20', 1, true), 'back: how long, the lowest')
lines = {}
now = now + 0.3
local got = P.frame(300, now, ctx, { mod = { terrainCommit = 280 }, engine = { RenderShadows = 250, Lua = 20 } }, nil, nil)
T.check(#got == 1 and got[1]:find('^perf hitch 300 ms at 39.28345') and got[1]:find('mod: terrainCommit 280', 1, true)
  and got[1]:find('engine: RenderShadows 250', 1, true), 'a hitch: where, the mod part and the engine subsystem')
got = P.frame(200, now + 0.2, ctx, { mod = {}, engine = {} }, nil, nil)
T.check(#got == 0, 'another within HITCH_GAP_S: counted, not a line')
now = now + 3
got = P.frame(150, now, ctx, { mod = {}, engine = {} }, nil, nil)
T.check(#got == 1 and got[1]:find('1 more hitches', 1, true), 'the next line says how many were skipped')
run(70, 1000 / 60)
local m = find('perf minute')
T.check(#m >= 1 and m[1]:find('^perf minute %d+ fps, 1%% low') and m[1]:find('3 hitches', 1, true)
  and m[1]:find('worst: terrainCommit 280', 1, true), 'the minute line: fps, 1 % low, hitches, the worst one\'s cause')
T.check(P.cause(1600, { mod = {}, engine = { FPSLimiter = 13 } }) == P.UNMEASURED,
  'a 1.6 s frame with 13 ms measured: not blamed on that part')
T.check(P.cause(300, { mod = { terrainCommit = 280 }, engine = {} }) == 'terrainCommit 280', 'a part that explains it: named')
T.check(#P.frame(0 / 0, now, ctx) == 0 and #P.frame(-1, now, ctx) == 0, 'bad frame times ignored')
-- pacing: slow frames under a hitch (Felix's "lehmig" 2026-10-26), their runs, who was in them
P.reset(); lines = {}
for _ = 1, 30 * 60 do now = now + 1 / 60; for _, l in ipairs(P.frame(1000 / 60, now, ctx, nil, { roads = 0.5 }, nil)) do lines[#lines + 1] = l end end
local k = 0
while #find('perf pacing') == 0 and k < 10000 do
  k = k + 1
  local slow = (k % 20 == 0) or (k % 200 >= 100 and k % 200 < 103)     -- lone slow frames, and runs of 3
  local ms = slow and 40 or 1000 / 60
  now = now + ms / 1000
  local e = function() return { Render = slow and 30 or 8 } end
  for _, l in ipairs(P.frame(ms, now, ctx, nil, slow and { ['sea+trees'] = 12, roads = 0.5 } or { roads = 0.5 }, e)) do
    lines[#lines + 1] = l
  end
end
local pc = find('perf pacing')[1]
T.check(pc ~= nil, 'a pacing line with the minute')
T.check(pc and pc:find('p50 16.7', 1, true) and pc:find('p99 40.0', 1, true), 'its percentiles: ' .. tostring(pc))
T.check(pc and pc:find('runs of 3%+') and not pc:find(' 0 runs', 1, true), 'runs of slow frames counted')
T.check(pc and pc:find('mod in slow frames %(ms%): sea%+trees 12') and pc:find('engine in slow frames %(ms%): Render 30'),
  'the mod\'s and the engine\'s parts in the slow frames')
T.check(pc and pc:find('mod per frame %(ms%): ') ~= nil, 'the mod\'s mean per frame')
T.done('test_perflog')
