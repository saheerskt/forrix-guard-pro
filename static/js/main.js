// DOM element cache — avoids repeated getElementById on every 1Hz tick
const _domCache = {};
function getEl(id) {
    if (!_domCache[id]) _domCache[id] = document.getElementById(id);
    return _domCache[id];
}

// Health tracking - connection status validated through data fetch
const healthStatus = {
    dataApi: true,
    redisConnected: true
};

function updateHealthStatus(component, isHealthy) {
    healthStatus[component] = isHealthy;
    updateConnectionStatus();
}

function updateConnectionStatus() {
    const isOnline = Object.values(healthStatus).every(status => status);
    const statusElem = getEl('connection-status');
    if (statusElem) {
        if (isOnline) {
            statusElem.textContent = 'System Online';
            statusElem.className = 'status-indicator status-normal';
        } else {
            statusElem.textContent = 'System Offline';
            statusElem.className = 'status-indicator status-offline';
        }
    }
}

// ── Data transport ─────────────────────────────────────────────────────────
// WebSocket is the primary channel. REST /api/data is the fallback (10s) and
// is also used for the first fetch to populate data before WS is established.

let _wsConnected = false;
let _pollInterval = null;
let _ws = null;
let _wsReconnectTimer = null;

function fetchData() {
    fetch('/api/data')
        .then(response => {
            if (response.ok) return response.json();
            if (response.status === 401) {
                window.location.href = '/login';
                throw new Error('Unauthorized');
            }
            throw new Error('HTTP ' + response.status);
        })
        .then(data => {
            updateHealthStatus('dataApi', true);
            updateHealthStatus('redisConnected', data.redis_connected === true);
            // Only call updateDashboard from REST when WS is not delivering data
            if (!_wsConnected) updateDashboard(data);
        })
        .catch(error => {
            console.error('REST poll error:', error);
            updateHealthStatus('dataApi', false);
        });
}

function _startRestFallback() {
    if (_pollInterval) return;
    _pollInterval = setInterval(fetchData, _wsConnected ? 10000 : 1000);
}

function _stopRestFallback() {
    if (_pollInterval) { clearInterval(_pollInterval); _pollInterval = null; }
}

function _connectWebSocket() {
    if (_ws && (_ws.readyState === WebSocket.CONNECTING || _ws.readyState === WebSocket.OPEN)) return;

    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
    _ws = new WebSocket(`${proto}//${location.host}/ws`);

    _ws.onopen = () => {
        _wsConnected = true;
        updateHealthStatus('dataApi', true);
        // Slow down REST poll to health-check only
        _stopRestFallback();
        _startRestFallback();
        console.log('WebSocket connected — REST poll slowed to 10s');
    };

    _ws.onmessage = (event) => {
        try {
            const msg = JSON.parse(event.data);
            if (msg.type === 'inverter_data' && msg.data) {
                updateHealthStatus('dataApi', true);
                updateHealthStatus('redisConnected', msg.data.redis_connected !== false);
                updateDashboard(msg.data);
            }
        } catch (e) {
            console.error('WS message parse error:', e);
        }
    };

    _ws.onerror = () => {
        _wsConnected = false;
        updateHealthStatus('dataApi', false);
    };

    _ws.onclose = () => {
        _wsConnected = false;
        updateHealthStatus('dataApi', false);
        // Revert to 1s REST polling while disconnected
        _stopRestFallback();
        _startRestFallback();
        // Reconnect after 5s
        if (_wsReconnectTimer) clearTimeout(_wsReconnectTimer);
        _wsReconnectTimer = setTimeout(_connectWebSocket, 5000);
        console.log('WebSocket closed — falling back to 1s REST poll, retrying WS in 5s');
    };
}

// Initial REST fetch so the dashboard has data immediately (WS delivers after handshake)
fetchData();
// Start 1s fallback poll; WS onopen will slow it to 10s once connected
_startRestFallback();
// Open WebSocket
_connectWebSocket();

function updateDashboard(data) {
    if (data.device_id) {
        setText('currentDevice', data.device_id);
    }
    if (data.timestamp) {
        const date = new Date(data.timestamp);
        setText('lastUpdate', date.toLocaleTimeString());
    }

    updateForrixGuardDemand(data);
    updateTariffImpact(data);
    updateRecommendation(data);
    updateSiteTelemetry(data);
    updateForrixGuardEvents(data);

    if (typeof numMeters !== 'undefined') {
        for (let m = 0; m < numMeters; m++) {
            updateMeter(m, data);
        }
    }

    if (typeof numPairs !== 'undefined') {
        for (let i = 0; i < numPairs; i++) {
            updateInverter(i, data);
            updateBattery(i, data);
            const mpptIndex = (typeof pairMpptMapping !== 'undefined' && pairMpptMapping[i] !== undefined)
                ? pairMpptMapping[i]
                : null;
            if (mpptIndex !== null) {
                updateMppt(mpptIndex, data);
            }
        }
    }

    if (typeof numDgs !== 'undefined') {
        for (let d = 0; d < numDgs; d++) {
            updateDG(d, data);
        }
    }
}

function updateSiteTelemetry(data) {
    const mapping = data.forrixguard_telemetry_mapping || {};
    updateVal('fg-grid-import', data.forrixguard_grid_import_kw, 'kW', 1);
    updateVal('fg-solar', data.forrixguard_solar_kw, 'kW', 1);
    updateVal('fg-load', data.forrixguard_load_kw, 'kW', 1);
    updateVal('fg-bess', data.forrixguard_bess_kw, 'kW', 1);
    setText('fg-dg-status', data.forrixguard_dg_status || 'Not configured');
    setText('fg-grid-map', mapping.grid_import || '--');
    setText('fg-solar-map', mapping.solar || '--');
    setText('fg-load-map', mapping.load || '--');
    setText('fg-bess-map', mapping.bess || '--');
    setText('fg-dg-map', mapping.dg || '--');
}

// --- Demand breach toast ---
let _prevBreachRisk = false;

function showToast(message, type = 'breach') {
    let container = getEl('toast-container');
    if (!container) return;
    const toast = document.createElement('div');
    toast.className = `toast ${type}`;
    toast.textContent = message;
    container.appendChild(toast);
    setTimeout(() => toast.remove(), 5000);
}

function titleFromToken(value) {
    const labels = {
        telemetry_gap: 'Telemetry gap',
        load_step_increase: 'Load step increase',
        bess_unavailable: 'BESS unavailable',
        bess_support_active: 'BESS support active',
        solar_shortfall: 'Solar shortfall',
        dg_started: 'DG started',
        load_rise: 'Load rise',
        window_closed: 'Window closed',
        normal: 'Normal'
    };
    if (labels[value]) return labels[value];
    return String(value || 'unknown')
        .replace(/_/g, ' ')
        .replace(/\b\w/g, ch => ch.toUpperCase());
}

function recommendationLabel(action) {
    const labels = {
        monitor: 'Monitor',
        reduce_import_or_dispatch_bess: 'Dispatch BESS or reduce import'
    };
    return labels[action] || titleFromToken(action);
}

function updateForrixGuardDemand(data) {
    updateVal('forrixguard-current-demand', data.forrixguard_current_demand_kw, 'kW', 1);
    updateVal('forrixguard-projected-demand', data.forrixguard_projected_demand_kw, 'kW', 1);
    updateVal('forrixguard-allowed-demand', data.forrixguard_allowed_kw, 'kW', 1);

    // Demand margin: headroom before breach (positive = safe, negative = over limit)
    const margin = (data.forrixguard_demand_margin_kw !== undefined)
        ? data.forrixguard_demand_margin_kw
        : ((data.forrixguard_allowed_kw || 0) - (data.forrixguard_current_demand_kw || 0));
    const marginEl = getEl('forrixguard-demand-margin');
    if (marginEl) {
        marginEl.textContent = isNaN(margin) ? '--' : margin.toFixed(1);
        marginEl.className = margin <= 0 ? 'value temp-critical'
            : margin < 10 ? 'value temp-warning'
            : 'value val-power';
    }

    updateVal('forrixguard-correction-demand', data.forrixguard_required_correction_kw, 'kW', 1);
    updateVal('forrixguard-savings-opportunity', data.forrixguard_savings_opportunity_kw, 'kW', 1);

    // Time left as MM:SS
    updateCountdown('forrixguard-demand-time-left', data.forrixguard_time_left_seconds);

    const riskElem = getEl('forrixguard-demand-risk');
    if (!riskElem) return;

    const breachRisk = data.forrixguard_breach_risk === true || data.forrixguard_breach_risk === 1;
    riskElem.textContent = breachRisk ? 'BREACH RISK' : 'NORMAL';
    riskElem.className = breachRisk ? 'status-indicator status-offline' : 'status-indicator status-normal';

    // D.6 — pulse the whole demand card red on breach
    const demandCard = document.querySelector('.demand-card');
    if (demandCard) demandCard.classList.toggle('breach-active', breachRisk);

    const causeEl = getEl('forrixguard-event-cause');
    if (causeEl) {
        causeEl.textContent = titleFromToken(data.forrixguard_event_cause);
        causeEl.className = breachRisk ? 'warning' : '';
    }

    const telemetryEl = getEl('forrixguard-telemetry-freshness');
    if (telemetryEl) {
        const fresh = data.forrixguard_telemetry_fresh !== false && data.forrixguard_telemetry_fresh !== 0;
        const age = Number(data.forrixguard_telemetry_age_seconds);
        telemetryEl.textContent = fresh
            ? `Fresh${Number.isFinite(age) ? `, ${age}s` : ''}`
            : `Stale${Number.isFinite(age) ? `, ${age}s` : ''}`;
        telemetryEl.className = fresh ? '' : 'critical';
    }

    const recommendationEl = getEl('forrixguard-recommendation');
    if (recommendationEl) {
        const action = data.forrixguard_recommendation_action || 'monitor';
        const kw = Number(data.forrixguard_recommendation_kw);
        const allowed = data.forrixguard_control_command_allowed === true || data.forrixguard_control_command_allowed === 1;
        const suffix = Number.isFinite(kw) && kw > 0 ? ` ${kw.toFixed(1)} kW` : '';
        recommendationEl.textContent = `${recommendationLabel(action)}${suffix} (${allowed ? 'control enabled' : 'advisory only'})`;
        recommendationEl.className = breachRisk ? 'warning' : '';
    }

    if (breachRisk && !_prevBreachRisk) {
        showToast('⚡ DEMAND BREACH RISK — Immediate correction required!');
    }
    _prevBreachRisk = breachRisk;

    // D.1 — window elapsed progress bar
    const WINDOW_SECONDS = 900;
    const timeLeft = data.forrixguard_time_left_seconds;
    if (timeLeft != null && !isNaN(timeLeft)) {
        const elapsed = Math.max(0, WINDOW_SECONDS - timeLeft);
        const pct = Math.min(100, Math.round((elapsed / WINDOW_SECONDS) * 100));
        const fill = getEl('demand-window-bar-fill');
        if (fill) {
            fill.style.width = pct + '%';
            fill.className = 'demand-window-bar-fill' + (pct > 80 ? ' red' : pct > 60 ? ' amber' : '');
        }
        const pctLabel = getEl('demand-window-pct');
        if (pctLabel) pctLabel.textContent = pct + '%';
    }

    // Next-window forecast
    const forecastKw   = data.fg_next_window_forecast_kw;
    const forecastRisk = data.fg_forecast_breach_risk;
    const confidence   = data.fg_forecast_confidence || 0;
    const forecastEl   = getEl('demand-forecast-kw');
    const badgeEl      = getEl('demand-forecast-badge');
    if (forecastEl && forecastKw > 0) {
        forecastEl.textContent = forecastKw.toFixed(1);
        forecastEl.classList.remove('skeleton');
    }
    if (badgeEl && forecastKw > 0) {
        if (forecastRisk) {
            badgeEl.textContent = 'BREACH RISK';
            badgeEl.className = 'demand-forecast-badge breach';
        } else {
            badgeEl.textContent = confidence > 0 ? `${Math.round(confidence * 100)}% conf` : 'learning';
            badgeEl.className = 'demand-forecast-badge safe';
        }
        badgeEl.style.display = '';
    }
}

function escapeHtml(value) {
    const text = value === null || value === undefined ? '' : value;
    return String(text).replace(/[&<>"']/g, (ch) => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#039;'
    }[ch]));
}

function formatEventValue(value, suffix = '') {
    const num = Number(value);
    if (!Number.isFinite(num)) return '--';
    return `${num.toFixed(1)}${suffix}`;
}

function eventDisplayLabel(code) {
    const labels = {
        DEMO_SCENARIO_STARTED: 'Demo Started',
        DEMAND_RISING: 'Demand Rising',
        DEMAND_BREACH_RISK: 'Peak Risk Warning',
        DEMAND_RISK_CLEAR: 'Peak Risk Cleared',
        DEMAND_WINDOW_CLOSED: 'Demand Window Closed',
        DEMAND_WINDOW_BREACHED: 'Demand Window Breached',
        BESS_DISPATCHED: 'Battery Support Started',
        DG_STARTED: 'DG Runtime Logged',
        LOAD_SHED_AVOIDED: 'Load Shedding Avoided',
        METER_COMM_LOST: 'Meter Communication Lost',
        SOLAR_UNDERPERFORMANCE: 'Solar Underperformance'
    };
    if (labels[code]) return labels[code];
    return String(code || 'Event').toLowerCase().replace(/_/g, ' ').replace(/\b\w/g, ch => ch.toUpperCase());
}

function eventStoryFallback(event, details) {
    const code = event.event_code || 'FORRIXGUARD_EVENT';
    const stories = {
        DEMAND_BREACH_RISK: {
            action: 'Calculate required correction kW',
            result: 'BESS dispatch preferred before load shedding'
        },
        DEMAND_RISK_CLEAR: {
            action: 'Continue monitoring after correction',
            result: 'Demand risk cleared'
        },
        BESS_DISPATCHED: {
            action: 'Discharge BESS within SOC reserve',
            result: 'Projected peak reduced without reserve violation'
        },
        DG_STARTED: {
            action: 'Log DG start against demand event',
            result: 'DG runtime available for fuel and operations review'
        },
        LOAD_SHED_AVOIDED: {
            action: 'Use BESS before trip relay',
            result: 'Production/HVAC load shedding avoided'
        },
        METER_COMM_LOST: {
            action: 'Flag communication loss and hold stale data proof',
            result: 'Data-quality issue visible immediately'
        },
        SOLAR_UNDERPERFORMANCE: {
            action: 'Record missed PV contribution',
            result: 'Report can explain why grid demand increased'
        },
        DEMAND_WINDOW_CLOSED: {
            action: 'Store window proof record',
            result: 'Monthly report can prove action and outcome'
        }
    };

    return {
        action: details.action || (stories[code] && stories[code].action) || 'Record event proof',
        result: details.result || (stories[code] && stories[code].result) || 'Available in local proof timeline'
    };
}

function updateForrixGuardEvents(data) {
    const list = getEl('forrixguard-event-list');
    const count = getEl('forrixguard-event-count');
    if (!list) return;

    const events = Array.isArray(data.forrixguard_events) ? data.forrixguard_events : [];
    if (count) {
        count.textContent = String(events.length);
        count.className = events.some(ev => ev.severity === 'warning' || ev.severity === 'critical')
            ? 'status-indicator status-offline'
            : 'status-indicator status-normal';
    }

    if (events.length === 0) {
        list.innerHTML = '<div class="event-proof-empty">No events recorded</div>';
        return;
    }

    list.innerHTML = events.slice(0, 6).map((event) => {
        const details = event.details || {};
        const when = event.timestamp ? new Date(event.timestamp).toLocaleTimeString() : '--';
        const severityClass = String(event.severity || 'info').replace(/[^a-z0-9_-]/gi, '').toLowerCase();
        const message = escapeHtml(event.message || event.event_code || 'ForrixGuard event');
        const label = eventDisplayLabel(event.event_code);
        const projected = details.projected_kw !== undefined && details.projected_kw !== null
            ? details.projected_kw
            : details.final_kw;
        const allowed = details.allowed_kw;
        const correction = details.correction_kw;
        const story = eventStoryFallback(event, details);
        const extra = [];
        if (details.bess_kw !== undefined) extra.push(`BESS ${formatEventValue(details.bess_kw, ' kW')}`);
        if (details.dg_runtime_min !== undefined) extra.push(`DG ${formatEventValue(details.dg_runtime_min, ' min')}`);
        if (details.avoided_load_shed_kw !== undefined) extra.push(`Avoided ${formatEventValue(details.avoided_load_shed_kw, ' kW')}`);
        if (details.actual_solar_kw !== undefined) extra.push(`Solar ${formatEventValue(details.actual_solar_kw, ' kW')}`);

        return `
            <div class="event-proof-item severity-${severityClass}">
                <div class="event-proof-main">
                    <span class="event-proof-code">${escapeHtml(label)}</span>
                    <span class="event-proof-time">${escapeHtml(when)}</span>
                </div>
                <div class="event-proof-message">${message}</div>
                <div class="event-proof-story">
                    <div><span>Action</span>${escapeHtml(story.action)}</div>
                    <div><span>Result</span>${escapeHtml(story.result)}</div>
                </div>
                <div class="event-proof-details">
                    <span>Projected ${escapeHtml(formatEventValue(projected, ' kW'))}</span>
                    <span>Allowed ${escapeHtml(formatEventValue(allowed, ' kW'))}</span>
                    <span>Correction ${escapeHtml(formatEventValue(correction, ' kW'))}</span>
                    ${extra.map(item => `<span>${escapeHtml(item)}</span>`).join('')}
                </div>
            </div>
        `;
    }).join('');
}

// Format seconds as MM:SS
function updateCountdown(id, totalSeconds) {
    const el = getEl(id);
    if (!el) return;
    if (totalSeconds == null || isNaN(totalSeconds) || totalSeconds < 0) {
        el.textContent = '--';
        el.classList.add('no-data');
        return;
    }
    el.classList.remove('no-data', 'skeleton');
    const m = Math.floor(totalSeconds / 60).toString().padStart(2, '0');
    const s = Math.floor(totalSeconds % 60).toString().padStart(2, '0');
    el.textContent = `${m}:${s}`;
}

// 2.2 Staleness: show "Stale" badge if field timestamp is > 15s old
const STALE_THRESHOLD_MS = 15000;
function updateStaleness(badgeId, data, field) {
    const badge = getEl(badgeId);
    if (!badge) return;
    const ts = data.timestamps && data.timestamps[field];
    if (!ts) { badge.textContent = ''; badge.className = 'device-staleness-badge'; return; }
    const ageMs = Date.now() - (ts * 1000);
    if (ageMs > STALE_THRESHOLD_MS) {
        badge.textContent = 'Stale';
        badge.className = 'device-staleness-badge stale';
    } else {
        badge.textContent = '';
        badge.className = 'device-staleness-badge';
    }
}

function updateMeter(index, data) {
    const pfx = `m${index}`;
    // Phase A
    updateVal(`${pfx}-phA-voltage`, data[`${pfx}_voltage`], 'V');
    updateVal(`${pfx}-phA-frequency`, data[`${pfx}_frequency`], 'Hz');
    updateVal(`${pfx}-phA-current`, data[`${pfx}_current`], 'A');
    updateVal(`${pfx}-phA-power`, data[`${pfx}_power`], 'W', 0);
    updateVal(`${pfx}-phA-reactive`, data[`${pfx}_reactive_power`], 'VAR', 0);

    // Phase B
    updateVal(`${pfx}-phB-voltage`, data[`${pfx}_phaseB_voltage`], 'V');
    updateVal(`${pfx}-phB-frequency`, data[`${pfx}_phaseB_frequency`], 'Hz');
    updateVal(`${pfx}-phB-current`, data[`${pfx}_phaseB_current`], 'A');
    updateVal(`${pfx}-phB-power`, data[`${pfx}_phaseB_power`], 'W', 0);
    updateVal(`${pfx}-phB-reactive`, data[`${pfx}_phaseB_reactive_power`], 'VAR', 0);

    // Phase C
    updateVal(`${pfx}-phC-voltage`, data[`${pfx}_phaseC_voltage`], 'V');
    updateVal(`${pfx}-phC-frequency`, data[`${pfx}_phaseC_frequency`], 'Hz');
    updateVal(`${pfx}-phC-current`, data[`${pfx}_phaseC_current`], 'A');
    updateVal(`${pfx}-phC-power`, data[`${pfx}_phaseC_power`], 'W', 0);
    updateVal(`${pfx}-phC-reactive`, data[`${pfx}_phaseC_reactive_power`], 'VAR', 0);

    // Total Active Power
    const phA = parseFloat(data[`${pfx}_power`] || 0);
    const phB = parseFloat(data[`${pfx}_phaseB_power`] || 0);
    const phC = parseFloat(data[`${pfx}_phaseC_power`] || 0);
    const totalPower = phA + phB + phC;
    updateVal(`${pfx}-total-power`, totalPower, 'W', 0);

    // Power Factor: PF = P / S, where S = sqrt(P² + Q²)
    const phAQ = parseFloat(data[`${pfx}_reactive_power`] || 0);
    const phBQ = parseFloat(data[`${pfx}_phaseB_reactive_power`] || 0);
    const phCQ = parseFloat(data[`${pfx}_phaseC_reactive_power`] || 0);
    const totalQ = phAQ + phBQ + phCQ;
    const S = Math.sqrt(totalPower * totalPower + totalQ * totalQ);
    const pfEl = getEl(`${pfx}-power-factor`);
    if (pfEl) {
        if (S > 1) {
            const pf = (Math.abs(totalPower) / S).toFixed(3);
            pfEl.textContent = pf;
            pfEl.classList.remove('no-data');
            pfEl.className = parseFloat(pf) < 0.85 ? 'value temp-warning' : 'value val-power';
        } else {
            pfEl.textContent = '--';
            pfEl.classList.add('no-data');
        }
    }

    // Gauge animation — no forced reflow
    updateGauge(`gauge-path-${pfx}`, `${pfx}-gauge-max`, totalPower);

    // 2.2 Staleness badge
    updateStaleness(`${pfx}-last-seen`, data, `${pfx}_power`);
}

function updateInverter(index, data) {
    const pfx = `inv${index}`;

    const statusVal = data[`${pfx}_status`] || 'unknown';
    const statusElem = getEl(`${pfx}-status`);
    if (statusElem) {
        const isNormal = statusVal.toLowerCase() === 'normal';
        statusElem.textContent = isNormal ? 'ACTIVE' : 'INACTIVE';
        statusElem.className = 'status-indicator ' + getStatusClass(statusVal);
    }

    updateVal(`${pfx}-phA-voltage`, data[`${pfx}_phaseA_voltage`], 'V');
    updateVal(`${pfx}-phA-power`, data[`${pfx}_phaseA_power`], 'W', 0);
    updateVal(`${pfx}-phA-reactive`, data[`${pfx}_phaseA_reactive_power`], 'VAR', 0);

    updateVal(`${pfx}-phB-voltage`, data[`${pfx}_phaseB_voltage`], 'V');
    updateVal(`${pfx}-phB-power`, data[`${pfx}_phaseB_power`], 'W', 0);
    updateVal(`${pfx}-phB-reactive`, data[`${pfx}_phaseB_reactive_power`], 'VAR', 0);

    updateVal(`${pfx}-phC-voltage`, data[`${pfx}_phaseC_voltage`], 'V');
    updateVal(`${pfx}-phC-power`, data[`${pfx}_phaseC_power`], 'W', 0);
    updateVal(`${pfx}-phC-reactive`, data[`${pfx}_phaseC_reactive_power`], 'VAR', 0);

    const phA = parseFloat(data[`${pfx}_phaseA_power`] || 0);
    const phB = parseFloat(data[`${pfx}_phaseB_power`] || 0);
    const phC = parseFloat(data[`${pfx}_phaseC_power`] || 0);
    const totalPower = phA + phB + phC;
    updateVal(`${pfx}-total-power`, totalPower, 'W', 0);
    updateGauge(`gauge-path-${pfx}`, `${pfx}-gauge-max`, totalPower);

    // 2.1 Fault code badge
    const faultCode = data[`${pfx}_fault_code`];
    const faultEl = getEl(`${pfx}-fault-code`);
    if (faultEl) {
        if (faultCode && faultCode !== 0) {
            faultEl.textContent = `F:0x${faultCode.toString(16).toUpperCase().padStart(4, '0')}`;
            faultEl.style.display = '';
        } else {
            faultEl.style.display = 'none';
        }
    }

    // 2.2 Staleness badge
    updateStaleness(`${pfx}-last-seen`, data, `${pfx}_phaseA_power`);
}

// Shared gauge update — single style batch, no forced reflow
function updateGauge(gaugePathId, maxLabelId, totalPower) {
    const gaugePath = getEl(gaugePathId);
    if (!gaugePath) return;

    const val = Math.abs(totalPower);
    const max = Math.max(100, Math.ceil((val * 1.25) / 100) * 100);
    const radius = 40;
    const circum = Math.PI * radius;
    const percent = Math.min(val / max, 1);

    // Pick CSS class for color — no forced reflow, composited by GPU
    const colorClass = percent > 0.8 ? 'gauge-arc-red'
        : percent > 0.6 ? 'gauge-arc-amber'
        : 'gauge-arc-green';
    if (gaugePath.dataset.colorClass !== colorClass) {
        gaugePath.classList.remove('gauge-arc-green', 'gauge-arc-amber', 'gauge-arc-red');
        gaugePath.classList.add(colorClass);
        gaugePath.dataset.colorClass = colorClass;
    }

    const fill = circum * percent;
    gaugePath.style.strokeDasharray = `${fill} ${circum}`;

    const maxLabel = getEl(maxLabelId);
    if (maxLabel) maxLabel.textContent = max;
}

function getTempClass(temp) {
    if (temp >= 55.0 || temp <= 0.0) return 'temp-critical';
    if (temp >= 45.0 || temp <= 10.0) return 'temp-warning';
    return 'val-temp';
}

function getVolClass(vol) {
    if (vol >= 3.6 || vol <= 2.8) return 'temp-critical';
    if (vol >= 3.5 || vol <= 3.0) return 'temp-warning';
    return 'val-voltage';
}

function updateBattery(index, data) {
    const pfx = `battery${index}`;
    updateVal(`${pfx}-soc`, data[`${pfx}_soc`], '%', 1);
    updateVal(`inv${index}-simple-soc`, data[`${pfx}_soc`], '%', 1);
    updateVal(`${pfx}-voltage`, data[`${pfx}_voltage`], 'V');

    const mainTemp = parseFloat(data[`${pfx}_temp`]);
    const maxTemp  = parseFloat(data[`${pfx}_max_temp`]);
    const minTemp  = parseFloat(data[`${pfx}_min_temp`]);
    const maxVol   = parseFloat(data[`${pfx}_max_voltage`]);
    const minVol   = parseFloat(data[`${pfx}_min_voltage`]);

    const mainTempElem = getEl(`${pfx}-temp`);
    if (mainTempElem) mainTempElem.className = getTempClass(mainTemp);
    const maxTempElem = getEl(`${pfx}-max-temp`);
    if (maxTempElem) maxTempElem.className = getTempClass(maxTemp);
    const minTempElem = getEl(`${pfx}-min-temp`);
    if (minTempElem) minTempElem.className = getTempClass(minTemp);
    const maxVolElem = getEl(`${pfx}-max-voltage`);
    if (maxVolElem) maxVolElem.className = getVolClass(maxVol);
    const minVolElem = getEl(`${pfx}-min-voltage`);
    if (minVolElem) minVolElem.className = getVolClass(minVol);

    updateVal(`${pfx}-temp`, data[`${pfx}_temp`], '°C');
    updateVal(`${pfx}-max-temp`, data[`${pfx}_max_temp`], '°C');
    updateVal(`${pfx}-min-temp`, data[`${pfx}_min_temp`], '°C');
    updateVal(`${pfx}-power`, data[`${pfx}_power`], 'W', 0);
    updateVal(`${pfx}-max-voltage`, data[`${pfx}_max_voltage`], 'V', 3);
    updateVal(`${pfx}-min-voltage`, data[`${pfx}_min_voltage`], 'V', 3);
}

function updateMppt(index, data) {
    const pfx = `mppt${index}`;
    const mpptTemp = parseFloat(data[`${pfx}_temperature`]);
    const mainTempElem = getEl(`${pfx}-temperature`);
    if (mainTempElem) mainTempElem.className = getTempClass(mpptTemp);

    updateVal(`${pfx}-output-voltage`, data[`${pfx}_output_voltage`], 'V');
    updateVal(`${pfx}-total-power`, data[`${pfx}_total_power`], 'W', 0);
    updateVal(`${pfx}-temperature`, data[`${pfx}_temperature`], '°C');

    const statusVal = data[`${pfx}_status`] || 'unknown';
    const statusElem = getEl(`${pfx}-status`);
    if (statusElem) {
        statusElem.textContent = statusVal.toUpperCase();
        statusElem.className = 'status-indicator ' + getStatusClass(statusVal);
    }
}

function updateTariffImpact(data) {
    const loaded = data.fg_tariff_loaded;
    const currency = data.fg_tariff_currency || 'INR';
    const rate = data.fg_demand_charge_per_kva || 0;
    const projected = data.fg_projected_demand_charge || 0;
    const breachCost = data.fg_breach_cost_if_hit || 0;
    const savingsToday = data.fg_savings_today || 0;

    const badge = getEl('tariff-currency-badge');
    if (badge) badge.textContent = currency;

    const fmt = (n) => loaded && rate > 0
        ? `${currency} ${Number(n).toLocaleString('en-IN', {maximumFractionDigits: 0})}`
        : '--';

    const setFmt = (id, val) => { const el = getEl(id); if (el) { el.textContent = fmt(val); el.classList.remove('skeleton'); } };
    setFmt('tariff-demand-rate',     rate);
    setFmt('tariff-projected-charge', projected);
    setFmt('tariff-breach-cost',     breachCost);
    setFmt('tariff-savings-today',   savingsToday);

    const note = getEl('tariff-note');
    if (note) note.style.display = (loaded && rate > 0) ? 'none' : '';
}

let _currentRecommendation = null;
let _recommendationDismissed = false;

function updateRecommendation(data) {
    const rec = data.fg_recommendation;
    const card = getEl('recommendation-card');
    if (!card) return;

    if (!rec || !rec.action || rec.action === 'bess_unavailable' || _recommendationDismissed) {
        card.style.display = 'none';
        if (!rec || !rec.action) _recommendationDismissed = false;
        return;
    }

    _currentRecommendation = rec;
    card.style.display = '';

    const modeBadge = getEl('rec-mode-badge');
    if (modeBadge) {
        const isClosedLoop = rec.control_mode === 'CLOSED_LOOP_CONTROL';
        modeBadge.textContent = isClosedLoop ? 'AUTO-CONTROL' : 'ADVISORY';
        modeBadge.className = 'recommendation-mode-badge' + (isClosedLoop ? ' closed-loop' : '');
    }

    const desc = getEl('rec-description');
    if (desc) {
        const mins = Math.ceil(rec.time_left_sec / 60);
        desc.textContent = `Discharge ${rec.correction_kw} kW from BESS for next ${mins} min to prevent demand breach. BESS SOC: ${rec.bess_soc?.toFixed(0) ?? '--'}%`;
    }

    const fin = getEl('rec-financials');
    const breachEl = getEl('rec-breach-cost');
    const battEl = getEl('rec-batt-value');
    const cur = rec.currency || 'INR';
    if (fin && breachEl && battEl && (rec.breach_cost || rec.batt_value)) {
        fin.style.display = '';
        breachEl.textContent = rec.breach_cost ? `Breach avoided: ${cur} ${Number(rec.breach_cost).toLocaleString('en-IN')}` : '';
        battEl.textContent  = rec.batt_value  ? `Battery cost: ${cur} ${Number(rec.batt_value).toLocaleString('en-IN')}` : '';
    } else if (fin) {
        fin.style.display = 'none';
    }

    const applyBtn = getEl('btn-apply-recommendation');
    if (applyBtn) applyBtn.disabled = rec.auto_apply;
}

async function applyRecommendation() {
    if (!_currentRecommendation) return;
    try {
        const r = await fetch('/api/apply-recommendation', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                correction_kw: _currentRecommendation.correction_kw,
                time_left_sec: _currentRecommendation.time_left_sec,
            })
        });
        const res = await r.json();
        if (res.success) {
            showToast(`Dispatching ${res.applied_kw} kW BESS for ${Math.ceil(res.duration_sec / 60)} min`, 'success');
            dismissRecommendation();
        } else {
            showToast('Apply failed: ' + (res.error || ''), 'error');
        }
    } catch (e) { showToast('Error applying recommendation', 'error'); }
}

function dismissRecommendation() {
    _recommendationDismissed = true;
    const card = getEl('recommendation-card');
    if (card) card.style.display = 'none';
}

async function loadTariffConfig() {
    try {
        const r = await fetch('/api/tariff');
        if (!r.ok) return;
        const t = await r.json();
        const fields = {
            'tariff-currency': t.currency,
            'tariff-demand-charge': t.demandChargePerKva,
            'tariff-day-rate': t.dayRatePerKwh,
            'tariff-night-rate': t.nightRatePerKwh,
            'tariff-peak-start': t.peakStartHHMM,
            'tariff-peak-end': t.peakEndHHMM,
        };
        Object.entries(fields).forEach(([id, val]) => {
            const el = getEl(id);
            if (el && val !== undefined) el.value = val;
        });
    } catch (e) { /* silently skip if on non-setup page */ }
}

async function saveTariffConfig() {
    const get = (id) => { const el = getEl(id); return el ? el.value : null; };
    const body = {
        currency:           get('tariff-currency') || 'INR',
        demandChargePerKva: parseFloat(get('tariff-demand-charge') || 0),
        dayRatePerKwh:      parseFloat(get('tariff-day-rate') || 0),
        nightRatePerKwh:    parseFloat(get('tariff-night-rate') || 0),
        peakStartHHMM:      get('tariff-peak-start') || '0700',
        peakEndHHMM:        get('tariff-peak-end') || '2300',
    };
    try {
        const r = await fetch('/api/tariff', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body) });
        const res = await r.json();
        if (res.success) showToast('Tariff rates saved', 'success');
        else showToast('Failed to save tariff: ' + (res.error || ''), 'error');
    } catch (e) { showToast('Error saving tariff', 'error'); }
}

function updateDG(index, data) {
    const pfx = `dg${index}`;
    const statusVal = data[`${pfx}_status`] || 'unknown';
    const statusElem = getEl(`${pfx}-status`);
    if (statusElem) {
        statusElem.textContent = statusVal.toUpperCase();
        statusElem.className = 'value ' + getStatusClass(statusVal);
        statusElem.classList.remove('skeleton');
    }
    updateVal(`${pfx}-power-kw`, data[`${pfx}_power_kw`], 'kW', 1);
    updateVal(`${pfx}-runtime-min`, data[`${pfx}_runtime_min`], 'min', 0);
    updateStaleness(`${pfx}-last-seen`, data, `${pfx}_status`);
}

function updateVal(id, value, unit, decimals = 1) {
    const el = getEl(id);
    if (!el) return;

    if (value !== null && value !== undefined) {
        el.textContent = parseFloat(value).toFixed(decimals);
        el.classList.remove('no-data', 'skeleton');
        if (el.classList.contains('gauge-value')) {
            adjustGaugeFontSize(el);
        }
    } else {
        el.textContent = '--';
        el.classList.add('no-data');
    }
}

function adjustGaugeFontSize(element) {
    const wrapper = element.parentElement;
    if (!wrapper || !wrapper.classList.contains('gauge-value-wrapper')) return;
    const textLength = element.textContent.length;
    element.style.fontSize = '';
    if (textLength >= 7) {
        element.style.fontSize = '0.75rem';
    } else if (textLength >= 6) {
        element.style.fontSize = '0.85rem';
    } else if (textLength >= 5) {
        element.style.fontSize = '1.0rem';
    }
}

function setText(id, text) {
    const el = getEl(id);
    if (el) el.textContent = text;
}

function getStatusClass(status) {
    const s = String(status).toLowerCase();
    if (s === 'normal' || s === 'online') return 'status-normal';
    if (s === 'waiting' || s.includes('warning')) return 'status-warning';
    if (s.includes('fault') || s.includes('error')) return 'status-fault';
    return 'status-offline';
}

async function fetchUserProfile() {
    try {
        const response = await fetch('/api/me');
        if (response.ok) {
            const data = await response.json();
            const nameElem = getEl('user-display-name');
            const iconElem = getEl('user-role-icon');
            if (nameElem) nameElem.textContent = data.username;
            if (iconElem) {
                iconElem.style.color = data.role === 'admin' ? 'var(--accent-amber)' : 'var(--accent-cyan)';
            }
        }
    } catch (e) {
        console.error('Error fetching user profile:', e);
    }
}

document.addEventListener('DOMContentLoaded', () => {
    fetchUserProfile();
});

function toggleBatteryDetails(btn, targetId) {
    const details = document.getElementById(targetId);
    if (!details) return;
    if (details.classList.contains('expanded')) {
        details.classList.remove('expanded');
        btn.classList.remove('expanded');
    } else {
        details.classList.add('expanded');
        btn.classList.add('expanded');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    const toggleBtn = getEl('toggle-details-btn');
    if (toggleBtn) {
        const savedState = localStorage.getItem('dashboardDetailsMode');
        if (savedState === 'true') {
            document.body.classList.add('show-details-mode');
            const icon = getEl('toggle-details-icon');
            const text = getEl('toggle-details-text');
            if (icon && text) {
                icon.className = 'fa-solid fa-eye-slash';
                text.textContent = 'Hide Detail';
            }
        }

        toggleBtn.addEventListener('click', () => {
            const isDetailed = document.body.classList.toggle('show-details-mode');
            const icon = getEl('toggle-details-icon');
            const text = getEl('toggle-details-text');
            if (isDetailed) {
                if (icon && text) { icon.className = 'fa-solid fa-eye-slash'; text.textContent = 'Hide Detail'; }
            } else {
                if (icon && text) { icon.className = 'fa-solid fa-eye'; text.textContent = 'Show Detail'; }
            }
            localStorage.setItem('dashboardDetailsMode', isDetailed);
        });
    }
});
