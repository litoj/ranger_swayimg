local socket_path = os.getenv 'RANGER_SWAYIMG_SOCKET'
if not socket_path then
	print 'RANGER_SWAYIMG_SOCKET not set'
	return swayimg.exit(1)
end

-- Extend package.path so modules from the swayimg config directory
-- (e.g. sai.swi, sai.lib.ipc, user plugins) can be required.
local swi_config = os.getenv 'XDG_CONFIG_HOME' or (os.getenv 'HOME' .. '/.config')
package.path = swi_config .. '/swayimg/?.lua;' .. package.path

require 'init' -- load the base config

sai.overlay = true
sai.decoration = false
sai.antialiasing = false
sai.viewer.default_scale = 'fit'
sai.imagelist.adjacent = true -- load the files that are likely to get previewed
sai.imagelist.recursive = false
sai.imagelist.fsmon = false
sai.imagelist.order = 'none'

function _G.preview(path, w, h)
	if path == '' then return end
	sai.mode = 'viewer'
	sai.viewer.go(path)
	if w and h then
		sai.set_window_size(w, h)
	end
end

sai.eventloop.subscribe {
	event = 'SwiEnter',
	once = true,
	callback = function() require('sai.lib.ipc').server(socket_path) end,
}
