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

function _G.preview(path, w, h)
	if path == '' then return end
	sai.mode = 'viewer'
	if sai.viewer.get_image().path ~= path then
		pcall(sai.viewer.go, path)
	end
	if w then
		local res = sai.get_window_size()
		if w ~= res.width or h ~= res.height then
			sai.set_window_size(w, h)
		end
	end
end

sai.eventloop.subscribe {
	event = 'SwiEnter',
	once = true,
	callback = function() require('sai.bridge.ipc').server() end,
}
