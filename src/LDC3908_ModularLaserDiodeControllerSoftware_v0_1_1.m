function LDC3908_ModularLaserDiodeControllerSoftware_v0_1_1()
% LDC3908_ModularLaserDiodeControllerSoftware_v0_1_1
% A fully graphical interface for the Newport LDC-3908 controller
% Written by Zev Granowitz in collaboration with  in 
% 4/30/2026


%% Setup UI Window
fig = uifigure('Name', 'LDC-3908 Modular Laser Diode Controller Software', 'Position', [100, 100, 1400, 800]);

% Shared Application State Variables
s = []; % Serial port object holder
num_channels = 8; % Maximum defined slots in chassis
is_executing = false; % Execution locking safety
is_scanning = false; % Scanning lockout
is_stop_requested = false; % Hardware halt flag
is_simulated = false; % Hardware mockup routing flag
telemetry_timer = []; % Background update timer
is_telemetry_running = false; % Prevent re-entry into updateTelemetry
active_profile_path = ''; % Tracks currently loaded/saved profile
total_estimated_time = 0; % ETA tracking
sequence_start_time = []; % ETA tracking
is_closing = false; % Graceful shutdown flag


% --- Simulation Mock State ---
% 1=Installed/Good, 2=Installed/NoLaser(T<0), 0=Empty Slot
sim_state = struct();
sim_state.curr_chan = 1;
sim_state.T_actual = num2cell(22 * ones(1, 8));
sim_state.I_actual = num2cell(zeros(1, 8));
sim_state.TEC_ON = num2cell(zeros(1, 8));
sim_state.LAS_ON = num2cell(zeros(1, 8));
sim_state.LAS_MOD = num2cell(ones(1, 8));
sim_state.is_installed = num2cell([1 1 0 2 1 0 0 0]);
sim_query_response = '';

% Define Master Layout Grid
maingrid = uigridlayout(fig, [3, 1]);
maingrid.RowHeight = {60, '1x', 150};

%% --- Top Panel: Connection ---
topPanel = uipanel(maingrid);
topGrid = uigridlayout(topPanel, [1, 7]);
topGrid.ColumnWidth = {100, 150, 100, 130, 100, '1x', 200};

lbl1 = uilabel(topGrid, 'Text', 'COM Port:'); lbl1.Layout.Column = 1;

% Fetch dynamic list of COM ports available on Windows USB stack
avail_ports = cellstr(serialportlist("available"));
avail_ports{end+1} = 'Demo Simulator'; % Inject simulator node physically into list
comDropdown = uidropdown(topGrid, 'Items', avail_ports);
comDropdown.Layout.Column = 2;
if length(avail_ports) > 1
    % Default to first physically installed COM port if available, else Simulator
    comDropdown.Value = avail_ports{1};
end

btnConnect = uibutton(topGrid, 'Text', 'Connect', 'ButtonPushedFcn', @connectSerial, 'Tooltip', 'Establish connection to the selected port.'); btnConnect.Layout.Column = 3;
btnScan = uibutton(topGrid, 'Text', 'Scan Channels', 'ButtonPushedFcn', @scanChannels, 'Enable', 'off', 'Tooltip', 'Interrogate controller for active cards and reset status.'); btnScan.Layout.Column = 4;

btnClearFaults = uibutton(topGrid, 'Text', 'Clear Faults', 'FontColor', [0.6 0 0], 'ButtonPushedFcn', @clearFaults, 'Enable', 'off', 'Tooltip', 'Send *CLS to clear latched hardware faults.');
btnClearFaults.Layout.Column = 5;

statusLabel = uilabel(topGrid, 'Text', 'Status: Disconnected', 'FontColor', 'red', 'FontWeight', 'bold'); statusLabel.Layout.Column = [6 7];


% lblBranding = uilabel(topGrid, 'Text', 'Hanson Research Group', 'FontColor', [140 21 21]/255, 'FontWeight', 'bold', 'FontAngle', 'italic', 'HorizontalAlignment', 'right', 'FontSize', 16);
% lblBranding.Layout.Column = 7;

%% --- Middle Panel: Channels Grid ---
chanPanel = uipanel(maingrid, 'Title', 'Individual Channel Configuration & Live Telemetry');
chanGrid = uigridlayout(chanPanel, [num_channels + 3, 16]);
chanGrid.ColumnWidth = {35, 50, 100, 25, '1x', 65, 65, 75, 75, 75, 75, 75, 45, 75, 45, 80};
chanGrid.RowHeight = repmat({32}, 1, num_channels + 3);

% Draw Headers (with adjusted terminology)
lblH1 = uilabel(chanGrid, 'Text', 'Ch.', 'FontWeight', 'bold'); lblH1.Layout.Column = 1;
lblH2 = uilabel(chanGrid, 'Text', 'Enable', 'FontWeight', 'bold'); lblH2.Layout.Column = 2;
lblH3 = uilabel(chanGrid, 'Text', 'Label', 'FontWeight', 'bold'); lblH3.Layout.Column = 3;
% Column 4 is empty header for LED
lblH4 = uilabel(chanGrid, 'Text', 'Status', 'FontWeight', 'bold'); lblH4.Layout.Column = 5;

lblH5 = uilabel(chanGrid, 'Text', 'Live TEC', 'FontWeight', 'bold', 'FontColor', [0, 0.4, 0]); lblH5.Layout.Column = 6;
lblH6 = uilabel(chanGrid, 'Text', 'Live LAS', 'FontWeight', 'bold', 'FontColor', [0, 0.4, 0]); lblH6.Layout.Column = 7;

lblH7 = uilabel(chanGrid, 'Text', 'Live T (°C)', 'FontWeight', 'bold', 'FontColor', [0, 0.4, 0]); lblH7.Layout.Column = 8;
lblH8 = uilabel(chanGrid, 'Text', 'Live I (mA)', 'FontWeight', 'bold', 'FontColor', [0, 0.4, 0]); lblH8.Layout.Column = 9;

lblH9 = uilabel(chanGrid, 'Text', 'Target TEC', 'FontWeight', 'bold'); lblH9.Layout.Column = 10;
lblH10 = uilabel(chanGrid, 'Text', 'Target LAS', 'FontWeight', 'bold'); lblH10.Layout.Column = 11;

lblH11 = uilabel(chanGrid, 'Text', 'Target T (°C)', 'FontWeight', 'bold'); lblH11.Layout.Column = 12;
lblH12 = uilabel(chanGrid, 'Text', 'Max T', 'FontWeight', 'normal', 'FontColor', [0.5 0.5 0.5]); lblH12.Layout.Column = 13;
lblH13 = uilabel(chanGrid, 'Text', 'Target I (mA)', 'FontWeight', 'bold'); lblH13.Layout.Column = 14;
lblH14 = uilabel(chanGrid, 'Text', 'Max I', 'FontWeight', 'normal', 'FontColor', [0.5 0.5 0.5]); lblH14.Layout.Column = 15;
lblH15 = uilabel(chanGrid, 'Text', 'Action', 'FontWeight', 'bold'); lblH15.Layout.Column = 16;

% Store UI widgets dynamically per active channel
chUI = struct();
for i = 1:num_channels
    row = i + 1;
    chUI(i).label = uilabel(chanGrid, 'Text', num2str(i)); chUI(i).label.Layout.Row = row; chUI(i).label.Layout.Column = 1;
    chUI(i).enable = uicheckbox(chanGrid, 'Text', '', 'Value', false, 'Enable', 'off'); chUI(i).enable.Layout.Row = row; chUI(i).enable.Layout.Column = 2;
    chUI(i).laserLabel = uieditfield(chanGrid, 'text', 'Value', sprintf('Laser %d', i), 'Enable', 'off', 'ValueChangedFcn', @markProfileUnsaved); chUI(i).laserLabel.Layout.Row = row; chUI(i).laserLabel.Layout.Column = 3;
    chUI(i).led = uilamp(chanGrid, 'Color', [0.8 0.8 0.8]); chUI(i).led.Layout.Row = row; chUI(i).led.Layout.Column = 4;
    chUI(i).status = uilabel(chanGrid, 'Text', 'Run Scan First'); chUI(i).status.Layout.Row = row; chUI(i).status.Layout.Column = 5;

    chUI(i).liveTec = uieditfield(chanGrid, 'text', 'Value', 'OFF', 'Editable', 'off', 'BackgroundColor', [0.8 0.8 0.8], 'FontColor', 'black', 'HorizontalAlignment', 'center', 'FontWeight', 'bold'); chUI(i).liveTec.Layout.Row = row; chUI(i).liveTec.Layout.Column = 6;
    chUI(i).liveLas = uieditfield(chanGrid, 'text', 'Value', 'OFF', 'Editable', 'off', 'BackgroundColor', [0.8 0.8 0.8], 'FontColor', 'black', 'HorizontalAlignment', 'center', 'FontWeight', 'bold'); chUI(i).liveLas.Layout.Row = row; chUI(i).liveLas.Layout.Column = 7;

    % Live Telemetry Readouts
    chUI(i).curT = uieditfield(chanGrid, 'numeric', 'Value', 0, 'Editable', 'off', 'BackgroundColor', [0.9 0.9 0.9]); chUI(i).curT.Layout.Row = row; chUI(i).curT.Layout.Column = 8;
    chUI(i).curI = uieditfield(chanGrid, 'numeric', 'Value', 0, 'Editable', 'off', 'BackgroundColor', [0.9 0.9 0.9]); chUI(i).curI.Layout.Row = row; chUI(i).curI.Layout.Column = 9;

    % Targets
    chUI(i).tecCmd = uidropdown(chanGrid, 'Items', {'ON', 'OFF'}, 'Value', 'OFF', 'Enable', 'off'); chUI(i).tecCmd.Layout.Row = row; chUI(i).tecCmd.Layout.Column = 10;
    chUI(i).lasCmd = uidropdown(chanGrid, 'Items', {'ON', 'OFF'}, 'Value', 'OFF', 'Enable', 'off'); chUI(i).lasCmd.Layout.Row = row; chUI(i).lasCmd.Layout.Column = 11;

    chUI(i).tTarget = uieditfield(chanGrid, 'numeric', 'Value', 22, 'Enable', 'off', 'ValueChangedFcn', @markProfileUnsaved); chUI(i).tTarget.Layout.Row = row; chUI(i).tTarget.Layout.Column = 12;
    chUI(i).tLim = uilabel(chanGrid, 'Text', '-', 'FontColor', [0.5 0.5 0.5], 'FontSize', 11); chUI(i).tLim.Layout.Row = row; chUI(i).tLim.Layout.Column = 13;

    chUI(i).iTarget = uieditfield(chanGrid, 'numeric', 'Value', 0, 'Enable', 'off', 'ValueChangedFcn', @markProfileUnsaved); chUI(i).iTarget.Layout.Row = row; chUI(i).iTarget.Layout.Column = 14;
    chUI(i).iLim = uilabel(chanGrid, 'Text', '-', 'FontColor', [0.5 0.5 0.5], 'FontSize', 11); chUI(i).iLim.Layout.Row = row; chUI(i).iLim.Layout.Column = 15;

    chUI(i).btnExec = uibutton(chanGrid, 'Text', '▶ Run Ch.', 'Enable', 'off', 'ButtonPushedFcn', @(src, event) executeChannels(i), 'Tooltip', 'Start sequence on this channel.'); chUI(i).btnExec.Layout.Row = row; chUI(i).btnExec.Layout.Column = 16;
end

% Global Dropdown Overrides aligned directly underneath the dropdown columns
btnMasterOn = uibutton(chanGrid, 'Text', 'MASTER All ON', 'FontWeight', 'bold', 'FontColor', [0 0.4 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllSystems('ON'));
btnMasterOn.Layout.Row = num_channels + 2; btnMasterOn.Layout.Column = [8 9];

btnMasterOff = uibutton(chanGrid, 'Text', 'MASTER All OFF', 'FontWeight', 'bold', 'FontColor', [0.6 0 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllSystems('OFF'));
btnMasterOff.Layout.Row = num_channels + 3; btnMasterOff.Layout.Column = [8 9];

btnTecOn = uibutton(chanGrid, 'Text', 'TEC All ON', 'FontWeight', 'bold', 'FontColor', [0 0.4 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllDropdowns('TEC', 'ON'));
btnTecOn.Layout.Row = num_channels + 2; btnTecOn.Layout.Column = 10;

btnTecOff = uibutton(chanGrid, 'Text', 'TEC All OFF', 'FontWeight', 'bold', 'FontColor', [0.6 0 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllDropdowns('TEC', 'OFF'));
btnTecOff.Layout.Row = num_channels + 3; btnTecOff.Layout.Column = 10;

btnLasOn = uibutton(chanGrid, 'Text', 'LAS All ON', 'FontWeight', 'bold', 'FontColor', [0 0.4 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllDropdowns('LAS', 'ON'));
btnLasOn.Layout.Row = num_channels + 2; btnLasOn.Layout.Column = 11;

btnLasOff = uibutton(chanGrid, 'Text', 'LAS All OFF', 'FontWeight', 'bold', 'FontColor', [0.6 0 0], 'Enable', 'off', 'ButtonPushedFcn', @(~,~) setAllDropdowns('LAS', 'OFF'));
btnLasOff.Layout.Row = num_channels + 3; btnLasOff.Layout.Column = 11;

%% --- Bottom Panel: Global Sequence Parameters & Utilities ---
botPanel = uipanel(maingrid, 'Title', 'Global Procedure Parameters & Configuration Tools');
botGrid = uigridlayout(botPanel, [3, 7]);
botGrid.ColumnWidth = {130, 110, 130, 110, '1x', 35, 170};
botGrid.RowHeight = {30, 30, 30};

lblB1 = uilabel(botGrid, 'Text', 'T Ramp (°C/s):'); lblB1.Layout.Row = 1; lblB1.Layout.Column = 1;
tRampEdit = uieditfield(botGrid, 'numeric', 'Value', 0.1, 'ValueChangedFcn', @markProfileUnsaved, 'Tooltip', 'Speed of temperature change during a ramp.'); tRampEdit.Layout.Row = 1; tRampEdit.Layout.Column = 2;

lblB3 = uilabel(botGrid, 'Text', 'I Ramp (mA/s):'); lblB3.Layout.Row = 2; lblB3.Layout.Column = 1;
iRampEdit = uieditfield(botGrid, 'numeric', 'Value', 0.5, 'ValueChangedFcn', @markProfileUnsaved, 'Tooltip', 'Speed of current change during a ramp.'); iRampEdit.Layout.Row = 2; iRampEdit.Layout.Column = 2;

lblB5 = uilabel(botGrid, 'Text', 'T OFF Target (°C):'); lblB5.Layout.Row = 1; lblB5.Layout.Column = 3;
tOffEdit = uieditfield(botGrid, 'numeric', 'Value', 22, 'ValueChangedFcn', @markProfileUnsaved, 'Tooltip', 'Target temperature when TEC is turned OFF.'); tOffEdit.Layout.Row = 1; tOffEdit.Layout.Column = 4;

%% Save / Load / Rapid Setting Tools
btnSave = uibutton(botGrid, 'Text', '💾 Save Profile', 'ButtonPushedFcn', @saveConfig, 'Tooltip', 'Save current parameters to a text file.');
btnSave.Layout.Row = 3; btnSave.Layout.Column = 1;

btnLoad = uibutton(botGrid, 'Text', '📂 Load Profile', 'ButtonPushedFcn', @loadConfig, 'Tooltip', 'Load parameters from a saved text file.');
btnLoad.Layout.Row = 3; btnLoad.Layout.Column = 2;

btnClearProfile = uibutton(botGrid, 'Text', '❌ Clear Profile', 'ButtonPushedFcn', @clearProfile, 'Tooltip', 'Reset all settings to default values.');
btnClearProfile.Layout.Row = 3; btnClearProfile.Layout.Column = 3;

lblProfile = uilabel(botGrid, 'Text', 'Active Profile: [Unsaved]', 'FontWeight', 'bold');
lblProfile.Layout.Row = 3; lblProfile.Layout.Column = [4 5];

%% Execution Controls
btnExecAll = uibutton(botGrid, 'Text', '▶ RUN ALL', 'FontWeight', 'bold', 'BackgroundColor', [0.2 0.7 0.2], 'FontColor', 'w', ...
    'Enable', 'off', 'ButtonPushedFcn', @(src, event) executeAll(), 'Tooltip', 'Start sequence on all enabled channels.');
btnExecAll.Layout.Row = [1 2]; btnExecAll.Layout.Column = 7;

btnStop = uibutton(botGrid, 'Text', '⏹ CANCEL RUN (Safe)', 'FontWeight', 'bold', 'BackgroundColor', [0.8 0.1 0.1], 'FontColor', 'w', ...
    'Enable', 'off', 'ButtonPushedFcn', @stopExecution, 'Tooltip', 'Immediately halt all running sequences safely.');
btnStop.Layout.Row = 3; btnStop.Layout.Column = 7;

btnEmerg = uibutton(botGrid, 'Text', sprintf('⚠\nE\nM\n\nO\nF\nF'), 'FontWeight', 'bold', 'BackgroundColor', [1 0 0], 'FontColor', 'w', ...
    'Enable', 'off', 'ButtonPushedFcn', @emergencyLasOff, 'Tooltip', 'DANGER: Cuts LASER current immediately! Use only in emergencies.');
btnEmerg.Layout.Row = [1 3]; btnEmerg.Layout.Column = 6;

%% Set Cleanup Callback
fig.CloseRequestFcn = @closeApp;

%% Auto-Load Last Profile
try
    last_profile = getpref('LDC_Control_GUI', 'LastProfilePath', '');
    if ~isempty(last_profile) && isfile(last_profile)
        loadProfileFromFile(last_profile);
    end
catch
    % Ignore preference errors on startup
end


%% ================= CALLBACKS ================== %%

    function doCleanupAndClose()
        if ~isempty(telemetry_timer) && isvalid(telemetry_timer)
            stop(telemetry_timer);
            delete(telemetry_timer);
        end
        if ~isempty(s)
            s = [];
        end
        delete(fig);
    end

    function closeApp(~, ~)
        if is_executing
            selection = uiconfirm(fig, 'A hardware sequence is currently running. You must safely halt the hardware before closing the application. Do you want to issue a STOP command now?', ...
                'Action Required', 'Options', {'Stop Hardware', 'Cancel'}, 'DefaultOption', 1, 'CancelOption', 2, 'Icon', 'warning');
            if strcmp(selection, 'Stop Hardware')
                stopExecution();
            end
            return; % Prevent closing while executing
        end

        if is_scanning
            is_closing = true;
            statusLabel.Text = 'Status: Aborting scan to close...';
            statusLabel.FontColor = [0.8 0.4 0];
            drawnow;
            return; % Let scanChannels finish and handle the deletion
        end

        if startsWith(lblProfile.Text, '* ')
            selection = uiconfirm(fig, 'You have unsaved changes to the active profile. Are you sure you want to exit without saving?', ...
                'Unsaved Changes', 'Options', {'Save & Exit', 'Exit Without Saving', 'Cancel'}, ...
                'DefaultOption', 1, 'CancelOption', 3, 'Icon', 'warning');

            if strcmp(selection, 'Cancel')
                return;
            elseif strcmp(selection, 'Save & Exit')
                saveConfig();
                if startsWith(lblProfile.Text, '* ')
                    % Save was cancelled or failed, abort closing
                    return;
                end
            end
        end

        doCleanupAndClose();
    end

    function emergencyLasOff(~, ~)
        btnEmerg.Enable = 'off';
        selection = uiconfirm(fig, 'WARNING: Immediately cutting current without ramping down can damage the laser diode. Are you sure you want to proceed?', ...
            'Emergency LAS OFF', 'Options', {'Yes, Cut Current Now', 'Cancel'}, 'DefaultOption', 2, 'CancelOption', 2, 'Icon', 'warning');

        if strcmp(selection, 'Cancel')
            btnEmerg.Enable = 'on';
            return;
        end

        if strcmp(selection, 'Yes, Cut Current Now')
            for k = 1:num_channels
                if ~isvalid(fig) || is_closing, return; end
                if strcmp(chUI(k).enable.Enable, 'on')
                    try
                        sendCmd(sprintf('CHAN %d', k));
                        pause(0.15);
                        sendCmd('LAS:OUTPUT 0');
                        chUI(k).liveLas.Value = 'OFF'; chUI(k).liveLas.FontColor = 'black'; chUI(k).liveLas.BackgroundColor = [0.8 0.8 0.8];
                        chUI(k).status.Text = 'EMERGENCY OFF: Current Cut';
                        chUI(k).status.FontColor = 'red';
                        chUI(k).led.Color = [1 0 0];
                    catch
                    end
                end
            end
            if ~isvalid(fig) || is_closing, return; end
            drawnow;
            statusLabel.Text = 'Status: EMERGENCY LASER SHUTDOWN TRIGGERED.';
            statusLabel.FontColor = 'red';
            is_stop_requested = true; % Try to abort any running sequences as well
        end
        btnEmerg.Enable = 'on';
    end

    function connectSerial(~, ~)
        % Ensure we aren't destructing mid-run
        if is_executing
            if is_stop_requested
                statusLabel.Text = 'Status: WAITING for hardware sequence to safely halt...';
                statusLabel.FontColor = [0.8, 0.4, 0];
            else
                statusLabel.Text = 'Status: WARNING - MUST PRESS [STOP ALL] BEFORE DISCONNECTING!';
                statusLabel.FontColor = [0.8, 0, 0];
            end
            drawnow;
            return;
        end

        % Handle Disconnect case
        if strcmp(btnConnect.Text, 'Disconnect')
            if ~isempty(telemetry_timer) && isvalid(telemetry_timer)
                stop(telemetry_timer);
                delete(telemetry_timer);
                telemetry_timer = [];
            end
            if ~isempty(s)
                s = [];
            end
            is_simulated = false;
            statusLabel.Text = 'Status: Disconnected';
            statusLabel.FontColor = 'red';
            btnConnect.Text = 'Connect';
            btnConnect.BackgroundColor = [0.94 0.94 0.94];
            btnConnect.FontColor = 'black';
            comDropdown.Enable = 'on';
            btnScan.Enable = 'off';
            btnClearFaults.Enable = 'off';
            btnExecAll.Enable = 'off';
            lockUI('off');
            btnConnect.Enable = 'on'; % Ensure Connect button remains clickable to restore connection

            % Grey out all channels as well
            for ch = 1:num_channels
                markEmpty(ch, 'Disconnected');
            end
            return;
        end

        % Bind choice of simulation vs physical port
        is_simulated = strcmp(comDropdown.Value, 'Demo Simulator');

        if is_simulated
            statusLabel.Text = 'Status: Demo Mode Active';
            statusLabel.FontColor = [0.8, 0, 0.8];
            btnConnect.Text = 'Disconnect';
            btnConnect.BackgroundColor = [0.8 0.1 0.1];
            btnConnect.FontColor = 'w';
            comDropdown.Enable = 'off';
            btnScan.Enable = 'on';
            btnClearFaults.Enable = 'on';
            return;
        end

        port = comDropdown.Value;
        try
            s = serialport(port, 9600);
            configureTerminator(s, "LF");
            flush(s);
            statusLabel.Text = 'Status: Connected (Ready to Scan)';
            statusLabel.FontColor = [0, 0.6, 0];
            btnConnect.Text = 'Disconnect';
            btnConnect.BackgroundColor = [0.8 0.1 0.1];
            btnConnect.FontColor = 'w';
            comDropdown.Enable = 'off';
            btnScan.Enable = 'on';
            btnClearFaults.Enable = 'on';
        catch ME
            statusLabel.Text = 'Status: Connection Failed';
            uialert(fig, ME.message, 'Connection Error', 'Icon', 'error');
        end
    end

    function scanChannels(~, ~)
        is_scanning = true;
        % Visually lock UI during query
        btnScan.Enable = 'off';
        btnExecAll.Enable = 'off';
        lockUI('off');
        statusLabel.Text = 'Status: Scanning active hardware...';
        statusLabel.FontColor = [0.8, 0.5, 0];
        drawnow;

        cards_found = 0;

        for k = 1:num_channels
            if is_closing, break; end
            try
                % Flush any stale data from previous channel queries
                if ~is_simulated, flush(s, "input"); end

                % Check if channel physically responds to slot command
                cmdPause(sprintf('CHAN %d', k));
                pause(0.1); % Extra settling for channel switch
                if is_closing, break; end
                ansChanStr = queryCmd('CHAN?');
                if ~is_simulated, s.Timeout = 1; end
                ansChan = str2double(ansChanStr);

                if isempty(ansChanStr) || isnan(ansChan) || ansChan ~= k
                    % The processor slot is empty
                    markEmpty(k, 'Empty Slot');
                    continue;
                end

                cards_found = cards_found + 1;

                % Check thermocouple signature to deduce if laser actively attached.
                T_valStr = queryCmd('TEC:T?');
                T_val = str2double(T_valStr);

                % Update Telemetry Snapshot
                I_valStr = queryCmd('LAS:LDI?');
                I_val = str2double(I_valStr);
                if isnan(I_val), I_val = 0; end

                % Query logical command states to match dropdowns to hardware true states
                tecOutStatus = str2double(queryCmd('TEC:OUT?'));
                lasOutStatus = str2double(queryCmd('LAS:OUT?'));

                if isempty(T_valStr) || isnan(T_val) || T_val < 0
                    % Physical card exists, but floating/negative voltage implies no physical laser diode
                    markEmpty(k, 'No Laser Attached');
                else
                    % Available & Intact - Snap dropdown UI to match
                    chUI(k).enable.Enable = 'on';
                    chUI(k).enable.Value = true; % Auto check available slots

                    % Query for persistent faults upfront
                    [hasHwErr, hwErrStr] = checkControllerErrors(k);

                    % Assess if the hardware is already active
                    if hasHwErr
                        chUI(k).status.Text = sprintf('FAULT: %s', hwErrStr);
                        chUI(k).status.FontColor = [0.8, 0, 0];
                        chUI(k).led.Color = [1 0 0];
                    elseif tecOutStatus == 1 && lasOutStatus == 1
                        chUI(k).status.Text = 'TEC ON, LAS ON';
                        chUI(k).status.FontColor = [0.6, 0.4, 0];
                        chUI(k).led.Color = [0 1 0];
                    elseif tecOutStatus == 1
                        chUI(k).status.Text = 'TEC ON, LAS OFF';
                        chUI(k).status.FontColor = [0.6, 0.4, 0.0];
                        chUI(k).led.Color = [1 1 0];
                    elseif lasOutStatus == 1
                        chUI(k).status.Text = 'WARNING: LAS ON, TEC OFF';
                        chUI(k).status.FontColor = [0.8, 0, 0];
                        chUI(k).led.Color = [1 0 0];
                    else
                        chUI(k).status.Text = 'Ready';
                        chUI(k).status.FontColor = [0, 0.5, 0];
                        chUI(k).led.Color = [0 1 0];
                    end

                    chUI(k).curT.Value = T_val;
                    chUI(k).curI.Value = I_val;

                    maxT = str2double(queryCmd('TEC:LIM:THI?'));
                    if isnan(maxT), maxT = 99; end
                    chUI(k).tLim.Text = sprintf('%.0f', maxT);

                    maxI = str2double(queryCmd('LAS:LIM:I?'));
                    if isnan(maxI), maxI = str2double(queryCmd('LAS:LIM:LDI?')); end
                    if isnan(maxI), maxI = 500; end
                    chUI(k).iLim.Text = sprintf('%.0f', maxI);

                    cmdPause('LAS:MOD 1');

                    if tecOutStatus == 1, chUI(k).tecCmd.Value = 'ON'; else, chUI(k).tecCmd.Value = 'OFF'; end
                    if lasOutStatus == 1, chUI(k).lasCmd.Value = 'ON'; else, chUI(k).lasCmd.Value = 'OFF'; end

                    chUI(k).tecCmd.Enable = 'on';
                    chUI(k).lasCmd.Enable = 'on';
                    chUI(k).tTarget.Enable = 'on';
                    chUI(k).iTarget.Enable = 'on';
                    chUI(k).btnExec.Enable = 'on';
                    chUI(k).enable.Enable = 'on';
                end
            catch
                % Serial Timeout -> Mark empty
                markEmpty(k, 'Empty Slot');
            end
        end

        if ~is_simulated, s.Timeout = 5; end % Restore standard serial timeout

        if cards_found == 0
            statusLabel.Text = 'WARNING: Connected but 0 slots responded. Wrong COM or Controller Off?';
            statusLabel.FontColor = [0.8, 0.4, 0];
        else
            statusLabel.Text = 'Status: Scan Complete & Hardware Matched';
            statusLabel.FontColor = [0, 0.6, 0];
        end

        is_scanning = false;

        if is_closing
            doCleanupAndClose();
            return;
        end

        % Start telemetry timer to monitor live T and I
        if isempty(telemetry_timer) || ~isvalid(telemetry_timer)
            telemetry_timer = timer('ExecutionMode', 'fixedSpacing', 'Period', 2.0, ...
                'TimerFcn', @updateTelemetry);
            start(telemetry_timer);
        end

        lockUI('on');
    end

    function updateTelemetry(~, ~)
        if is_executing || is_scanning || strcmp(btnConnect.Text, 'Connect') || (~is_simulated && isempty(s))
            return;
        end

        if is_telemetry_running
            return;
        end
        is_telemetry_running = true;

        oldTimeout = 5;
        if ~is_simulated
            oldTimeout = s.Timeout;
            s.Timeout = 0.2; % Short timeout for background telemetry
        end

        for k = 1:num_channels
            % Abort telemetry immediately if a sequence or scan starts to prevent serial collision
            if is_executing || is_scanning
                is_telemetry_running = false;
                return;
            end

            % Only query valid connected channels via UI check
            if strcmp(chUI(k).enable.Enable, 'on')
                try
                    cmdPause(sprintf('CHAN %d', k));

                    T_valStr = queryCmd('TEC:T?');
                    T_val = str2double(T_valStr);
                    if ~isnan(T_val), chUI(k).curT.Value = T_val; end

                    I_valStr = queryCmd('LAS:LDI?');
                    I_val = str2double(I_valStr);
                    if ~isnan(I_val), chUI(k).curI.Value = I_val; end

                    tecStat = str2double(queryCmd('TEC:OUT?'));
                    if tecStat == 1
                        chUI(k).liveTec.Value = 'ON'; chUI(k).liveTec.FontColor = [1 1 1]; chUI(k).liveTec.BackgroundColor = [0 0.6 0];
                    else
                        chUI(k).liveTec.Value = 'OFF'; chUI(k).liveTec.FontColor = 'black'; chUI(k).liveTec.BackgroundColor = [0.8 0.8 0.8];
                    end

                    lasStat = str2double(queryCmd('LAS:OUT?'));
                    if lasStat == 1
                        chUI(k).liveLas.Value = 'ON'; chUI(k).liveLas.FontColor = [1 1 1]; chUI(k).liveLas.BackgroundColor = [0 0.6 0];
                    else
                        chUI(k).liveLas.Value = 'OFF'; chUI(k).liveLas.FontColor = 'black'; chUI(k).liveLas.BackgroundColor = [0.8 0.8 0.8];
                    end
                catch
                    % Background queries ignore timeouts
                end
            end
        end

        % Dynamically update Emergency Button State
        anyLaserOn = false;
        for k = 1:num_channels
            if strcmp(chUI(k).liveLas.Value, 'ON')
                anyLaserOn = true;
                break;
            end
        end

        if anyLaserOn
            btnEmerg.Enable = 'on';
        else
            btnEmerg.Enable = 'off';
        end

        if ~is_simulated && ~isempty(s)
            s.Timeout = oldTimeout;
        end
        
        is_telemetry_running = false;
    end

    function setAllDropdowns(typeStr, stateStr)
        for k = 1:num_channels
            if strcmp(typeStr, 'TEC') && strcmp(chUI(k).tecCmd.Enable, 'on')
                chUI(k).tecCmd.Value = stateStr;
            elseif strcmp(typeStr, 'LAS') && strcmp(chUI(k).lasCmd.Enable, 'on')
                chUI(k).lasCmd.Value = stateStr;
            end
        end
    end

    function setAllSystems(stateStr)
        for k = 1:num_channels
            if strcmp(chUI(k).tecCmd.Enable, 'on')
                chUI(k).tecCmd.Value = stateStr;
            end
            if strcmp(chUI(k).lasCmd.Enable, 'on')
                chUI(k).lasCmd.Value = stateStr;
            end
        end
    end

    function saveConfig(~, ~)
        [file, path] = uiputfile('*.txt', 'Save Laser Profile As...');
        if isequal(file, 0)
            return;
        end
        fileName = fullfile(path, file);
        figure(fig); % Fix window focus

        configData = struct();
        configData.T_ramp = tRampEdit.Value;
        configData.I_ramp = iRampEdit.Value;
        configData.T_OFF_Target = tOffEdit.Value;

        chanData = struct();
        for k = 1:num_channels
            chanData(k).T_Target = chUI(k).tTarget.Value;
            chanData(k).I_Target = chUI(k).iTarget.Value;
            chanData(k).Label = chUI(k).laserLabel.Value;
        end
        configData.channels = chanData;

        try
            jsonTxt = jsonencode(configData, 'PrettyPrint', true);
        catch
            jsonTxt = jsonencode(configData); % Fallback for older MATLAB versions
        end

        fid = fopen(fileName, 'w');
        if fid == -1
            uialert(fig, 'Failed to open file for writing.', 'Save Error', 'Icon', 'error');
            return;
        end
        fprintf(fid, '%s', jsonTxt);
        fclose(fid);

        active_profile_path = fileName;
        setpref('LDC_Control_GUI', 'LastProfilePath', active_profile_path);
        [~, name, ext] = fileparts(active_profile_path);
        lblProfile.Text = ['Active Profile: ' name ext];
        lblProfile.FontColor = 'black';

        uialert(fig, 'Hardware configuration profile has been saved as an editable plaintext (.txt) file.', ...
            'Profile Saved', 'Icon', 'success');
    end

    function loadConfig(~, ~)
        [file, path] = uigetfile('*.txt', 'Load Laser Profile');
        if isequal(file, 0)
            return;
        end
        fileName = fullfile(path, file);
        figure(fig); % Fix window focus
        loadProfileFromFile(fileName);
    end

    function loadProfileFromFile(fileName)
        try
            fid = fopen(fileName, 'r');
            if fid == -1
                error('Cannot open file.');
            end
            rawTxt = fread(fid, '*char')';
            fclose(fid);

            configData = jsondecode(rawTxt);

            saved_channels = length(configData.channels);
            if saved_channels ~= num_channels
                msg = sprintf('Profile Mismatch: The profile contains settings for %d channels, but the GUI is configured for %d channels.\n\nDo you want to apply the compatible settings to the available layout and ignore the rest?', saved_channels, num_channels);
                selection = uiconfirm(fig, msg, 'Channel Count Mismatch', ...
                    'Options', {'Apply Available Settings', 'Cancel'}, ...
                    'DefaultOption', 1, 'CancelOption', 2, 'Icon', 'warning');

                if strcmp(selection, 'Cancel')
                    return;
                end
            end

            tRampEdit.Value = configData.T_ramp;
            iRampEdit.Value = configData.I_ramp;
            tOffEdit.Value = configData.T_OFF_Target;

            apply_count = min(saved_channels, num_channels);

            for k = 1:apply_count
                chUI(k).tTarget.Value = configData.channels(k).T_Target;
                chUI(k).iTarget.Value = configData.channels(k).I_Target;
                if isfield(configData.channels, 'Label')
                    chUI(k).laserLabel.Value = configData.channels(k).Label;
                end
            end

            active_profile_path = fileName;
            setpref('LDC_Control_GUI', 'LastProfilePath', active_profile_path);
            [~, name, ext] = fileparts(active_profile_path);
            lblProfile.Text = ['Active Profile: ' name ext];
            lblProfile.FontColor = 'black';

            uialert(fig, sprintf('Hardware configuration profile "%s%s" has been loaded.', name, ext), ...
                'Profile Loaded', 'Icon', 'success');
        catch ME
            uialert(fig, ['Failed to load config file. It might be corrupted or outdated: ' ME.message], ...
                'Load Error', 'Icon', 'error');
        end
    end

    function markProfileUnsaved(~, ~)
        if ~startsWith(lblProfile.Text, '* ')
            lblProfile.Text = ['* ' lblProfile.Text];
            lblProfile.FontColor = [0.8 0.4 0];
        end
    end

    function clearProfile(~, ~)
        selection = uiconfirm(fig, 'Are you sure you want to clear the active profile and reset all settings to defaults?', ...
            'Confirm Clear', 'Options', {'Yes, Clear It', 'Cancel'}, 'DefaultOption', 2, 'CancelOption', 2, 'Icon', 'warning');
        if strcmp(selection, 'Cancel')
            return;
        end

        % Global Params
        tRampEdit.Value = 0.1;
        iRampEdit.Value = 0.5;
        tOffEdit.Value = 22;

        % Channel Params
        for k = 1:num_channels
            chUI(k).tTarget.Value = 22;
            chUI(k).iTarget.Value = 0;
            chUI(k).laserLabel.Value = sprintf('Laser %d', k);
        end

        % Unset preference
        if ispref('LDC_Control_GUI', 'LastProfilePath')
            rmpref('LDC_Control_GUI', 'LastProfilePath');
        end
        active_profile_path = '';

        lblProfile.Text = 'Active Profile: [Default/Cleared]';
        lblProfile.FontColor = 'black';
    end

    function clearFaults(~, ~)
        if isempty(s) && ~is_simulated
            uialert(fig, 'Not connected to controller.', 'Error', 'Icon', 'error');
            return;
        end
        lockUI('off');
        btnScan.Enable = 'off';
        btnClearFaults.Enable = 'off';
        btnExecAll.Enable = 'off';

        for k = 1:num_channels
            if ~isvalid(fig) || is_closing, return; end
            if strcmp(chUI(k).enable.Enable, 'on')
                try
                    sendCmd(sprintf('CHAN %d', k));
                    pause(0.1);
                    sendCmd('*CLS');
                catch
                end
            end
        end
        if ~isvalid(fig) || is_closing, return; end
        scanChannels();
    end

    function markEmpty(idx, reason)
        chUI(idx).enable.Value = false;
        chUI(idx).enable.Enable = 'off';
        chUI(idx).laserLabel.Enable = 'off';
        chUI(idx).liveTec.Value = 'OFF'; chUI(idx).liveTec.FontColor = 'black'; chUI(idx).liveTec.BackgroundColor = [0.8 0.8 0.8];
        chUI(idx).liveLas.Value = 'OFF'; chUI(idx).liveLas.FontColor = 'black'; chUI(idx).liveLas.BackgroundColor = [0.8 0.8 0.8];
        chUI(idx).status.Text = reason;
        chUI(idx).status.FontColor = [0.5, 0.5, 0.5];
        chUI(idx).led.Color = [0.8 0.8 0.8];
        chUI(idx).curT.Value = 0;
        chUI(idx).curI.Value = 0;
        chUI(idx).tecCmd.Enable = 'off';
        chUI(idx).lasCmd.Enable = 'off';
        chUI(idx).tTarget.Enable = 'off';
        chUI(idx).iTarget.Enable = 'off';
        chUI(idx).btnExec.Enable = 'off';
    end

    function lockUI(state)
        for k = 1:num_channels
            if strcmp(chUI(k).status.Text, 'Empty Slot') || strcmp(chUI(k).status.Text, 'No Laser Attached') || strcmp(chUI(k).status.Text, 'Disconnected')
                continue;
            end
            chUI(k).btnExec.Enable = state;
            chUI(k).enable.Enable = state;
            chUI(k).tecCmd.Enable = state;
            chUI(k).lasCmd.Enable = state;
            chUI(k).tTarget.Enable = state;
            chUI(k).iTarget.Enable = state;
            chUI(k).laserLabel.Enable = state;
        end
        btnExecAll.Enable = state;
        btnScan.Enable = state;
        btnClearFaults.Enable = state;
        btnLoad.Enable = state;
        btnSave.Enable = state;
        btnClearProfile.Enable = state;
        tRampEdit.Enable = state;
        iRampEdit.Enable = state;
        tOffEdit.Enable = state;
        btnMasterOn.Enable = state;
        btnMasterOff.Enable = state;
        btnTecOn.Enable = state;
        btnTecOff.Enable = state;
        btnLasOn.Enable = state;
        btnLasOff.Enable = state;
    end

    function stopExecution(~, ~)
        if is_executing
            is_stop_requested = true;
            statusLabel.Text = 'Status: STOP COMMANDED. Halting safely...';
            statusLabel.FontColor = 'red';
            beep; pause(0.15); beep; pause(0.15); beep;
            drawnow;
        end
    end

    function executeAll(~, ~)
        % Get array of ticked channels
        active_channels = zeros(1, num_channels);
        count = 0;
        for k = 1:num_channels
            if chUI(k).enable.Value
                count = count + 1;
                active_channels(count) = k;
            end
        end
        active_channels = active_channels(1:count);

        if isempty(active_channels)
            uialert(fig, 'No channels are marked "Enable" for the sequence!', 'Warning', 'Icon', 'warning');
            return;
        end
        executeChannels(active_channels);
    end

%% ================= PROCEDURE EXECUTION LOOP ================== %%

    function safePause(t)
        pause(t);
        if is_stop_requested
            error('HALT');
        end
    end

    function executeChannels(channelsToRun)
        if is_executing
            return;
        end

        is_executing = true;
        is_stop_requested = false; % Reset stop flag
        statusLabel.Text = 'Status: Sequence Running...';
        statusLabel.FontColor = [0, 0.4, 0];

        lockUI('off');
        btnStop.Enable = 'on'; % Only activate stop button during a run
        btnEmerg.Enable = 'on'; % Ensure emergency button is always available during runs

        if tRampEdit.Value <= 0 || iRampEdit.Value <= 0
            uialert(fig, 'Ramp speeds must be strictly greater than 0 to prevent hardware damage and infinite loops.', 'Invalid Configuration', 'Icon', 'error');
            lockUI('on');
            btnStop.Enable = 'off';
            btnEmerg.Enable = 'off';
            is_executing = false;
            return;
        end

        % Capture sequence configs
        rampConfig = struct(...
            'T_ramp', tRampEdit.Value, 'I_ramp', iRampEdit.Value);

        T_OFF_Target = tOffEdit.Value;

        % Pre-calculate global estimated time of arrival (ETA)
        total_estimated_time = 0;
        
        TEC_ON_TIME = 1.0;
        LAS_ON_TIME = 4.0;
        LAS_OFF_TIME = 1.5;
        TEC_OFF_TIME = 1.0;

        for j = 1:length(channelsToRun)
            c = channelsToRun(j);
            currT = chUI(c).curT.Value;
            currI = chUI(c).curI.Value;
            tTarg = chUI(c).tTarget.Value;
            iTarg = chUI(c).iTarget.Value;
            tOff = tOffEdit.Value;
            tCmd = chUI(c).tecCmd.Value;
            lCmd = chUI(c).lasCmd.Value;
            liveT = chUI(c).liveTec.Value;
            liveL = chUI(c).liveLas.Value;

            if strcmp(liveT, 'OFF') && strcmp(liveL, 'OFF')
                if strcmp(tCmd, 'ON') && strcmp(lCmd, 'OFF')
                    total_estimated_time = total_estimated_time + TEC_ON_TIME + abs(tTarg - currT)/rampConfig.T_ramp;
                elseif strcmp(tCmd, 'ON') && strcmp(lCmd, 'ON')
                    total_estimated_time = total_estimated_time + TEC_ON_TIME + abs(tTarg - currT)/rampConfig.T_ramp;
                    total_estimated_time = total_estimated_time + LAS_ON_TIME + abs(iTarg - 0)/rampConfig.I_ramp;
                end
            elseif strcmp(liveT, 'ON') && strcmp(liveL, 'OFF')
                if strcmp(tCmd, 'ON') && strcmp(lCmd, 'ON')
                    total_estimated_time = total_estimated_time + abs(tTarg - currT)/rampConfig.T_ramp;
                    total_estimated_time = total_estimated_time + LAS_ON_TIME + abs(iTarg - 0)/rampConfig.I_ramp;
                elseif strcmp(tCmd, 'OFF') && strcmp(lCmd, 'OFF')
                    total_estimated_time = total_estimated_time + abs(tOff - currT)/rampConfig.T_ramp + TEC_OFF_TIME;
                elseif strcmp(tCmd, 'ON') && strcmp(lCmd, 'OFF')
                    total_estimated_time = total_estimated_time + abs(tTarg - currT)/rampConfig.T_ramp;
                end
            elseif strcmp(liveT, 'ON') && strcmp(liveL, 'ON')
                if strcmp(tCmd, 'ON') && strcmp(lCmd, 'OFF')
                    total_estimated_time = total_estimated_time + abs(0 - currI)/rampConfig.I_ramp + LAS_OFF_TIME;
                    total_estimated_time = total_estimated_time + abs(tTarg - currT)/rampConfig.T_ramp;
                elseif strcmp(tCmd, 'OFF') && strcmp(lCmd, 'OFF')
                    total_estimated_time = total_estimated_time + abs(0 - currI)/rampConfig.I_ramp + LAS_OFF_TIME;
                    total_estimated_time = total_estimated_time + abs(tOff - currT)/rampConfig.T_ramp + TEC_OFF_TIME;
                elseif strcmp(tCmd, 'ON') && strcmp(lCmd, 'ON')
                    total_estimated_time = total_estimated_time + abs(tTarg - currT)/rampConfig.T_ramp;
                    total_estimated_time = total_estimated_time + abs(iTarg - currI)/rampConfig.I_ramp;
                end
            end
        end

        sequence_start_time = tic;

        for idx = 1:length(channelsToRun)
            if is_stop_requested, break; end

            chNum = channelsToRun(idx);

            TEC_ON_OFF = chUI(chNum).tecCmd.Value;
            LAS_ON_OFF = chUI(chNum).lasCmd.Value;
            T_ON_Target = chUI(chNum).tTarget.Value;
            I_ON_Target = chUI(chNum).iTarget.Value;

            try
                % Hardware Safety Interrogator Loop
                cmdPause(sprintf('CHAN %d', chNum));
                safePause(0.1);

                % Query physical hardware chassis limit to avoid bounding faults seamlessly!
                H_I_LIM = str2double(queryCmd('LAS:LIM:I?'));

                H_T_LIM = str2double(queryCmd('TEC:LIM:THI?'));

                % Validation Checks based entirely on chassis reporting
                if strcmpi(TEC_ON_OFF, 'ON') && ~isnan(H_T_LIM) && (T_ON_Target > H_T_LIM)
                    error('Target T (%.1f °C) exceeds hardware limit (%.1f °C). Aborted.', T_ON_Target, H_T_LIM);
                end

                if strcmpi(LAS_ON_OFF, 'ON') && ~isnan(H_I_LIM) && (I_ON_Target > H_I_LIM)
                    error('Target I (%.1f mA) exceeds hardware limit (%.1f mA). Aborted.', I_ON_Target, H_I_LIM);
                end

                if strcmpi(TEC_ON_OFF, 'OFF') && strcmpi(LAS_ON_OFF, 'ON')
                    error('TEC must be ON for LAS to be ON. Aborted.');
                end

                % Pass validated route to the executor
                LDC_Control_Core(chNum, TEC_ON_OFF, T_ON_Target, T_OFF_Target, ...
                    LAS_ON_OFF, I_ON_Target, rampConfig);

                % Final check for silent hardware errors (e.g. E501 Key Interlock)
                [hasErr, errStr] = checkControllerErrors(chNum);
                if hasErr
                    error('%s', errStr);
                end
            catch ME
                if startsWith(ME.message, 'HALT')
                    if strcmp(chUI(chNum).status.Text, 'Initializing...')
                        chUI(chNum).status.Text = 'HALTED (Before Ramp)';
                    else
                        chUI(chNum).status.Text = sprintf('HALTED at %.1f°C, %.1fmA', chUI(chNum).curT.Value, chUI(chNum).curI.Value);
                    end
                    chUI(chNum).status.FontColor = [0.8, 0, 0];
                    break;
                end

                chUI(chNum).status.Text = ME.message;
                chUI(chNum).status.FontColor = 'red';
                chUI(chNum).led.Color = [1 0 0];
                disp(['[Hardware Fault/Error] Channel ' num2str(chNum) ': ' ME.message]);

                % Global safety halt on any module error across any channel
                is_stop_requested = true;
                beep; pause(0.15); beep; pause(0.15); beep;
                break;
            end
        end

        % Finished Phase Cleanup
        lockUI('on');
        btnStop.Enable = 'off';
        is_executing = false;

        if is_stop_requested
            statusLabel.Text = 'Status: Hardware Halted & Pinned.';
            statusLabel.FontColor = [0.8, 0, 0];
        else
            statusLabel.Text = 'Status: Sequence Complete & Hardware Settled.';
            statusLabel.FontColor = [0, 0.6, 0];

            % Only popup if fully completed globally
            if length(channelsToRun) > 1
                beep; pause(0.15); beep;
                uialert(fig, 'Sequence completed across all selected channels.', 'Done', 'Icon', 'success');
            end
        end

        % Force immediate telemetry check to resolve button states
        updateTelemetry([], []);
    end

    function LDC_Control_Core(chNum, TEC_ON_OFF, T_ON_Target, T_OFF_Target, LAS_ON_OFF, I_ON_Target, rampConfig)
        chUI(chNum).status.Text = 'Initializing...';
        chUI(chNum).status.FontColor = [0.2, 0.2, 0.2];
        chUI(chNum).led.Color = [1 1 0];
        drawnow;

        % 1. Command Verification Alignment
        for retry = 1:3
            Chan_curr = str2double(queryCmd('CHAN?'));
            if ~isnan(Chan_curr) && Chan_curr == chNum
                break;
            end
            safePause(0.15);
            cmdPause(sprintf('CHAN %d', chNum));
        end

        if isnan(Chan_curr) || Chan_curr ~= chNum
            error('Hardware failed to switch to Channel %d. Communication timed out or controller is unresponsive.', chNum);
        end

        cmdPause('LAS:MOD 0'); % Safely lock out external modulation before execution
        safePause(0.1);
        verifyHwState('LAS:MOD?', 0, 'Hardware failed to disable external modulation.');

        % 2. Read Current Status Matrix
        TEC_CURR_STATUS = str2double(queryCmd('TEC:OUT?'));
        LAS_CURR_STATUS = str2double(queryCmd('LAS:OUT?'));

        % Logic Route Sequence
        if TEC_CURR_STATUS == 0 && LAS_CURR_STATUS == 0
            if strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'OFF')
                TEC_TEMP_Tset_Tcurr();
                safePause(0.15);
                sendCmd('TEC:OUTPUT 1');
                safePause(0.2);
                verifyHwState('TEC:OUT?', 1, 'Hardware failed to acknowledge TEC ON command.');
                chUI(chNum).liveTec.Value = 'ON'; chUI(chNum).liveTec.FontColor = [1 1 1]; chUI(chNum).liveTec.BackgroundColor = [0 0.6 0]; drawnow;
                safePause(0.15);
                RAMP_TEMP(T_ON_Target, rampConfig, chNum);

            elseif strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'ON')
                TEC_TEMP_Tset_Tcurr();
                safePause(0.15);
                sendCmd('TEC:OUTPUT 1');
                safePause(0.2);
                verifyHwState('TEC:OUT?', 1, 'Hardware failed to acknowledge TEC ON command.');
                chUI(chNum).liveTec.Value = 'ON'; chUI(chNum).liveTec.FontColor = [1 1 1]; chUI(chNum).liveTec.BackgroundColor = [0 0.6 0]; drawnow;
                safePause(0.15);
                RAMP_TEMP(T_ON_Target, rampConfig, chNum);

                safePause(0.15);
                sendCmd('LAS:LDI 0.0'); % Explicit zero anchor to prevent transient spikes
                safePause(0.15);
                sendCmd('LAS:OUTPUT 1');
                safePause(0.2);
                verifyHwState('LAS:OUT?', 1, 'Hardware failed to acknowledge LAS ON command.');
                chUI(chNum).liveLas.Value = 'ON'; chUI(chNum).liveLas.FontColor = [1 1 1]; chUI(chNum).liveLas.BackgroundColor = [0 0.6 0]; drawnow;
                safePause(2.5); % Safety lockout mandatory 2-sec delay
                [hasHwErr, hwErrStr] = checkControllerErrors(chNum);
                if hasHwErr, error('%s', hwErrStr); end

                RAMP_CURRENT(I_ON_Target, rampConfig, chNum);
            end

        elseif TEC_CURR_STATUS == 1 && LAS_CURR_STATUS == 0
            if strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'ON')
                RAMP_TEMP(T_ON_Target, rampConfig, chNum); % Ensure TEMP anchors structurally before continuing
                safePause(0.15);

                sendCmd('LAS:LDI 0.0'); % Explicit zero anchor to prevent transient spikes
                safePause(0.15);
                sendCmd('LAS:OUTPUT 1');
                safePause(0.2);
                verifyHwState('LAS:OUT?', 1, 'Hardware failed to acknowledge LAS ON command.');
                chUI(chNum).liveLas.Value = 'ON'; chUI(chNum).liveLas.FontColor = [1 1 1]; chUI(chNum).liveLas.BackgroundColor = [0 0.6 0]; drawnow;
                safePause(0.5);
                [hasHwErr, hwErrStr] = checkControllerErrors(chNum);
                if hasHwErr, error('%s', hwErrStr); end

                RAMP_CURRENT(I_ON_Target, rampConfig, chNum);

            elseif strcmpi(TEC_ON_OFF, 'OFF') && strcmpi(LAS_ON_OFF, 'OFF')
                RAMP_TEMP(T_OFF_Target, rampConfig, chNum);
                safePause(0.15);
                sendCmd('TEC:OUTPUT 0');
                safePause(0.2);
                verifyHwState('TEC:OUT?', 0, 'Hardware failed to acknowledge TEC OFF command.');
                chUI(chNum).liveTec.Value = 'OFF'; chUI(chNum).liveTec.FontColor = 'black'; chUI(chNum).liveTec.BackgroundColor = [0.8 0.8 0.8]; drawnow;
                safePause(0.5);

            elseif strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'OFF')
                RAMP_TEMP(T_ON_Target, rampConfig, chNum);
            end

        elseif TEC_CURR_STATUS == 1 && LAS_CURR_STATUS == 1
            if strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'OFF')
                RAMP_CURRENT(0, rampConfig, chNum);
                safePause(0.15);
                sendCmd('LAS:OUTPUT 0');
                safePause(0.2);
                verifyHwState('LAS:OUT?', 0, 'Hardware failed to acknowledge LAS OFF command.');
                chUI(chNum).liveLas.Value = 'OFF'; chUI(chNum).liveLas.FontColor = 'black'; chUI(chNum).liveLas.BackgroundColor = [0.8 0.8 0.8]; drawnow;
                safePause(0.5);
                RAMP_TEMP(T_ON_Target, rampConfig, chNum);

            elseif strcmpi(TEC_ON_OFF, 'OFF') && strcmpi(LAS_ON_OFF, 'OFF')
                RAMP_CURRENT(0, rampConfig, chNum);
                safePause(0.15);
                sendCmd('LAS:OUTPUT 0');
                safePause(0.2);
                verifyHwState('LAS:OUT?', 0, 'Hardware failed to acknowledge LAS OFF command.');
                chUI(chNum).liveLas.Value = 'OFF'; chUI(chNum).liveLas.FontColor = 'black'; chUI(chNum).liveLas.BackgroundColor = [0.8 0.8 0.8]; drawnow;
                safePause(1);
                RAMP_TEMP(T_OFF_Target, rampConfig, chNum);
                safePause(0.15);
                sendCmd('TEC:OUTPUT 0');
                safePause(0.2);
                verifyHwState('TEC:OUT?', 0, 'Hardware failed to acknowledge TEC OFF command.');
                chUI(chNum).liveTec.Value = 'OFF'; chUI(chNum).liveTec.FontColor = 'black'; chUI(chNum).liveTec.BackgroundColor = [0.8 0.8 0.8]; drawnow;
                safePause(0.5);

            elseif strcmpi(TEC_ON_OFF, 'ON') && strcmpi(LAS_ON_OFF, 'ON')
                RAMP_TEMP(T_ON_Target, rampConfig, chNum);
                safePause(0.15);
                RAMP_CURRENT(I_ON_Target, rampConfig, chNum);
            end
        else
            chUI(chNum).status.Text = 'Warning Status Collision. Skipping.';
            chUI(chNum).status.FontColor = 'red';
            return;
        end

        if ~is_stop_requested
            finalCheck(chNum);
        end
    end

    function TEC_TEMP_Tset_Tcurr()
        T_curr = str2double(queryCmd('TEC:T?'));
        if ~isnan(T_curr)
            cmdPause(sprintf('TEC:T %.2f', T_curr));
        end
    end

    function RAMP_TEMP(T_Target, rampConfig, chNum)
        T_curr = NaN;
        for retry = 1:5
            T_curr = str2double(queryCmd('TEC:SYNCT?'));
            if ~isnan(T_curr), break; end
            safePause(0.15);
        end

        if isnan(T_curr)
            error('CRITICAL FAULT: Lost telemetry during initial Thermal readout. Aborting to protect laser.');
        end

        if abs(T_curr - T_Target) < 0.05
            chUI(chNum).status.Text = sprintf('T at Target (%.1f °C)', T_curr);
            drawnow;
            return;
        end

        T_set = T_curr;
        T_start = T_curr;
        step = sign(T_Target - T_curr) * abs(rampConfig.T_ramp) * 0.5;

        while abs(T_set - T_Target) > 0.01


            T_set = T_set + step;
            if (step > 0 && T_set > T_Target) || (step < 0 && T_set < T_Target)
                T_set = T_Target;
            end

            sendCmd(sprintf('TEC:T %.2f', T_set));

            % High-responsiveness polling pause instead of locked pause
            pStart = tic;
            while toc(pStart) < 0.5

                updateETA();
                safePause(0.1);
            end

            T_curr = str2double(queryCmd('TEC:SYNCT?'));
            if isnan(T_curr), T_curr = T_set; end

            chUI(chNum).curT.Value = T_curr;
            pct = min(1, max(0, abs(T_curr - T_start) / max(0.01, abs(T_Target - T_start))));
            numBlocks = round(pct * 10);
            progBar = [repmat('█', 1, numBlocks), repmat('░', 1, 10 - numBlocks)];
            chUI(chNum).status.Text = sprintf('[%s] (%.1f °C)', progBar, T_curr);
            chUI(chNum).status.FontColor = [0, 0, 0.8];
            drawnow;
        end
    end

    function RAMP_CURRENT(I_Target, rampConfig, chNum)
        I_curr = NaN;
        for retry = 1:5
            I_curr = str2double(queryCmd('LAS:SYNCLDI?'));
            if ~isnan(I_curr), break; end
            safePause(0.15);
        end

        if isnan(I_curr)
            error('CRITICAL FAULT: Lost telemetry during initial Laser readout. Aborting to protect equipment.');
        end

        if abs(I_curr - I_Target) < 0.05
            chUI(chNum).status.Text = sprintf('I at Target (%.1f mA)', I_curr);
            drawnow;
            return;
        end

        I_set = I_curr;
        I_start = I_curr;
        step = sign(I_Target - I_curr) * abs(rampConfig.I_ramp) * 0.5;

        while abs(I_set - I_Target) > 0.01


            I_set = I_set + step;
            if (step > 0 && I_set > I_Target) || (step < 0 && I_set < I_Target)
                I_set = I_Target;
            end

            sendCmd(sprintf('LAS:LDI %.2f', I_set));

            % High-responsiveness polling pause instead of locked pause
            pStart = tic;
            while toc(pStart) < 0.5

                updateETA();
                safePause(0.1);
            end

            I_curr = str2double(queryCmd('LAS:SYNCLDI?'));
            if isnan(I_curr), I_curr = I_set; end

            chUI(chNum).curI.Value = I_curr;
            pct = min(1, max(0, abs(I_curr - I_start) / max(0.01, abs(I_Target - I_start))));
            numBlocks = round(pct * 10);
            progBar = [repmat('█', 1, numBlocks), repmat('░', 1, 10 - numBlocks)];
            chUI(chNum).status.Text = sprintf('[%s] (%.1f mA)', progBar, I_curr);
            chUI(chNum).status.FontColor = [0.8, 0, 0.8];
            drawnow;
        end
    end

    function finalCheck(chNum)
        tecStat = str2double(queryCmd('TEC:OUT?'));
        lasStat = str2double(queryCmd('LAS:OUT?'));

        statusStr = 'Final Set: ';
        if tecStat == 1
            statusStr = [statusStr 'TEC ON, '];
            chUI(chNum).liveTec.Value = 'ON'; chUI(chNum).liveTec.FontColor = [1 1 1]; chUI(chNum).liveTec.BackgroundColor = [0 0.6 0];
        else
            statusStr = [statusStr 'TEC OFF, '];
            chUI(chNum).liveTec.Value = 'OFF'; chUI(chNum).liveTec.FontColor = 'black'; chUI(chNum).liveTec.BackgroundColor = [0.8 0.8 0.8];
        end

        if lasStat == 1
            statusStr = [statusStr 'LAS ON'];
            chUI(chNum).liveLas.Value = 'ON'; chUI(chNum).liveLas.FontColor = [1 1 1]; chUI(chNum).liveLas.BackgroundColor = [0 0.6 0];
        else
            statusStr = [statusStr 'LAS OFF'];
            chUI(chNum).liveLas.Value = 'OFF'; chUI(chNum).liveLas.FontColor = 'black'; chUI(chNum).liveLas.BackgroundColor = [0.8 0.8 0.8];
        end

        chUI(chNum).status.Text = statusStr;
        chUI(chNum).status.FontColor = [0, 0.5, 0];
        chUI(chNum).led.Color = [0 1 0];

        sendCmd('LAS:MOD 1'); % Restore external modulation explicitly at end of run
        drawnow;
    end

    function updateETA()
        if isempty(sequence_start_time), return; end
        elapsed = toc(sequence_start_time);
        rem = max(0, total_estimated_time - elapsed);
        if rem > 0
            statusLabel.Text = sprintf('Status: Sequence Running... (Time Remaining: %02d:%02d)', floor(rem/60), floor(mod(rem, 60)));
        else
            statusLabel.Text = 'Status: Sequence Running... (Finishing up)';
        end
        statusLabel.FontColor = [0, 0.4, 0];
    end

    function verifyHwState(cmd, expectedVal, errMsg)
        for retry = 1:2
            res = str2double(queryCmd(cmd));
            if ~isnan(res) && res == expectedVal
                return;
            end
            safePause(0.15);
        end
        error('%s (Expected %d, Got %d)', errMsg, expectedVal, res);
    end

    function [hasError, errorStr] = checkControllerErrors(chNum)
        hasError = false;
        errorStr = '';
        errStrC = '';

        for retry = 1:3
            cmdPause(sprintf('CHAN %d', chNum));
            if ~is_simulated
                flush(s, "input");
            end
            sendCmd('MODERR?');
            pause(0.15);
            errResp = readCmd();
            errStrC = char(strtrim(errResp));

            if isempty(errStrC) || strcmp(errStrC, '0') || strcmp(errStrC, '000') || strcmp(errStrC, '00')
                return;
            end

            % Known standard hardware errors or codes
            if contains(errStrC, '501') || contains(errStrC, '504') || contains(errStrC, '503') || contains(errStrC, '505') || contains(errStrC, '508') || contains(errStrC, '511') || contains(errStrC, '404') || contains(errStrC, '407')
                break;
            end

            % If it's an unknown garbled response like '123,123', retry
            pause(0.2);
        end

        hasError = true;
        if contains(errStrC, '501')
            errorStr = sprintf('Interlock Error (E501): Key switch is off. [%s]', errStrC);
        elseif contains(errStrC, '504')
            errorStr = sprintf('Current Limit Reached (E504). [%s]', errStrC);
        elseif contains(errStrC, '503')
            errorStr = sprintf('Voltage Limit Reached / Open Circuit (E503). [%s]', errStrC);
        elseif contains(errStrC, '505')
            errorStr = sprintf('Voltage Limit Warning (E505). [%s]', errStrC);
        elseif contains(errStrC, '508')
            errorStr = sprintf('TEC Off Status Forced LAS Off (E508). [%s]', errStrC);
        elseif contains(errStrC, '511')
            errorStr = sprintf('Hardware Error (E511). [%s]', errStrC);
        elseif contains(errStrC, '404') || contains(errStrC, '407')
            errorStr = sprintf('Temperature Limit Error. [%s]', errStrC);
        else
            errorStr = sprintf('Module Error Code: %s', errStrC);
        end
    end

%% ================= HARDWARE SIMULATION WRAPPERS ================== %%

    function sendCmd(cmd)
        if is_simulated
            processSimCmd(cmd);
        else
            writeline(s, cmd);
        end
    end

    function val = readCmd()
        if is_simulated
            val = processSimQuery();
        else
            raw = readline(s);
            if isstring(raw) || ischar(raw)
                val = char(strtrim(raw));
            else
                val = '';
            end
        end
    end

    function val = queryCmd(cmd)
        % Atomic query: flush stale data, send query, wait, read response
        if ~is_simulated
            flush(s, "input");
        end
        sendCmd(cmd);
        pause(0.15);
        val = readCmd();
    end

    function cmdPause(cmd)
        % Send a write-only command and wait for the hardware to process it
        sendCmd(cmd);
        pause(0.15);
    end

    function processSimCmd(cmd)
        parts = split(cmd, ' ');
        if startsWith(cmd, 'CHAN?')
            sim_query_response = num2str(sim_state.curr_chan);

        elseif startsWith(cmd, 'CHAN')
            ch = str2double(parts{end});
            if sim_state.is_installed{ch} == 0
                sim_query_response = 'ERROR';
            else
                sim_state.curr_chan = ch;
            end

        elseif startsWith(cmd, 'TEC:T?') || startsWith(cmd, 'TEC:SYNCT?')
            ch = sim_state.curr_chan;
            if sim_state.is_installed{ch} == 2
                sim_query_response = '-10.5';
            else
                sim_query_response = num2str(sim_state.T_actual{ch});
            end

        elseif startsWith(cmd, 'TEC:T')
            if length(parts) > 1
                ch = sim_state.curr_chan;
                sim_state.T_actual{ch} = str2double(parts{end});
            end

        elseif startsWith(cmd, 'LAS:LDI?') || startsWith(cmd, 'LAS:SYNCLDI?')
            ch = sim_state.curr_chan;
            if sim_state.is_installed{ch} == 2
                sim_query_response = 'NaN';
            else
                sim_query_response = num2str(sim_state.I_actual{ch});
            end

        elseif startsWith(cmd, 'LAS:LDI')
            if length(parts) > 1
                ch = sim_state.curr_chan;
                sim_state.I_actual{ch} = str2double(parts{end});
            end

        elseif startsWith(cmd, 'TEC:OUT?')
            sim_query_response = num2str(sim_state.TEC_ON{sim_state.curr_chan});

        elseif startsWith(cmd, 'TEC:OUTPUT')
            if length(parts) > 1
                sim_state.TEC_ON{sim_state.curr_chan} = str2double(parts{end});
            end

        elseif startsWith(cmd, 'LAS:OUT?')
            sim_query_response = num2str(sim_state.LAS_ON{sim_state.curr_chan});

        elseif startsWith(cmd, 'LAS:OUTPUT')
            if length(parts) > 1
                sim_state.LAS_ON{sim_state.curr_chan} = str2double(parts{end});
            end

        elseif startsWith(cmd, 'LAS:LIM:I?')
            sim_query_response = '150'; % Fake current Limit
        elseif startsWith(cmd, 'TEC:LIM:THI?')
            sim_query_response = '80';  % Fake Temp Limit
        elseif startsWith(cmd, 'LAS:MOD?')
            sim_query_response = num2str(sim_state.LAS_MOD{sim_state.curr_chan});
        elseif startsWith(cmd, 'LAS:MOD')
            if length(parts) > 1
                sim_state.LAS_MOD{sim_state.curr_chan} = str2double(parts{end});
            end
        elseif startsWith(cmd, 'MODERR?')
            sim_query_response = '0';   % Simulator always reports no error
        end
    end

    function val = processSimQuery()
        val = sim_query_response;
    end
end
