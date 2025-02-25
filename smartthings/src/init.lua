local capabilities = require('st.capabilities')
local Driver = require('st.driver')
local log = require('st.log')
local json = require('dkjson')
local cosock = require('cosock')
local http = cosock.asyncify('socket.http')
local ltn12 = require('ltn12')

-- Device configuration
local REFRESH_INTERVAL = 300 -- 5 minutes

-- Bridge communication functions
local function build_bridge_url(device)
    local addr = device.preferences.bridgeAddress or 'localhost'
    local port = device.preferences.bridgePort or 5000
    return string.format('http://%s:%s', addr, port)
end

local function make_request(device, path, method)
    local url = build_bridge_url(device) .. path
    local response_body = {}
    
    local success, code = http.request{
        url = url,
        method = method or 'GET',
        sink = ltn12.sink.table(response_body),
        headers = {
            ['Content-Type'] = 'application/json',
        }
    }
    
    if success and code == 200 then
        local body = table.concat(response_body)
        local decoded = json.decode(body)
        return decoded
    else
        log.error(string.format('[%s] Request failed: %s, code: %s', device.label, url, code))
        return nil
    end
end

-- Device status update
local function update_device_status(device, status)
    if status.battery then
        -- Convert voltage to percentage (assuming 6V max, 3.5V min)
        local battery_pct = math.floor(((status.battery - 3.5) / 2.5) * 100)
        battery_pct = math.max(0, math.min(100, battery_pct))
        device:emit_event(capabilities.battery.battery(battery_pct))
    end
    
    if status.connected ~= nil then
        if status.connected then
            device:online()
        else
            device:offline()
        end
    end
    
    if status.food_low ~= nil then
        device:emit_event(capabilities.switch.switch.off())
    end
end

-- Capability handlers
local function handle_refresh(driver, device)
    log.info(string.format('[%s] Refreshing device status', device.label))
    
    local status = make_request(device, '/status/' .. device.device_network_id)
    if status then
        update_device_status(device, status)
    end
end

local function handle_feed(driver, device)
    log.info(string.format('[%s] Manual feed triggered', device.label))
    
    -- Get portion size from preferences
    local portions = device.preferences.portionSize or 1
    
    local result = make_request(
        device, 
        string.format('/feed/%s?portions=%d', device.device_network_id, portions),
        'POST'
    )
    
    if result and result.status == 'success' then
        -- Emit temporary "on" state
        device:emit_event(capabilities.switch.switch.on())
        
        -- Schedule switch to turn off after 2 seconds
        device.thread:call_with_delay(2, function()
            device:emit_event(capabilities.switch.switch.off())
        end)
        
        -- Refresh status after feeding
        device.thread:call_with_delay(5, function()
            handle_refresh(driver, device)
        end)
    end
end

-- Lifecycle handlers
local function device_init(driver, device)
    log.info(string.format('[%s] Initializing device', device.label))
    
    -- Set initial state
    device:emit_event(capabilities.switch.switch.off())
    
    -- Initial refresh
    handle_refresh(driver, device)
    
    -- Schedule regular updates
    device.thread:call_on_schedule(
        REFRESH_INTERVAL,
        function()
            handle_refresh(driver, device)
        end
    )
end

local function device_added(driver, device)
    log.info(string.format('[%s] Adding device', device.label))
    device_init(driver, device)
end

local function device_doconfigure(driver, device)
    log.info(string.format('[%s] Configuring device', device.label))
    -- Validate bridge connection
    local status = make_request(device, '/status/' .. device.device_network_id)
    if status then
        device:online()
        return true
    end
    return false
end

-- Driver definition
local driver = Driver('PetSafe-Feeder', {
    discovery = nil,  -- Manual device addition only
    lifecycle_handlers = {
        init = device_init,
        added = device_added,
        doConfigure = device_doconfigure,
    },
    capability_handlers = {
        [capabilities.refresh.ID] = {
            [capabilities.refresh.commands.refresh.NAME] = handle_refresh,
        },
        [capabilities.switch.ID] = {
            [capabilities.switch.commands.on.NAME] = handle_feed,
        },
    },
})

log.info('Starting PetSafe Feeder driver')
driver:run()