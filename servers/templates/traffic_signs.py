from .base import render_template

_EXTRA_CSS = '''
.key-display {
    display: grid;
    grid-template-areas: ". up ." "left down right";
    gap: 4px; justify-content: center; margin: 8px 0 4px;
}
.key-box {
    width: 32px; height: 32px;
    display: flex; align-items: center; justify-content: center;
    border: 1px solid var(--border-color); border-radius: 4px;
    font-size: 13px; font-weight: 700; color: var(--text-muted);
    background: var(--bg-sidebar); transition: background 0.1s, border-color 0.1s, color 0.1s;
}
.key-box.active { background: rgba(63,185,80,0.2); border-color: var(--accent-green); color: var(--accent-green); }
.key-up { grid-area: up; } .key-down { grid-area: down; }
.key-left { grid-area: left; } .key-right { grid-area: right; }

.active-sign {
    padding: 10px; border-radius: 4px; margin-bottom: 10px;
    background: var(--bg-sidebar); border: 1px solid var(--border-color);
}
.active-sign.live { border-color: var(--accent-green); }
.active-sign .as-type { font-size: 18px; font-weight: 700; color: var(--text-primary); }
.active-sign .as-meta { font-size: 12px; color: var(--text-secondary); margin-top: 4px; font-variant-numeric: tabular-nums; }
.active-sign .as-badge { display:inline-block; font-size:11px; font-weight:700; padding:2px 6px; border-radius:3px; background:var(--accent-green); color:#06210b; margin-left:6px; }

.detections-list { display: flex; flex-direction: column; gap: 6px; max-height: 260px; overflow-y: auto; }
.det-row {
    display: flex; justify-content: space-between; align-items: center;
    padding: 5px 8px; background: var(--bg-sidebar);
    border: 1px solid var(--border-color); border-radius: 4px; font-size: 12px;
}
.det-class { font-weight: 600; color: var(--text-primary); }
.det-id    { color: var(--text-muted); font-size: 11px; }
.det-dist  { color: var(--text-secondary); font-variant-numeric: tabular-nums; }
.empty-state { color: var(--text-muted); font-size: 12px; text-align: center; padding: 12px; }
.model-status { padding: 6px 10px; border-radius: 4px; font-size: 12px; margin-bottom: 10px; }
.model-status.ok  { background: rgba(63,185,80,0.1); border: 1px solid rgba(63,185,80,0.3); color: var(--accent-green); }
.model-status.err { background: rgba(248,81,73,0.1); border: 1px solid rgba(248,81,73,0.3); color: var(--accent-red); }
'''

_CONTENT = '''
    <div class="container">
        <div class="video-section">
            <div class="video-wrapper" style="position:relative;display:inline-block;line-height:0">
                <img src="{{ url_for('video') }}" id="stream-img" class="stream">
            </div>
        </div>

        <div class="controls-section">
            <div class="card">
                <div class="card-header">Drive Control</div>
                <div style="display:flex;align-items:center;gap:16px;margin-bottom:12px">
                    <span id="run-indicator" style="display:inline-block;width:14px;height:14px;border-radius:50%;background:#e74c3c;flex-shrink:0"></span>
                    <span id="run-label" style="font-size:14px;font-weight:600;color:var(--text-secondary)">STOPPED</span>
                </div>
                <div style="display:flex;gap:10px;margin-bottom:8px">
                    <button onclick="driveStart()" class="button success" style="flex:1">Start</button>
                    <button onclick="driveStop()"  class="button" style="flex:1;background:var(--accent-orange,#e67e22)">Stop</button>
                </div>
                {% if virtual %}
                <div style="display:flex;gap:10px;margin-bottom:8px">
                    <button id="mode-btn" onclick="toggleMode()" class="button" style="flex:1;background:#555">Manual</button>
                    <button onclick="resetPosition()" class="button" style="flex:1;background:#444">Reset</button>
                </div>
                {% else %}
                <div style="margin-bottom:8px">
                    <button id="mode-btn" onclick="toggleMode()" class="button" style="width:100%;background:#555">Manual</button>
                </div>
                {% endif %}
                <div id="key-panel" style="display:none">
                    <div class="key-display">
                        <div class="key-box key-up"    id="key-up">&#9650;</div>
                        <div class="key-box key-left"  id="key-left">&#9664;</div>
                        <div class="key-box key-down"  id="key-down">&#9660;</div>
                        <div class="key-box key-right" id="key-right">&#9654;</div>
                    </div>
                    <p style="text-align:center;font-size:11px;color:var(--text-muted);margin:4px 0 0">Arrow keys or WASD</p>
                </div>
            </div>

            <div class="card">
                <div class="card-header">Speed</div>
                <div style="display:flex;align-items:center;gap:10px">
                    <input id="speed-slider" type="range" min="0.05" max="0.5" step="0.01" value="0.2"
                        style="flex:1" oninput="onSpeedChange(this.value)">
                    <span id="speed-value" style="font-size:13px;font-variant-numeric:tabular-nums;min-width:32px">0.20</span>
                </div>
            </div>

            <div class="card">
                <div class="card-header">Active Sign</div>
                <div id="active-sign" class="active-sign">
                    <div class="as-type" id="as-type">none</div>
                    <div class="as-meta" id="as-meta">&nbsp;</div>
                </div>
            </div>

            <div class="card">
                <div class="card-header">
                    Detections
                    <span style="font-size:11px;font-weight:400;color:var(--text-muted)" id="det-count"></span>
                </div>
                <div id="model-status" class="model-status ok">Loading…</div>
                <div id="detections" class="detections-list">
                    <div class="empty-state">Waiting for frames…</div>
                </div>
            </div>
        </div>
    </div>
'''

_EXTRA_JS = '''
    function setRunningUI(isRunning) {
        const indicator = document.getElementById('run-indicator');
        const label     = document.getElementById('run-label');
        indicator.style.background = isRunning ? '#2ecc71' : '#e74c3c';
        label.textContent = isRunning ? 'RUNNING' : 'STOPPED';
        label.style.color = isRunning ? '#2ecc71' : 'var(--text-secondary)';
    }
    function driveStart() { postJSON('/start', {}).then(() => setRunningUI(true)); }
    function driveStop()  { postJSON('/stop', {}).then(() => setRunningUI(false)); }
    function resetPosition() { postJSON('/reset', {}).then(data => { if (data && data.running !== undefined) setRunningUI(data.running); }); }

    let _manualMode = false;
    const keyState = {up: false, down: false, left: false, right: false};
    const keyMap = {
        'ArrowUp': 'up', 'w': 'up', 'W': 'up',
        'ArrowDown': 'down', 's': 'down', 'S': 'down',
        'ArrowLeft': 'left', 'a': 'left', 'A': 'left',
        'ArrowRight': 'right', 'd': 'right', 'D': 'right',
    };
    function updateKeyDisplay() {
        for (const [key, active] of Object.entries(keyState)) {
            const el = document.getElementById('key-' + key);
            if (el) el.classList.toggle('active', active);
        }
    }
    function sendKeys() {
        fetch('/keys', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(keyState)}).catch(() => {});
    }
    function toggleMode() {
        _manualMode = !_manualMode;
        postJSON('/set_mode', {mode: _manualMode ? 'manual' : 'auto'});
        const btn = document.getElementById('mode-btn');
        const panel = document.getElementById('key-panel');
        if (btn)   btn.textContent = _manualMode ? 'Auto' : 'Manual';
        if (panel) panel.style.display = _manualMode ? 'block' : 'none';
    }
    document.addEventListener('keydown', e => {
        const dir = keyMap[e.key];
        if (dir && !keyState[dir]) { e.preventDefault(); keyState[dir] = true; updateKeyDisplay(); if (_manualMode) sendKeys(); }
    });
    document.addEventListener('keyup', e => {
        const dir = keyMap[e.key];
        if (dir && keyState[dir]) { e.preventDefault(); keyState[dir] = false; updateKeyDisplay(); if (_manualMode) sendKeys(); }
    });
    window.addEventListener('blur', () => {
        Object.keys(keyState).forEach(k => keyState[k] = false);
        updateKeyDisplay(); if (_manualMode) sendKeys();
    });
    setInterval(() => { if (_manualMode && Object.values(keyState).some(Boolean)) sendKeys(); }, 150);

    let _speedDirty = false;
    function onSpeedChange(value) {
        document.getElementById('speed-value').textContent = parseFloat(value).toFixed(2);
        _speedDirty = true;
        postJSON('/set_speed', {value: parseFloat(value)}).then(() => { _speedDirty = false; });
    }

    async function pollStatus() {
        try {
            const data = await fetch('/status').then(r => r.json());
            setRunningUI(data.running);

            if (!_speedDirty && data.base_speed != null) {
                document.getElementById('speed-slider').value = data.base_speed;
                document.getElementById('speed-value').textContent = Number(data.base_speed).toFixed(2);
            }

            const status = document.getElementById('model-status');
            if (data.detector_ready) { status.className = 'model-status ok'; status.textContent = 'Detector ready (' + (data.family || 'tag36h11') + ')'; }
            else { status.className = 'model-status err'; status.textContent = data.load_error || 'Detector not ready'; }

            // Active sign card
            const card = document.getElementById('active-sign');
            const asType = document.getElementById('as-type');
            const asMeta = document.getElementById('as-meta');
            const a = data.active_sign;
            if (a) {
                card.classList.add('live');
                asType.innerHTML = a.sign_type + (a.at_sign ? '<span class="as-badge">AT SIGN</span>' : '');
                let meta = 'tag ' + a.tag_id + ' · ' + a.distance_m + ' m · offset ' + a.offset_norm;
                if (a.turns) meta += ' · turns: ' + a.turns.join(', ');
                asMeta.textContent = meta;
            } else {
                card.classList.remove('live');
                asType.textContent = 'none';
                asMeta.innerHTML = '&nbsp;';
            }

            const dets = data.detections || [];
            document.getElementById('det-count').textContent = dets.length ? dets.length + ' found' : '';
            const list = document.getElementById('detections');
            list.innerHTML = dets.length === 0
                ? '<div class="empty-state">No tags in view</div>'
                : dets.map(d => `
                    <div class="det-row">
                        <span class="det-class">${d.sign_type || '(unknown)'}</span>
                        <span class="det-id">tag ${d.tag_id}</span>
                        <span class="det-dist">${d.distance_m.toFixed(2)} m</span>
                    </div>`).join('');
        } catch (e) {}
    }
    setInterval(pollStatus, 300);
    pollStatus();
'''

TRAFFIC_SIGNS_TEMPLATE = render_template(
    'Traffic Signs',
    '{{ hostname }} — AprilTag Sign Recognition',
    _CONTENT,
    extra_css=_EXTRA_CSS,
    extra_js=_EXTRA_JS,
)
