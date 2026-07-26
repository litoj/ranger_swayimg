-- Swayimg config for ranger image previews.
-- Loaded by ranger via `swayimg --config <this_file>`.
-- Communicates with ranger through a command file + SIGUSR1.
--
-- The Lua layer only handles image display and window sizing.
-- All compositor window management (positioning, hiding, focus) is
-- handled by the Python backend layer.
--
local cmd_file = os.getenv 'RANGER_SWAYIMG_CMD'
if not cmd_file then
	print 'RANGER_SWAYIMG_CMD not set'
	return swayimg.exit(1)
end

-- Extend package.path so modules from the swayimg config directory
-- (e.g. sai.swi, user plugins) can be required.
local swi_config = os.getenv 'XDG_CONFIG_HOME' or (os.getenv 'HOME' .. '/.config')
package.path = swi_config .. '/swayimg/?.lua;' .. package.path

require 'init'

sai.overlay = true
sai.decoration = false
sai.antialiasing = false
sai.viewer.default_scale = 'fit'
sai.imagelist.adjacent = true
sai.imagelist.recursive = false
sai.imagelist.fsmon = false

local function handle_command()
	local f = io.open(cmd_file, 'r')
	if not f then return end
	local action = f:read '*l'
	local path = f:read '*l' or ''
	local size = f:read '*l' or ''
	f:close()

	if action == 'show' then
		if path == '' then return end

		sai.mode = 'viewer'
		sai.viewer.go(path)

		if size ~= '' then
			local w, h = size:match '^(%d+),(%d+)$'
			if w then sai.set_window_size(tonumber(w), tonumber(h)) end
		end
	elseif action == 'exit' then
		sai.exit()
	end
end

sai.eventloop.subscribe { event = 'SwiEnter', once = true, callback = handle_command }
sai.eventloop.subscribe { event = 'Signal', match = 'USR1', callback = handle_command }
