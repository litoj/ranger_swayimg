-- Extend package.path so modules from the swayimg config directory
-- (e.g. sai.swi, sai.lib.ipc, user plugins) can be required.
local swi_config = os.getenv 'XDG_CONFIG_HOME' or (os.getenv 'HOME' .. '/.config')
package.path = swi_config .. '/swayimg/?.lua;' .. package.path

require 'init' -- load the base config

sai.overlay = true
sai.decoration = false
sai.antialiasing = false
sai.viewer.default_scale = 'fit'
sai.imagelist.adjacent = false -- preloading neighbours starves the actual preview decode
sai.imagelist.recursive = false
sai.imagelist.fsmon = false
sai.imagelist.order = 'none'

local dbg_path = os.getenv 'SWAYIMG_DEBUG_LOG'
local function dbg(...)
	if not dbg_path then return end
	local parts = {}
	for i = 1, select('#', ...) do parts[#parts + 1] = tostring(select(i, ...)) end
	local now = os.clock()
	local f = io.open(dbg_path, 'a')
	if f then
		f:write(string.format('%.3f [lua] %s\n', now, table.concat(parts, ' ')))
		f:close()
	end
end

function _G.preview(path, w, h)
	if path == '' then return end
	dbg('preview():', path, w, h)
	sai.mode = 'viewer'
	if sai.viewer.get_image().path ~= path then
		local ok, err = pcall(sai.viewer.go, path)
		dbg('viewer.go ok=', ok, 'err=', err)
	end
	if w then
		local res = sai.get_window_size()
		if w ~= res.width or h ~= res.height then
			dbg('set_window_size:', w, 'x', h)
			sai.set_window_size(w, h)
		end
	end
end

dbg('lua init done')

sai.eventloop.subscribe {
	event = 'SwiEnter',
	once = true,
	callback = function() require('sai.lib.ipc').server() end,
}
