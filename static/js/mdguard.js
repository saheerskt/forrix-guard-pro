/* MDGuard live dashboard — talks to existing /api/data + /api/monthly-report endpoints */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const fmt = (n, d = 0) => (n == null || isNaN(n)) ? '--' : Number(n).toLocaleString('en-IN', { maximumFractionDigits: d, minimumFractionDigits: d });
  const fmtMoney = (n, ccy) => {
    if (n == null || isNaN(n)) return '—';
    const sym = ({ INR: '₹', AED: 'AED ', USD: '$', GBP: '£' })[ccy] || '';
    return sym + Number(n).toLocaleString(ccy === 'INR' ? 'en-IN' : 'en-US', { maximumFractionDigits: 0 });
  };
  const clamp = (n, lo, hi) => Math.max(lo, Math.min(hi, n));

  let pollTimer = null;
  let lastBreachState = null;
  let monthlyCache = null;

  function setStatusBadge(pct, breach) {
    const b = $('mgStatusBadge');
    const t = $('mgStatusText');
    const hero = $('mgHero');
    hero.classList.remove('is-warn', 'is-breach');
    b.classList.remove('safe', 'warn', 'danger');
    if (breach || pct >= 100) {
      b.classList.add('danger'); t.textContent = 'BREACH RISK'; hero.classList.add('is-breach');
    } else if (pct >= 90) {
      b.classList.add('danger'); t.textContent = 'BREACH RISK'; hero.classList.add('is-breach');
    } else if (pct >= 80) {
      b.classList.add('warn'); t.textContent = 'CAUTION'; hero.classList.add('is-warn');
    } else {
      b.classList.add('safe'); t.textContent = 'DEMAND SAFE';
    }
  }

  function setBar(elId, pct) {
    const el = $(elId);
    const p = clamp(pct, 0, 100);
    el.style.width = p + '%';
    el.classList.remove('warn', 'danger');
    if (p >= 90) el.classList.add('danger');
    else if (p >= 80) el.classList.add('warn');
  }

  function showAlarm(level, text) {
    const al = $('mgAlarm');
    const ic = $('mgAlarmIcon');
    al.classList.remove('hidden', 'safe', 'warn', 'danger');
    al.classList.add(level);
    ic.className = level === 'danger' ? 'fa-solid fa-triangle-exclamation'
      : level === 'warn' ? 'fa-solid fa-circle-exclamation'
      : 'fa-solid fa-circle-check';
    $('mgAlarmText').textContent = text;
    if (level === 'safe') setTimeout(() => al.classList.add('hidden'), 5000);
  }

  function toast(level, text) {
    const t = document.createElement('div');
    t.className = 'mg-toast ' + level;
    t.innerHTML = `<i class="fa-solid fa-circle-info"></i><span>${text}</span>`;
    $('mgToasts').appendChild(t);
    setTimeout(() => t.remove(), 4000);
  }

  function fmtCountdown(sec) {
    if (sec == null || sec < 0) return '--:--';
    const m = Math.floor(sec / 60), s = sec % 60;
    return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
  }

  // ---- Device strip rendering ----
  function deviceCard(opts) {
    const div = document.createElement('div');
    div.className = 'mg-devcard';
    div.innerHTML = `
      <div class="head"><i class="${opts.icon}"></i><span>${opts.title}</span></div>
      <div class="model">${opts.model || '—'}</div>
      <div class="v">${opts.value}</div>
      <div class="status"><span class="mg-dot ${opts.live ? 'live' : 'down'}"></span>${opts.live ? 'Live' : 'Offline'}</div>`;
    div.addEventListener('click', () => openDrawer(opts.title, opts.detail || []));
    return div;
  }

  function openDrawer(title, rows) {
    $('mgDrawerTitle').textContent = title;
    const body = $('mgDrawerBody');
    body.innerHTML = '';
    if (!rows.length) body.innerHTML = '<div class="mg-mute">No detail available.</div>';
    rows.forEach(r => {
      const row = document.createElement('div');
      row.className = 'mg-drawer__row';
      row.innerHTML = `<span class="mg-mute">${r.k}</span><span class="v">${r.v}</span>`;
      body.appendChild(row);
    });
    $('mgDrawer').classList.add('open');
    $('mgDrawerBg').classList.add('open');
  }
  window.mgCloseDrawer = function () {
    $('mgDrawer').classList.remove('open');
    $('mgDrawerBg').classList.remove('open');
  };

  function renderDevices(d) {
    const strip = $('mgDevStrip');
    strip.innerHTML = '';
    const meters = d.meters || [];
    const inverters = d.inverters || [];
    const batteries = d.batteries || [];
    const dgs = d.dgs || [];

    const phaseRows = (m) => {
      if (!m) return [];
      const out = [];
      ['a', 'b', 'c'].forEach(ph => {
        const v = m['voltage_' + ph], i = m['current_' + ph], p = m['power_' + ph], pf = m['pf_' + ph];
        if (v != null) out.push({ k: `Phase ${ph.toUpperCase()} Voltage`, v: fmt(v, 1) + ' V' });
        if (i != null) out.push({ k: `Phase ${ph.toUpperCase()} Current`, v: fmt(i, 2) + ' A' });
        if (p != null) out.push({ k: `Phase ${ph.toUpperCase()} Power`,   v: fmt(p, 2) + ' kW' });
        if (pf != null) out.push({ k: `Phase ${ph.toUpperCase()} PF`,     v: fmt(pf, 2) });
      });
      if (m.frequency != null) out.push({ k: 'Frequency', v: fmt(m.frequency, 2) + ' Hz' });
      return out;
    };

    meters.forEach((m, idx) => {
      const total = (m.power_a || 0) + (m.power_b || 0) + (m.power_c || 0);
      strip.appendChild(deviceCard({
        icon: 'fa-solid fa-plug', title: m.name || `Meter ${idx + 1}`, model: m.model || 'Grid Meter',
        value: fmt(total, 1) + ' kW', live: !!m.online, detail: phaseRows(m)
      }));
    });
    inverters.forEach((inv, idx) => {
      const power = inv.ac_power_kw != null ? inv.ac_power_kw : inv.power_kw;
      strip.appendChild(deviceCard({
        icon: 'fa-solid fa-sun', title: inv.name || `Inverter ${idx + 1}`, model: inv.model || 'PV Inverter',
        value: fmt(power, 1) + ' kW', live: !!inv.online,
        detail: [
          { k: 'AC Power', v: fmt(power, 2) + ' kW' },
          { k: 'DC Voltage', v: fmt(inv.dc_voltage, 1) + ' V' },
          { k: 'DC Current', v: fmt(inv.dc_current, 2) + ' A' },
          { k: 'Temperature', v: fmt(inv.temperature, 1) + ' °C' }
        ]
      }));
    });
    batteries.forEach((b, idx) => {
      strip.appendChild(deviceCard({
        icon: 'fa-solid fa-battery-half', title: b.name || `BESS ${idx + 1}`, model: b.model || 'Battery',
        value: 'SOC ' + fmt(b.soc, 0) + '%', live: !!b.online,
        detail: [
          { k: 'SOC', v: fmt(b.soc, 0) + ' %' },
          { k: 'Voltage', v: fmt(b.voltage, 1) + ' V' },
          { k: 'Current', v: fmt(b.current, 2) + ' A' },
          { k: 'Temperature', v: fmt(b.temperature, 1) + ' °C' }
        ]
      }));
    });
    dgs.forEach((g, idx) => {
      strip.appendChild(deviceCard({
        icon: 'fa-solid fa-gas-pump', title: g.name || `DG ${idx + 1}`, model: g.model || 'Diesel Genset',
        value: g.running ? 'RUNNING' : 'IDLE', live: !!g.online,
        detail: [{ k: 'Runtime today', v: (g.runtime_today_min || 0) + ' min' }]
      }));
    });

    if (!strip.children.length) {
      strip.innerHTML = '<div class="mg-mute" style="padding:.75rem;">No devices configured. Visit Setup to add meters, inverters, BESS or DG.</div>';
    }
  }

  function deriveDevices(data) {
    // Best-effort mapping from the existing latest_data shape
    const out = { meters: [], inverters: [], batteries: [], dgs: [] };
    if (!data) return out;
    const md = data.meters || data.METER || [];
    const inv = data.inverters || data.INVERTER || [];
    const bat = data.batteries || data.BATTERY || [];
    const dg  = data.dgs || data.DG || [];
    const cp = (a) => Array.isArray(a) ? a : Object.values(a || {});
    out.meters = cp(md);
    out.inverters = cp(inv);
    out.batteries = cp(bat);
    out.dgs = cp(dg);
    return out;
  }

  function update(data) {
    if (!data) return;
    const cur = data.forrixguard_current_demand_kw;
    const proj = data.forrixguard_projected_demand_kw;
    const allowed = data.forrixguard_allowed_kw;
    const sanctioned = data.forrixguard_sanctioned_kw || allowed;
    const correction = data.forrixguard_required_correction_kw || 0;
    const breach = !!data.forrixguard_breach_risk;
    const tlSec = data.forrixguard_time_left_seconds;
    const ccy = data.fg_tariff_currency || 'INR';
    const controlAllowed = !!data.forrixguard_control_command_allowed;

    $('mgCurrent').textContent = fmt(cur, 1);
    $('mgProjected').textContent = fmt(proj, 1);
    $('mgAllowed').textContent = fmt(allowed, 1);
    $('mgWindow').textContent = fmtCountdown(tlSec);
    $('mgForecast').textContent = fmt(data.fg_next_window_forecast_kw, 1);

    const headroom = (allowed || 0) - (cur || 0);
    $('mgHeadroom').innerHTML = (headroom >= 0 ? '+' : '') + fmt(headroom, 0) + ' <span class="unit">kW</span>';
    $('mgCorrection').innerHTML = fmt(correction, 0) + ' <span class="unit">kW</span>';

    const pct = allowed > 0 ? (cur / allowed) * 100 : 0;
    setBar('mgBarFill', pct);
    setStatusBadge(pct, breach);

    // Power flow — best-effort derivation
    const dev = deriveDevices(data);
    const solarKw = dev.inverters.reduce((s, i) => s + (i.ac_power_kw || i.power_kw || 0), 0);
    const bessKw  = dev.batteries.reduce((s, b) => s + (b.power_kw || 0), 0);
    const gridKw  = (dev.meters[0] && (dev.meters[0].power_total != null ? dev.meters[0].power_total
                      : ((dev.meters[0].power_a || 0) + (dev.meters[0].power_b || 0) + (dev.meters[0].power_c || 0)))) || cur;
    const loadKw  = (gridKw || 0) + (solarKw || 0) - (bessKw || 0);
    $('mgFlowSolar').textContent = fmt(solarKw, 1);
    $('mgFlowBess').textContent  = fmt(bessKw, 1);
    $('mgFlowGrid').textContent  = fmt(gridKw, 1);
    $('mgFlowLoad').textContent  = fmt(loadKw, 1);
    $('mgFlowBessMode').textContent = bessKw > 0.1 ? 'Discharging' : (bessKw < -0.1 ? 'Charging' : 'Idle');

    // Battery first card
    const b0 = dev.batteries[0] || {};
    $('mgSoc').textContent = fmt(b0.soc, 0);
    $('mgSocFill').style.width = clamp(b0.soc || 0, 0, 100) + '%';
    $('mgDischargeAvail').textContent = b0.discharge_available_kwh ? fmt(b0.discharge_available_kwh, 0) + ' kWh' : '—';
    $('mgBessMode').textContent = (b0.mode || 'IDLE').toUpperCase();

    // Tariff
    const tariffLoaded = !!data.fg_tariff_loaded;
    $('mgCurrency').textContent = ccy;
    if (tariffLoaded) {
      $('mgSavingsToday').textContent = fmtMoney(data.fg_savings_today, ccy);
      $('mgChargeRate').textContent = fmtMoney(data.fg_demand_charge_per_kva, ccy) + ' /kVA/mo';
      $('mgProjCharge').textContent = fmtMoney(data.fg_projected_demand_charge, ccy);
      $('mgBreachCost').textContent = fmtMoney(data.fg_breach_cost_if_hit, ccy);
      $('mgTariffMissing').style.display = 'none';
    } else {
      ['mgSavingsToday','mgChargeRate','mgProjCharge','mgBreachCost'].forEach(id => $(id).textContent = '—');
      $('mgTariffMissing').style.display = 'block';
    }

    // Devices strip
    renderDevices(dev);

    // Connection / freshness
    const fresh = data.forrixguard_telemetry_fresh !== false;
    const dot = $('mgConnDot');
    dot.classList.remove('live', 'warn', 'down');
    if (data.redis_connected === false) { dot.classList.add('down'); $('mgConnText').textContent = 'Backend offline'; }
    else if (!fresh) { dot.classList.add('warn'); $('mgConnText').textContent = 'Telemetry stale'; }
    else { dot.classList.add('live'); $('mgConnText').textContent = 'Live'; }

    $('mgUpdatedAt').textContent = new Date().toLocaleTimeString();

    // Site name
    if (data.site_name) $('mgSiteName').textContent = data.site_name;

    // Alarm
    const alarmKey = breach ? 'breach' : (pct >= 80 ? 'caution' : 'safe');
    if (alarmKey !== lastBreachState) {
      if (alarmKey === 'breach') {
        const recKw = data.forrixguard_recommendation_kw || correction || 0;
        const verb = controlAllowed ? 'Recommended (auto-dispatch enabled)' : 'Recommended (advisory only)';
        showAlarm('danger', `BREACH RISK — projected ${fmt(proj,0)} kW vs allowed ${fmt(allowed,0)} kW. ${verb}: discharge ${fmt(recKw,0)} kW from BESS.`);
      } else if (alarmKey === 'caution') {
        showAlarm('warn', `CAUTION — demand at ${fmt(pct,0)}% of allowed (${fmt(cur,0)} / ${fmt(allowed,0)} kW). Monitoring closely.`);
      } else if (lastBreachState && lastBreachState !== 'safe') {
        showAlarm('safe', 'Demand back within safe range.');
      }
      lastBreachState = alarmKey;
    }

    // Monthly KPIs (cached from /api/monthly-report)
    if (monthlyCache) {
      $('mgMtdPeak').textContent = fmt(monthlyCache.peak_kw, 0);
      $('mgSanctioned').textContent = fmt(sanctioned, 0);
      const mpct = sanctioned > 0 ? (monthlyCache.peak_kw / sanctioned) * 100 : 0;
      setBar('mgMtdFill', mpct);
      $('mgExposure').textContent = tariffLoaded ? fmtMoney(monthlyCache.exposure, ccy) : '—';
      $('mgBreachCount').textContent = monthlyCache.breach_events || 0;
      if (monthlyCache.month) $('mgMtdMonth').textContent = monthlyCache.month;
    }
  }

  async function fetchData() {
    try {
      const r = await fetch('/api/data', { credentials: 'same-origin' });
      if (r.status === 401) { window.location.href = '/login'; return; }
      const d = await r.json();
      update(d);
    } catch (e) {
      const dot = $('mgConnDot');
      dot.classList.remove('live', 'warn'); dot.classList.add('down');
      $('mgConnText').textContent = 'Disconnected';
    }
  }

  async function fetchMonthly() {
    try {
      const r = await fetch('/api/monthly-report', { credentials: 'same-origin' });
      if (!r.ok) return;
      const d = await r.json();
      monthlyCache = {
        peak_kw: d.peak_kw || d.mtd_peak_kw || 0,
        exposure: d.demand_charge || d.exposure || 0,
        breach_events: d.breach_events || (d.events || []).filter(e => /breach/i.test(e.code || '')).length || 0,
        month: d.month_label || d.month || null
      };
    } catch (_) { /* ignore */ }
  }

  function start() {
    fetchMonthly();
    fetchData();
    pollTimer = setInterval(fetchData, 2000);
    setInterval(fetchMonthly, 60000);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', start);
  else start();
})();
