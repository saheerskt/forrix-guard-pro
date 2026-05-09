#!/usr/bin/env python3
"""
Simple Web Dashboard for 3-Phase Inverter Monitoring using Redis Streams
"""

import asyncio
import collections
import csv
import io
from aiohttp import web, WSMsgType
import redis.asyncio as redis
import json
import os
import re
import time
import math
import hashlib
import secrets
import logging
import subprocess
from datetime import datetime
import jinja2

# Suppress aiohttp access logs
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)

# Configure module logger
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)  # Set to DEBUG in development, INFO in production

# Import firmware update module
try:
    from fw_update import (
        handle_system_update, 
        handle_forrixguard_app_update, 
        handle_uboot_update,
        update_status
    )
    HAS_FW_UPDATE = True
except ImportError as e:
    HAS_FW_UPDATE = False
    logger.warning("fw_update module not available: %s", e)

# Global variable to track last API call time for data_status decay
last_api_call_time = 0

# Get the directory of the script
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# Authentication Globals
AUTH_FILE = os.path.join(SCRIPT_DIR, 'auth.json')
FORRIXGUARD_CONFIG_PATH = os.environ.get('FORRIXGUARD_CONFIG_PATH', '/etc/forrixguard-support/ForrixGuard.json')
COOKIE_NAME = 'forrixguard_session'
SESSIONS = {}  # In-memory sessions {session_token: username}

def load_users():
    """Load users from auth.json, create default if missing"""
    default_users = {
        'admin': hashlib.sha256('admin'.encode()).hexdigest(),
        'user': hashlib.sha256('user'.encode()).hexdigest()
    }
    if not os.path.exists(AUTH_FILE):
        save_users(default_users)
        return default_users
    try:
        with open(AUTH_FILE, 'r') as f:
            users = json.load(f)
            
        # Initialize user if missing (backward compatibility)
        if 'user' not in users:
            users['user'] = hashlib.sha256('user'.encode()).hexdigest()
            save_users(users)
            
        return users
    except Exception:
        return default_users

def save_users(users):
    """Save users to auth.json"""
    with open(AUTH_FILE, 'w') as f:
        json.dump(users, f, indent=2)

async def auth_middleware(app, handler):
    """Middleware to check authentication"""
    async def middleware_handler(request):
        # Allow static files and login endpoints
        if request.path.startswith('/static') or \
           request.path in ['/login', '/api/login']:
            return await handler(request)
        
        # Check cookie
        token = request.cookies.get(COOKIE_NAME)
        if not token or token not in SESSIONS:
            if request.path.startswith('/api'):
                return web.json_response({'error': 'Unauthorized'}, status=401)
            return web.HTTPFound('/login')
            
        return await handler(request)
    return middleware_handler




def load_forrixguard_config():
    try:
        with open(FORRIXGUARD_CONFIG_PATH, 'r') as f:
            config = json.load(f)
        return config
    except Exception as e:
        logger.error(f"Error loading ForrixGuard config: {e}")
        return None

def sync_serial_from_fuse():
    """
    Sync serial_number in ForrixGuard.json with OTP fuse value if available.
    Uses /tmp/.serial_synced to avoid repeated operations.
    """
    sync_flag = '/tmp/.serial_synced'
    fuse_path = '/sys/bus/nvmem/devices/fsb_s400_fuse0/nvmem'
    
    if os.path.exists(sync_flag):
        logger.debug("Serial sync flag exists, skipping check")
        return

    logger.info("Checking hardware serial number from OTP fuses...")
    try:
        if not os.path.exists(fuse_path):
            logger.warning(f"Fuse path {fuse_path} not found, skipping serial sync")
            return

        # Read serial from fuse (offset 1504, length 10)
        with open(fuse_path, 'rb') as f:
            f.seek(1504)
            fuse_serial = f.read(10).decode('ascii', errors='ignore').strip('\x00').strip()

        logger.info(f"Hardware serial from fuse: {fuse_serial}")
        
        # Validation: 10 chars, starts with C or H, printable ASCII
        is_valid = (len(fuse_serial) == 10 and 
                    fuse_serial[0] in ('C', 'H') and 
                    all(32 <= ord(c) <= 126 for c in fuse_serial))

        if is_valid:
            config = load_forrixguard_config()
            if config:
                current_serial = config.get('serial_number')
                if current_serial != fuse_serial:
                    logger.info(f"Syncing serial from fuse: {current_serial} -> {fuse_serial}")
                    config['serial_number'] = fuse_serial
                    with open(FORRIXGUARD_CONFIG_PATH, 'w') as f:
                        json.dump(config, f, indent=4)
                    logger.info("Updated ForrixGuard.json with hardware serial")
                else:
                    logger.info(f"Serial in ForrixGuard.json already matches hardware: {fuse_serial}")
                
                # Create flag file
                with open(sync_flag, 'w') as f:
                    f.write(fuse_serial)
            else:
                logger.error("Failed to load ForrixGuard.json for serial sync")
        else:
            logger.warning(f"Invalid serial format in fuse (must start with C/H and be 10 ASCII chars): {fuse_serial}")
    except Exception as e:
        logger.error(f"Error syncing serial from fuse: {e}")

# Global version caching to avoid repeatedly calling rpm
GLOBAL_APP_VERSION = "0"
def get_forrixguard_app_version():
    """Get the current package version of forrixguard-app to use as cache buster."""
    global GLOBAL_APP_VERSION
    try:
        if GLOBAL_APP_VERSION == "0":
            res = subprocess.run(["rpm", "-q", "forrixguard-app", "--queryformat", "%{VERSION}-%{RELEASE}"], 
                                 capture_output=True, text=True)
            if res.returncode == 0:
                # Sanitise to remove special characters just in case
                GLOBAL_APP_VERSION = ''.join(c for c in res.stdout.strip() if c.isalnum() or c in '.-_')
            else:
                GLOBAL_APP_VERSION = str(int(time.time()))
    except Exception as e:
        logger.error(f"Error getting forrixguard-app version: {e}")
        GLOBAL_APP_VERSION = str(int(time.time()))
    return GLOBAL_APP_VERSION

GLOBAL_MONITOR_VERSION = "0"
def get_forrixguard_monitor_version():
    """Get the current package version of forrixguard-monitor to use in footer."""
    global GLOBAL_MONITOR_VERSION
    try:
        if GLOBAL_MONITOR_VERSION == "0":
            res = subprocess.run(["rpm", "-q", "forrixguard-app", "--queryformat", "%{VERSION}-%{RELEASE}"], 
                                 capture_output=True, text=True)
            if res.returncode == 0:
                GLOBAL_MONITOR_VERSION = res.stdout.strip()
            else:
                GLOBAL_MONITOR_VERSION = "Unknown"
    except Exception as e:
        logger.error(f"Error getting forrixguard-monitor version: {e}")
        GLOBAL_MONITOR_VERSION = "Unknown"
    return GLOBAL_MONITOR_VERSION

app = web.Application(middlewares=[auth_middleware])

def build_default_data(config):
    """Create a default latest_data dict based on ForrixGuard config (0-based indices)."""
    defaults = {
        "device_id": current_device,
        "timestamp": int(time.time() * 1000),
        "messageId": None,
        "raw_telemetry": [],
        "forrixguard_current_demand_kw": 0,
        "forrixguard_projected_demand_kw": 0,
        "forrixguard_sanctioned_kw": 0,
        "forrixguard_allowed_kw": 0,
        "forrixguard_required_correction_kw": 0,
        "forrixguard_estimated_penalty_kw": 0,
        "forrixguard_savings_opportunity_kw": 0,
        "forrixguard_demand_margin_kw": 0,
        "forrixguard_time_left_seconds": 0,
        "forrixguard_breach_risk": False,
        "forrixguard_event_cause": "unknown",
        "forrixguard_telemetry_fresh": True,
        "forrixguard_telemetry_age_seconds": 0,
        "forrixguard_recommendation_action": "monitor",
        "forrixguard_recommendation_kw": 0,
        "forrixguard_control_command_allowed": False,
        "forrixguard_events": [],
        "forrixguard_grid_import_kw": None,
        "forrixguard_solar_kw": None,
        "forrixguard_load_kw": None,
        "forrixguard_bess_kw": None,
        "forrixguard_dg_status": "Not configured",
        "forrixguard_telemetry_mapping": {},
    }

    # Add global setpoints based on actual inverter count
    # Fallback to 3 if config not loaded yet, but try to use config if available
    num_inverters = 3
    if config and 'devices' in config:
        num_inverters = sum(1 for d in config['devices'] if d.get('type') == 'INVERTER')
    
    for offset in range(num_inverters):
        for phase in ['A', 'B', 'C']:
            defaults[f"inv{offset}_phase{phase}_setpoint"] = 0

    if not config or 'devices' not in config:
        return defaults

    devices = config['devices']
    meter_num = 0
    inverter_num = 0
    battery_num = 0
    mppt_num = 0
    dg_num = 0

    for device in devices:
        dtype = device.get('type')
        if dtype == 'METER':
            defaults.update({
                f"m{meter_num}_voltage": 0,
                f"m{meter_num}_frequency": 0,
                f"m{meter_num}_current": 0,
                f"m{meter_num}_power": 0,
                f"m{meter_num}_reactive_power": 0,
                f"m{meter_num}_phaseB_voltage": 0,
                f"m{meter_num}_phaseB_frequency": 0,
                f"m{meter_num}_phaseB_current": 0,
                f"m{meter_num}_phaseB_power": 0,
                f"m{meter_num}_phaseB_reactive_power": 0,
                f"m{meter_num}_phaseC_voltage": 0,
                f"m{meter_num}_phaseC_frequency": 0,
                f"m{meter_num}_phaseC_current": 0,
                f"m{meter_num}_phaseC_power": 0,
                f"m{meter_num}_phaseC_reactive_power": 0,
            })
            meter_num += 1
        elif dtype == 'INVERTER':
            defaults.update({
                f"inv{inverter_num}_status": None,
                f"inv{inverter_num}_fault_code": 0,
                f"inv{inverter_num}_phaseA_voltage": 0,
                f"inv{inverter_num}_phaseA_power": 0,
                f"inv{inverter_num}_phaseA_reactive_power": 0,
                f"inv{inverter_num}_phaseA_setpoint": 0,
                f"inv{inverter_num}_temperature": 0,
                f"inv{inverter_num}_phaseB_voltage": 0,
                f"inv{inverter_num}_phaseB_power": 0,
                f"inv{inverter_num}_phaseB_reactive_power": 0,
                f"inv{inverter_num}_phaseB_setpoint": 0,
                f"inv{inverter_num}_phaseC_voltage": 0,
                f"inv{inverter_num}_phaseC_power": 0,
                f"inv{inverter_num}_phaseC_reactive_power": 0,
                f"inv{inverter_num}_phaseC_setpoint": 0,
            })
            inverter_num += 1
        elif dtype == 'BATTERY':
            defaults.update({
                f"battery{battery_num}_soc": 0,
                f"battery{battery_num}_voltage": 0,
                f"battery{battery_num}_temp": 0,
            })
            battery_num += 1
        elif dtype == 'MPPT':
            defaults.update({
                f"mppt{mppt_num}_fault_code": 0,
                f"mppt{mppt_num}_output_voltage": 0,
                f"mppt{mppt_num}_total_power": 0,
                f"mppt{mppt_num}_temperature": 0,
                f"mppt{mppt_num}_status": "unknown",
                # Phase-specific data for averaging
                f"mppt{mppt_num}_phaseA_total_power": 0,
                f"mppt{mppt_num}_phaseB_total_power": 0,
                f"mppt{mppt_num}_phaseC_total_power": 0,
            })
            mppt_num += 1
        elif dtype == 'DG':
            defaults.update({
                f"dg{dg_num}_status": "unknown",
                f"dg{dg_num}_power_kw": 0,
                f"dg{dg_num}_runtime_min": 0,
            })
            dg_num += 1
    return defaults

REDIS_HOST = os.getenv('REDIS_HOST', '127.0.0.1') 
REDIS_PORT = 6379

# Global variables
latest_data = {}
latest_datasheet = []
latest_topology = {"nodes": {}, "edges": [], "pins": {}}
latest_timeslots = []
_timeslots_version = 0
latest_events = []
telemetry_proof_state = {}
redis_client = None

# ---------- Load Profile Store ----------
# In-memory EMA load profile: key=(dow 0-6, slot 0-95), value=avg_kw
# dow: 0=Monday … 6=Sunday  |  slot: 0-95 (each = 15 min)
_lp_profile: dict = {}        # {(dow, slot): float}  EMA average kW
_lp_counts:  dict = {}        # {(dow, slot): int}    number of samples seen
_lp_dirty:   bool = False
_lp_recent_actuals: collections.deque = collections.deque(maxlen=6)  # last 6 completed windows
_lp_last_slot: tuple = None   # (dow, slot) of the current 15-min block
_lp_last_kw:   float = 0.0    # kW seen in the last demand tick
_LP_ALPHA = 0.10              # EMA factor ≈ 10-window weighted average
_LP_REDIS_PREFIX = "fg:load_profile"


class LoadProfileStore:
    """15-minute demand load profile: EMA over historical windows, per day-of-week slot."""

    @staticmethod
    def _slot_now() -> tuple:
        now = datetime.now()
        return now.weekday(), (now.hour * 60 + now.minute) // 15

    @staticmethod
    def update(dow: int, slot: int, kw: float):
        global _lp_dirty
        key = (dow, slot)
        if key in _lp_profile:
            _lp_profile[key] = _LP_ALPHA * kw + (1 - _LP_ALPHA) * _lp_profile[key]
            _lp_counts[key] = min(_lp_counts.get(key, 0) + 1, 9999)
        else:
            _lp_profile[key] = kw
            _lp_counts[key] = 1
        _lp_dirty = True

    @staticmethod
    def get_slot(dow: int, slot: int) -> float:
        return _lp_profile.get((dow, slot), 0.0)

    @staticmethod
    def get_count(dow: int, slot: int) -> int:
        return _lp_counts.get((dow, slot), 0)

    @staticmethod
    def get_profile(dow: int) -> list:
        return [round(_lp_profile.get((dow, s), 0.0), 2) for s in range(96)]

    @staticmethod
    async def load_from_redis():
        global redis_client
        if not redis_client:
            return
        try:
            for dow in range(7):
                data   = await redis_client.hgetall(f"{_LP_REDIS_PREFIX}:{dow}")
                counts = await redis_client.hgetall(f"{_LP_REDIS_PREFIX}_count:{dow}")
                for slot_b, val_b in data.items():
                    try:
                        s = int(slot_b)
                        _lp_profile[(dow, s)] = float(val_b)
                        _lp_counts[(dow, s)]  = int(counts.get(slot_b, 1))
                    except (ValueError, TypeError):
                        pass
            logger.info(f"Load profile restored: {len(_lp_profile)} slots from Redis")
        except Exception as e:
            logger.warning(f"Could not load load profile from Redis: {e}")

    @staticmethod
    async def persist_to_redis():
        global redis_client, _lp_dirty
        if not redis_client or not _lp_dirty:
            return
        try:
            for dow in range(7):
                prof_map  = {}
                count_map = {}
                for slot in range(96):
                    key = (dow, slot)
                    if key in _lp_profile:
                        prof_map[str(slot)]  = str(round(_lp_profile[key], 3))
                        count_map[str(slot)] = str(_lp_counts.get(key, 1))
                if prof_map:
                    await redis_client.hset(f"{_LP_REDIS_PREFIX}:{dow}", mapping=prof_map)
                    await redis_client.hset(f"{_LP_REDIS_PREFIX}_count:{dow}", mapping=count_map)
            _lp_dirty = False
        except Exception as e:
            logger.warning(f"Could not persist load profile to Redis: {e}")


def _check_window_transition(current_kw: float, allowed_kw: float):
    """Detect 15-min window boundary by wall-clock slot; update profile and forecast.
    Returns (forecast_kw, breach_risk, confidence) — unchanged if no transition."""
    global _lp_last_slot, _lp_last_kw

    dow, slot = LoadProfileStore._slot_now()
    prev_forecast_kw    = latest_data.get('fg_next_window_forecast_kw', 0.0)
    prev_breach_risk    = latest_data.get('fg_forecast_breach_risk', False)
    prev_confidence     = latest_data.get('fg_forecast_confidence', 0.0)

    if _lp_last_slot is not None and (dow, slot) != _lp_last_slot:
        prev_dow, prev_slot = _lp_last_slot
        completed_kw = _lp_last_kw
        LoadProfileStore.update(prev_dow, prev_slot, completed_kw)
        _lp_recent_actuals.append(completed_kw)
        asyncio.get_event_loop().create_task(LoadProfileStore.persist_to_redis())

        historical = LoadProfileStore.get_slot(dow, slot)
        count      = LoadProfileStore.get_count(dow, slot)

        if count >= 2 and historical > 0:
            recent_avg    = sum(_lp_recent_actuals) / len(_lp_recent_actuals)
            forecast_kw   = round(0.7 * historical + 0.3 * recent_avg, 1)
            confidence    = round(min(1.0, count / 10.0), 2)
        elif historical > 0:
            forecast_kw   = round(historical, 1)
            confidence    = round(count / 10.0, 2)
        else:
            forecast_kw   = round(current_kw, 1)
            confidence    = 0.0

        breach_risk = forecast_kw > allowed_kw * 0.90 if allowed_kw > 0 else False
        _lp_last_slot = (dow, slot)
        _lp_last_kw   = current_kw
        return forecast_kw, breach_risk, confidence

    _lp_last_slot = (dow, slot)
    _lp_last_kw   = current_kw
    return prev_forecast_kw, prev_breach_risk, prev_confidence
# ----------------------------------------
consumer_task = None
multi_dashboard_template = None
phase_power_comparison_template = None
time_slots_template = None
forrixguard_setup_template = None
login_template = None
monthly_report_template = None

# Load device ID from ForrixGuard config
try:
    with open(FORRIXGUARD_CONFIG_PATH, 'r') as f:
        forrixguard_config = json.load(f)
        current_device = forrixguard_config.get('serial_number', 'FORRIXGUARD000111')
except Exception as e:
    logger.warning(f"Could not load ForrixGuard config: {e}, using default device ID")
    current_device = 'FORRIXGUARD000001'

# Initialize latest_data with defaults so UI always has expected keys
config = load_forrixguard_config()
latest_data = build_default_data(config)
logger.info(f"Initialized latest_data with {len(latest_data)} keys")

is_connected = False  # Start disconnected
connected_websockets = set()

async def send_to_clients(message_type, data):
    for ws in connected_websockets.copy():
        try:
            await ws.send_json({'type': message_type, 'data': data})
        except Exception:
            connected_websockets.discard(ws)

def count_type_before(devices, device_index, device_type):
    return sum(1 for d in devices[:device_index] if str(d.get('type', '')).upper() == device_type)

def find_role_device(config, role, device_type=None):
    devices = (config or {}).get('devices', [])
    # Primary: match by role field (new config format)
    for idx, device in enumerate(devices):
        if device.get('role') == role and (device_type is None or device.get('type') == device_type):
            type_index = count_type_before(devices, idx, device.get('type', 'UNKNOWN'))
            return idx, type_index, device
    # Fallback: match by device_type only — use first device of that type (old config without role field)
    if device_type:
        for idx, device in enumerate(devices):
            if device.get('type') == device_type:
                type_index = count_type_before(devices, idx, device_type)
                return idx, type_index, device
    return None, None, None

def number_or_none(value):
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None

def meter_total_kw(data, meter_index):
    vals = [
        number_or_none(data.get(f"m{meter_index}_power")),
        number_or_none(data.get(f"m{meter_index}_phaseB_power")),
        number_or_none(data.get(f"m{meter_index}_phaseC_power")),
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / 1000.0, 3)

def inverter_total_kw(data, inverter_index):
    vals = [
        number_or_none(data.get(f"inv{inverter_index}_phaseA_power")),
        number_or_none(data.get(f"inv{inverter_index}_phaseB_power")),
        number_or_none(data.get(f"inv{inverter_index}_phaseC_power")),
    ]
    vals = [v for v in vals if v is not None]
    if not vals:
        return None
    return round(sum(vals) / 1000.0, 3)

def battery_power_kw(data, battery_index):
    value = number_or_none(data.get(f"battery{battery_index}_power"))
    if value is None:
        return None
    return round(value / 1000.0 if abs(value) > 100 else value, 3)

def field_age_seconds(data, field):
    timestamps = data.get('timestamps') or {}
    ts = timestamps.get(field)
    if not ts:
        return None
    return max(0, time.time() - ts)

def add_proof_event_from_telemetry(code, severity, message, details):
    global latest_events, latest_data
    timestamp = int(time.time() * 1000)
    event = {
        "stream_id": f"telemetry-{code}-{timestamp}",
        "timestamp": timestamp,
        "time": datetime.fromtimestamp(timestamp / 1000).isoformat(),
        "severity": severity,
        "category": "site_telemetry",
        "event_code": code,
        "source": "telemetry_mapping",
        "message": message,
        "details": details,
    }
    latest_events.append(event)
    latest_events = latest_events[-100:]
    latest_data["forrixguard_events"] = list(reversed(latest_events[-20:]))

def set_proof_state_once(key, active, code, severity, message, details):
    previous = telemetry_proof_state.get(key)
    if previous == active:
        return
    telemetry_proof_state[key] = active
    if active:
        add_proof_event_from_telemetry(code, severity, message, details)

def update_real_telemetry_mapping(data, config):
    mapping = (config or {}).get('telemetryMapping', {})
    rules = (config or {}).get('proofRules', {})

    _, grid_index, grid_device = find_role_device(config, mapping.get('gridImport', {}).get('role', 'GRID_METER'), 'METER')
    _, solar_inv_index, solar_device = find_role_device(config, mapping.get('solar', {}).get('role', 'SOLAR_INVERTER'), 'INVERTER')
    _, load_index, load_device = find_role_device(config, mapping.get('load', {}).get('role', 'LOAD_METER'), 'METER')
    _, bess_index, bess_device = find_role_device(config, mapping.get('bess', {}).get('role', 'BESS'), 'BATTERY')

    grid_kw = meter_total_kw(data, grid_index) if grid_index is not None else None
    solar_kw = inverter_total_kw(data, solar_inv_index) if solar_inv_index is not None else None
    load_kw = meter_total_kw(data, load_index) if load_index is not None else None
    bess_kw = battery_power_kw(data, bess_index) if bess_index is not None else None

    if load_kw is None:
        load_kw = round(sum(v for v in [grid_kw, max(solar_kw or 0, 0), max(bess_kw or 0, 0)] if v is not None), 3)

    dg_cfg = mapping.get('dg', {})
    dg_status = 'Not configured'
    if dg_cfg.get('enabled'):
        dg_status = data.get('forrixguard_dg_status', 'Available')

    data['forrixguard_grid_import_kw'] = grid_kw
    data['forrixguard_solar_kw'] = solar_kw
    data['forrixguard_load_kw'] = load_kw
    data['forrixguard_bess_kw'] = bess_kw
    data['forrixguard_dg_status'] = dg_status
    data['forrixguard_telemetry_mapping'] = {
        'grid_import': grid_device.get('displayName', 'Grid meter') if grid_device else 'Not mapped',
        'solar': solar_device.get('displayName', 'Solar inverter') if solar_device else 'Not mapped',
        'load': load_device.get('displayName', 'Calculated load') if load_device else 'Calculated load',
        'bess': bess_device.get('displayName', 'Battery') if bess_device else 'Not mapped',
        'dg': 'Optional DG' if dg_cfg.get('enabled') else 'Not configured',
    }

    expected_solar = number_or_none(rules.get('expectedSolarKw')) or 0
    solar_ratio = number_or_none(rules.get('solarUnderperformanceRatio')) or 0.65
    bess_threshold = number_or_none(rules.get('bessDispatchThresholdKw')) or 5
    stale_seconds = number_or_none(rules.get('meterStaleSeconds')) or 20

    if grid_index is not None:
        age_values = [
            field_age_seconds(data, f"m{grid_index}_power"),
            field_age_seconds(data, f"m{grid_index}_phaseB_power"),
            field_age_seconds(data, f"m{grid_index}_phaseC_power"),
        ]
        valid_ages = [age for age in age_values if age is not None]
        stale = bool(valid_ages and min(valid_ages) > stale_seconds)
        set_proof_state_once('grid_meter_stale', stale, 'METER_COMM_LOST', 'warning',
                             'Grid meter telemetry is stale; verify communication.',
                             {'action': 'Check meter communication freshness',
                              'result': 'Operator can see data-quality issue before billing review',
                              'device': data['forrixguard_telemetry_mapping']['grid_import']})

    solar_low = bool(expected_solar > 0 and solar_kw is not None and solar_kw < expected_solar * solar_ratio and (grid_kw or 0) > 0)
    set_proof_state_once('solar_underperformance', solar_low, 'SOLAR_UNDERPERFORMANCE', 'warning',
                         'Solar contribution is below expected level during grid import.',
                         {'action': 'Compare live solar kW with commissioned expectation',
                          'result': 'Report can explain why grid demand increased',
                          'expected_solar_kw': expected_solar,
                          'actual_solar_kw': solar_kw or 0,
                          'grid_import_kw': grid_kw or 0})

    bess_dispatch = bool(bess_kw is not None and bess_kw > bess_threshold)
    set_proof_state_once('bess_dispatch', bess_dispatch, 'BESS_DISPATCHED', 'info',
                         'Battery support detected from live telemetry.',
                         {'action': 'Use BESS before trip relay',
                          'result': 'Projected peak can be corrected without immediate load shedding',
                          'bess_kw': bess_kw or 0,
                          'grid_import_kw': grid_kw or 0})

    load_shed_avoided = bool(data.get('forrixguard_breach_risk') and bess_dispatch)
    set_proof_state_once('load_shed_avoided', load_shed_avoided, 'LOAD_SHED_AVOIDED', 'info',
                         'Load shedding avoided by battery support during demand risk.',
                         {'action': 'Apply battery correction before shedding load',
                          'result': 'Production/HVAC interruption avoided',
                          'bess_kw': bess_kw or 0,
                          'projected_kw': data.get('forrixguard_projected_demand_kw', 0),
                          'allowed_kw': data.get('forrixguard_allowed_kw', 0)})

    dg_running = str(dg_status).lower() in ('running', 'on', 'active', 'available')
    set_proof_state_once('dg_started', bool(dg_cfg.get('enabled') and dg_running), 'DG_STARTED', 'info',
                         'DG status is active and runtime proof logging is available.',
                         {'action': 'Log DG status against demand event',
                          'result': 'DG runtime can be reviewed with demand timeline'})

def process_message(payload):
    """Process incoming message from meters_stream or inverters_stream"""
    global latest_data, latest_datasheet, latest_topology, is_connected
    
    def set_data_new(data_dict, status_dict, key, value):
        """Helper to set data and mark it as new (true) in status"""
        data_dict[key] = value
        status_dict[key] = True
        # Add timestamp for decay mechanism
        if 'timestamps' not in data_dict:
            data_dict['timestamps'] = {}
        data_dict['timestamps'][key] = time.time()
    
    try:
        # Load config early so we can use it for data_status initialization
        config = load_forrixguard_config()
        
        data = json.loads(payload)
        
        # For Redis streams with flat fields
        # Support both old field names (backward compat) and new compressed names from C++ backend
        device_trait_id = int(data.get("d") or data.get("device_trait_id", 0))
        
        # Reading: old format is float string, new format is int string (divide by 1000)
        # Accept both integer-string and float-string representations (e.g. '12345' or '12345.000000')
        reading_raw = data.get("r") or data.get("reading")
        reading = None
        reading_int = None
        if reading_raw is not None:
            try:
                # Some backends send values as decimal strings ("1234.000000").
                # Use float->int to handle both integer and decimal forms safely.
                reading_int = int(float(str(reading_raw)))
            except Exception:
                try:
                    # Fallback: strip fractional part if present and parse integer portion
                    reading_int = int(str(reading_raw).split('.', 1)[0])
                except Exception:
                    # Could not parse; leave reading_int as None and continue
                    reading_int = None
            # Default: inputs are milli-units except power/reactive which are already in W/VAR
            if reading_int is not None:
                reading = reading_int / 1000.0
        
        timestamp = int(data.get("ts") or data.get("timestamp", 0))
        status = data.get("ss") or data.get("status")  # New: "ss" (status state), Old: "status"
        fault_code_raw = data.get("f")  # Fault code field from inverter/MPPT stream
        
        # DEBUG: Log battery stream data
        device_id = device_trait_id >> 32
        trait_id = device_trait_id & 0xFFFFFFFF
        data_type = (trait_id >> 24) & 0xFF
        offset = trait_id & 0xFF
        logger.debug(f"device_trait_id={device_trait_id}, device_id={device_id}, trait_id=0x{trait_id:08x}, data_type=0x{data_type:02x}, offset={offset}, reading={reading}")
        
        # Create a shallow copy of latest_data for this message
        mapped_data = latest_data.copy() if latest_data else {}
        
        # Initialize data_status tracking with DEEP COPY to avoid race conditions
        # First time: create empty dict with all known fields initialized to True
        # Subsequent times: deep copy existing status and reset values to False
        # Only fields in THIS message will be set to True
        if "data_status" not in mapped_data:
            mapped_data["data_status"] = {}
            # First initialization: all existing fields are new
            for key in mapped_data:
                if key not in ['device_id', 'timestamp', 'messageId', 'raw_telemetry', 'data_status']:
                    mapped_data["data_status"][key] = True
        
        # CRITICAL: Deep copy data_status to prevent race conditions with concurrent messages
        import copy
        data_status = copy.deepcopy(mapped_data["data_status"])
        
        # Extract device_id and trait_id EARLY so we can use them for reset logic
        # device_trait_id format: (device_id << 32) | trait_id
        device_id = device_trait_id >> 32  # Extract upper 32 bits
        trait_id = device_trait_id & 0xFFFFFFFF  # Extract lower 32 bits
        tel_id = f"{device_id}:{trait_id}"
        
        # Get device info from config (already loaded at function start)
        device_type = 'UNKNOWN'
        devices = []
        if config and 'devices' in config:
            devices = config['devices']
            if device_id < len(devices):
                device = devices[device_id]
                device_type = device.get('type', 'UNKNOWN')
        
        # Ensure all known power/meter/inverter fields exist in data_status
        # This handles newly added fields that weren't in mapped_data yet
        known_fields = []
        if config and 'devices' in config:
            # Create list of all possible fields based on device count
            devices = config['devices']
            meter_count = sum(1 for d in devices if d.get('type') == 'METER')
            inverter_count = sum(1 for d in devices if d.get('type') == 'INVERTER')
            mppt_count = sum(1 for d in devices if d.get('type') == 'MPPT')
            
            # Add all possible meter fields
            for m in range(meter_count):
                for phase_suffix in ['', '_phaseB', '_phaseC']:
                    for measurement in ['voltage', 'frequency', 'current', 'power', 'reactive_power']:
                        known_fields.append(f"m{m}{phase_suffix}_{measurement}")
            
            # Add all possible inverter fields
            for inv in range(inverter_count):
                for phase in ['phaseA', 'phaseB', 'phaseC']:
                    for measurement in ['voltage', 'power', 'reactive_power']:
                        known_fields.append(f"inv{inv}_{phase}_{measurement}")
                known_fields.append(f"inv{inv}_status")
                known_fields.append(f"inv{inv}_fault_code")
                known_fields.append(f"inv{inv}_temperature")
            
            # Add battery fields
            for inv in range(inverter_count):
                for measurement in ['soc', 'voltage', 'current', 'power', 'frequency', 'temp', 'energy']:
                    known_fields.append(f"battery{inv}_{measurement}")
            
            # Add MPPT fields
            for mppt in range(mppt_count):
                for measurement in ['output_voltage', 'total_power', 'temperature', 'status', 'fault_code']:
                    known_fields.append(f"mppt{mppt}_{measurement}")
        
        # Initialize all known fields in data_status if not present
        for field in known_fields:
            if field not in data_status:
                data_status[field] = False
        
        # REMOVED: Reset ALL fields to FALSE at start of each message
        # Instead, use timestamp-based decay in API endpoint
        
        # Update metadata
        mapped_data["device_id"] = current_device
        mapped_data["timestamp"] = timestamp
        mapped_data["messageId"] = None
        mapped_data["raw_telemetry"] = []
        
        # Device info already extracted earlier (device_id, trait_id, device_type, devices)
        
        # Extract components from trait_id (NEW ENCODING from C++ backend)
        # trait_id structure: (data_type << 24) | (phase << 16) | offset
        # data_type: 0x01=METER, 0x02=INVERTER, 0x03=METER_DATASHEET, 0x04=INVERTER_DATASHEET
        # phase: 0x00=PhaseA, 0x01=PhaseB, 0x02=PhaseC
        # offset: measurement index (0=voltage, 1=current, 2=power, etc.)
        data_type = (trait_id >> 24) & 0xFF
        phase = (trait_id >> 16) & 0xFF
        offset = trait_id & 0xFFFF
        
        # Debug output
        dbg_val = reading if reading is not None else status
        
        # Handle status data first (doesn't have reading field)
        if device_type == 'INVERTER' and data_type == 0x02 and status is not None:
            inv_num = sum(1 for d in devices[:device_id] if d.get('type') == 'INVERTER')
            status_val = int(status) if isinstance(status, str) else status
            status_map = {0: "waiting", 1: "normal", 2: "warning", 3: "fault"}
            set_data_new(mapped_data, data_status, f"inv{inv_num}_status", status_map.get(status_val, "unknown"))
            if fault_code_raw is not None:
                try:
                    set_data_new(mapped_data, data_status, f"inv{inv_num}_fault_code", int(float(str(fault_code_raw))))
                except Exception:
                    pass

        # Handle MPPT status from mppts_stream (has "ss" field, no reading field)
        if device_type == 'MPPT' and status is not None:
            mppt_num = sum(1 for d in devices[:device_id] if d.get('type') == 'MPPT')
            status_val = int(status) if isinstance(status, str) else status
            status_map = {0: "waiting", 1: "normal", 2: "offline", 3: "fault"}
            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_status", status_map.get(status_val, "unknown"))
            if fault_code_raw is not None:
                try:
                    set_data_new(mapped_data, data_status, f"mppt{mppt_num}_fault_code", int(float(str(fault_code_raw))))
                except Exception:
                    pass
        
        # Process the data based on device type (measurement readings)
        if reading is not None:
            if device_type == 'METER':
                # Count METER devices before this one
                meter_num = sum(1 for d in devices[:device_id] if d.get('type') == 'METER')
                
                # Only process METER readings (data_type=0x01)
                if data_type == 0x01:
                    # Note: reading is already divided by 1000 from payload parsing
                    # For power/reactive_power the backend already sends W/VAR, so skip /1000
                    if offset in (3, 4) and reading_int is not None:
                        reading = float(reading_int)

                    # Phase A: phase = 0x00
                    if phase == 0x00:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_frequency", reading)
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_current", reading)
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_power", reading)
                        elif offset == 4:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_reactive_power", reading)
                    
                    # Phase B: phase = 0x01
                    elif phase == 0x01:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseB_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseB_frequency", reading)
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseB_current", reading)
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseB_power", reading)
                        elif offset == 4:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseB_reactive_power", reading)
                    
                    # Phase C: phase = 0x02
                    elif phase == 0x02:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseC_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseC_frequency", reading)
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseC_current", reading)
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseC_power", reading)
                        elif offset == 4:
                            set_data_new(mapped_data, data_status, f"m{meter_num}_phaseC_reactive_power", reading)
            
            elif device_type == 'INVERTER':
                inv_num = sum(1 for d in devices[:device_id] if d.get('type') == 'INVERTER')
                
                # Process INVERTER readings - comes in TWO forms:
                # 1. Meter-type readings (data_type=0x01) = actual inverter voltage/power/etc
                # 2. Inverter-type readings (data_type=0x02) = status/fault info
                
                if data_type == 0x01:  # Meter-type readings (actual inverter measurements)
                    # Adjust power/reactive power to use raw W/VAR (no /1000)
                    def inv_read(val_offset):
                        if reading_int is None:
                            return reading
                        if val_offset in (1, 2):  # power, reactive power
                            return float(reading_int)
                        return reading

                    # Phase A: phase = 0x00
                    if phase == 0x00:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseA_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseA_power", inv_read(1))
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseA_reactive_power", inv_read(2))
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_temperature", reading)
                    
                    # Phase B: phase = 0x01
                    elif phase == 0x01:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseB_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseB_power", inv_read(1))
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseB_reactive_power", inv_read(2))
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_temperature", reading)
                    
                    # Phase C: phase = 0x02
                    elif phase == 0x02:
                        if offset == 0:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseC_voltage", reading)
                        elif offset == 1:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseC_power", inv_read(1))
                        elif offset == 2:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_phaseC_reactive_power", inv_read(2))
                        elif offset == 3:
                            set_data_new(mapped_data, data_status, f"inv{inv_num}_temperature", reading)
                
                # Battery data is now in separate battery_stream (data_type=0x05)
                # This inverter raw trait_id handling is for legacy support only
                elif data_type == 0x00:  # Raw trait_ids (legacy battery data in inverter stream)
                    battery_num = None
                    # Search for BATTERY linked to this inverter device_id
                    for idx, d in enumerate(devices):
                        if d.get('type') == 'BATTERY':
                            link_ids = d.get('link-inverter-id', [])
                            for link in link_ids:
                                if link.get('device_id') == device_id:
                                    battery_num = d.get('device_index', 0)
                                    break
                            if battery_num is not None:
                                break

                    # Fallback: align battery index with inverter index when no explicit link found
                    if battery_num is None:
                        battery_num = inv_num
                    
                    if battery_num is not None:
                        if offset == 10:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_voltage", reading)
                        elif offset == 11:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_current", reading)
                        elif offset == 12:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_power", reading)
                        elif offset == 13:
                            soc_val = reading_int if reading_int is not None else reading
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_soc", soc_val)
                        elif offset == 14:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_frequency", reading)
                        elif offset == 20:
                            soc_val = reading_int if reading_int is not None else reading
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_soc", soc_val)
                        elif offset == 21:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_energy", reading)
                        elif offset == 22:
                            set_data_new(mapped_data, data_status, f"battery{battery_num}_temp", reading)
            
            elif device_type == 'BATTERY':
                # Handle BATTERY device type from battery_stream
                battery_num = sum(1 for d in devices[:device_id] if d.get('type') == 'BATTERY')
                
                # Note: reading is already divided by 1000 from payload parsing
                # New encoding (0x05) - offset mappings match C++ battery_reading_id enum:
                # 0=battery_soc, 1=battery_voltage, 2=battery_temperature, 3=battery_active_power
                # 4=max_battery_voltage, 5=min_battery_voltage, 6=max_battery_temperature, 7=min_battery_temperature
                if data_type == 0x05:
                    # Debug: Log what we're receiving
                    logger.debug(f"Battery data - device_id={device_id}, battery_num={battery_num}, data_type=0x{data_type:02x}, offset={offset}, reading={reading}")
                    
                    if offset == 0:
                        soc_val = reading_int if reading_int is not None else reading
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_soc", soc_val)
                    elif offset == 1:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_voltage", reading)
                    elif offset == 2:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_temp", reading)
                    elif offset == 3:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_power", reading)
                    elif offset == 4:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_max_voltage", reading)
                    elif offset == 5:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_min_voltage", reading)
                    elif offset == 6:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_max_temp", reading)
                    elif offset == 7:
                        set_data_new(mapped_data, data_status, f"battery{battery_num}_min_temp", reading)
            
            elif device_type == 'MPPT':
                # Handle MPPT device type - measurements come through meters_stream as Meter traits
                mppt_num = sum(1 for d in devices[:device_id] if d.get('type') == 'MPPT')
                
                # MPPT measurements are sent as Meter-type readings (data_type=0x01) from meters_stream
                # offset mappings match C++ mppt_reading_id enum:
                # 0=mppt_output_voltage, 1=mppt_total_power, 2=mppt_temperature
                if data_type == 0x01:
                    # Debug: Log what we're receiving
                    logger.debug(f"MPPT data - device_id={device_id}, mppt_num={mppt_num}, phase={phase}, data_type=0x{data_type:02x}, offset={offset}, reading={reading}")
                    
                    # Note: reading is already divided by 1000 from payload parsing
                    # For power (offset=1), backend sends W, so use reading_int without /1000
                    
                    # Process based on phase (similar to METER handling)
                    # Redis sends MPPT power per phase, so we accumulate and sum for total
                    if phase == 0x00:  # Phase A
                        if offset == 0:
                            # Output voltage in V - store from Phase A only
                            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_output_voltage", reading)
                        elif offset == 1:
                            # Total power in W (use raw integer value) - store per phase
                            power_val = float(reading_int) if reading_int is not None else reading
                            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_phaseA_total_power", power_val)
                        elif offset == 2:
                            # Temperature in °C - store from Phase A only
                            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_temperature", reading)
                    
                    elif phase == 0x01:  # Phase B
                        if offset == 1:
                            # Total power in W - store per phase
                            power_val = float(reading_int) if reading_int is not None else reading
                            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_phaseB_total_power", power_val)
                    
                    elif phase == 0x02:  # Phase C
                        if offset == 1:
                            # Total power in W - store per phase
                            power_val = float(reading_int) if reading_int is not None else reading
                            set_data_new(mapped_data, data_status, f"mppt{mppt_num}_phaseC_total_power", power_val)
                    
                    # Calculate total_power from phase data
                    # Priority: (1) Sum all 3 phases if available, (2) Multiply single phase by 3 if only one available
                    phaseA_key = f"mppt{mppt_num}_phaseA_total_power"
                    phaseB_key = f"mppt{mppt_num}_phaseB_total_power"
                    phaseC_key = f"mppt{mppt_num}_phaseC_total_power"
                    
                    # Check freshness: phase is available if it was updated in this message (data_status[key] == True)
                    # not just because the key exists from a previous message
                    phaseA_available = data_status.get(phaseA_key, False)
                    phaseB_available = data_status.get(phaseB_key, False)
                    phaseC_available = data_status.get(phaseC_key, False)
                    
                    # Count how many phases were updated in this message
                    num_phases_available = sum([phaseA_available, phaseB_available, phaseC_available])
                    
                    if num_phases_available == 3:
                        # All three phases available: sum them
                        phaseA_power = mapped_data[phaseA_key]
                        phaseB_power = mapped_data[phaseB_key]
                        phaseC_power = mapped_data[phaseC_key]
                        total_power = phaseA_power + phaseB_power + phaseC_power
                        set_data_new(mapped_data, data_status, f"mppt{mppt_num}_total_power", total_power)
                        logger.debug(f"MPPT{mppt_num} total power (3-phase): A={phaseA_power}W, B={phaseB_power}W, C={phaseC_power}W, Total={total_power}W")
                    elif num_phases_available == 1:
                        # Only one phase available: multiply by 3
                        if phaseA_available:
                            phase_power = mapped_data[phaseA_key]
                        elif phaseB_available:
                            phase_power = mapped_data[phaseB_key]
                        else:  # phaseC_available
                            phase_power = mapped_data[phaseC_key]
                        total_power = phase_power * 3
                        set_data_new(mapped_data, data_status, f"mppt{mppt_num}_total_power", total_power)
                        logger.debug(f"MPPT{mppt_num} total power (single-phase estimate): Phase={phase_power}W, Estimated Total={total_power}W")
        
        # Update data_status back into mapped_data
        mapped_data["data_status"] = data_status
        update_real_telemetry_mapping(mapped_data, config)
        mapped_data["forrixguard_events"] = list(reversed(latest_events[-20:]))
        
        latest_data = mapped_data
        asyncio.create_task(send_to_clients('inverter_data', dict(mapped_data)))
        
        # Update datasheet with raw telemetry
        try:
            latest_datasheet.append({
                "id": str(device_trait_id),
                "measurement_type": tel_id or "unknown",
                "timestamp": datetime.fromtimestamp(timestamp / 1000).strftime('%Y-%m-%d %H:%M:%S'),
                "raw_data": str(reading) if reading is not None else str(status)
            })
            # Keep only last 100 entries
            if len(latest_datasheet) > 100:
                latest_datasheet.pop(0)
        except Exception as e:
            logger.error(f"Error appending to datasheet: {e}")
            latest_datasheet.append({
                "id": "ERROR",
                "measurement_type": "Log",
                "timestamp": datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                "raw_data": f"Error: {str(e)}"
            })
        
        
        # Update topology dynamically based on device type
        config = load_forrixguard_config()
        if config and 'devices' in config:
            devices = config['devices']
            if device_id < len(devices):
                device = devices[device_id]
                device_type = device.get('type', 'UNKNOWN')
                model = device.get('model', 'Unknown')
                
                # Count devices of same type before this one to get the number (0-based)
                if device_type == 'METER':
                    device_num = sum(1 for d in devices[:device_id] if d.get('type') == 'METER')
                    device_name = f"Meter {device_num}"
                    group = "meter"
                elif device_type == 'INVERTER':
                    device_num = sum(1 for d in devices[:device_id] if d.get('type') == 'INVERTER')
                    device_name = f"Inverter {device_num}"
                    group = "inverter"
                elif device_type == 'BATTERY':
                    device_num = sum(1 for d in devices[:device_id] if d.get('type') == 'BATTERY')
                    device_name = f"Battery {device_num}"
                    group = "battery"
                elif device_type == 'MPPT':
                    device_num = sum(1 for d in devices[:device_id] if d.get('type') == 'MPPT')
                    device_name = f"MPPT {device_num}"
                    group = "mppt"
                else:
                    device_name = f"Device {device_id}"
                    group = "unknown"
                
                if device_name not in latest_topology["nodes"]:
                    latest_topology["nodes"][device_name] = {
                        "id": device_name,
                        "label": f"{device_name} ({model})",
                        "type": "device",
                        "group": group
                    }
                
                asyncio.create_task(send_to_clients('topology_update', latest_topology))
        
    except Exception as e:
        logger.error(f"Error processing message: {e}")

def process_forrixguard_demand_message(fields):
    """Process ForrixGuard maximum-demand forecast stream entries."""
    global latest_data

    def as_float(name, default=0.0):
        try:
            return float(fields.get(name, default))
        except Exception:
            return default

    def as_int(name, default=0):
        try:
            return int(float(fields.get(name, default)))
        except Exception:
            return default

    if not latest_data:
        latest_data = build_default_data(load_forrixguard_config())

    latest_data["forrixguard_current_demand_kw"] = as_float("current_kw")
    latest_data["forrixguard_projected_demand_kw"] = as_float("projected_kw")
    latest_data["forrixguard_sanctioned_kw"] = as_float("sanctioned_kw")
    allowed = as_float("allowed_kw")
    latest_data["forrixguard_allowed_kw"] = allowed
    latest_data["forrixguard_required_correction_kw"] = as_float("correction_kw")
    latest_data["forrixguard_estimated_penalty_kw"] = as_float("estimated_penalty_kw")
    latest_data["forrixguard_savings_opportunity_kw"] = as_float("savings_opportunity_kw")
    latest_data["forrixguard_time_left_seconds"] = as_int("time_left_sec")
    latest_data["forrixguard_breach_risk"] = bool(as_int("breach_risk"))
    latest_data["forrixguard_event_cause"] = fields.get("event_cause", "unknown")
    latest_data["forrixguard_telemetry_fresh"] = bool(as_int("telemetry_fresh", 1))
    latest_data["forrixguard_telemetry_age_seconds"] = as_int("telemetry_age_sec")
    latest_data["forrixguard_recommendation_action"] = fields.get("recommendation_action", "monitor")
    latest_data["forrixguard_recommendation_kw"] = as_float("recommendation_kw")
    latest_data["forrixguard_control_command_allowed"] = bool(as_int("control_command_allowed", 0))
    latest_data["forrixguard_demand_margin_kw"] = round(allowed - latest_data["forrixguard_current_demand_kw"], 2)
    latest_data["timestamp"] = as_int("ts", latest_data.get("timestamp", int(time.time() * 1000)))

    # Tariff ROI calculations
    config = load_forrixguard_config() or {}
    tariff = (config.get('site') or {}).get('tariff') or {}
    demand_charge = float(tariff.get('demandChargePerKva', 0) or 0)
    day_rate      = float(tariff.get('dayRatePerKwh', 0) or 0)
    night_rate    = float(tariff.get('nightRatePerKwh', 0) or 0)
    currency      = tariff.get('currency', 'INR')

    projected_kva = as_float("projected_kva", latest_data["forrixguard_projected_demand_kw"])
    projected_demand_charge = round(projected_kva * demand_charge, 0)

    correction_kw = latest_data["forrixguard_required_correction_kw"]
    breach_cost = round(max(0.0, correction_kw) * demand_charge, 0) if latest_data["forrixguard_breach_risk"] else 0.0

    bess_discharge_kwh = as_float("bess_discharge_kwh_today", 0.0)
    savings_today = round(bess_discharge_kwh * max(0.0, day_rate - night_rate), 2) if bess_discharge_kwh > 0 else 0.0

    latest_data["fg_tariff_currency"]            = currency
    latest_data["fg_demand_charge_per_kva"]      = demand_charge
    latest_data["fg_projected_demand_charge"]    = projected_demand_charge
    latest_data["fg_breach_cost_if_hit"]         = breach_cost
    latest_data["fg_savings_today"]              = savings_today
    latest_data["fg_tariff_loaded"]              = bool(tariff)

    # Load profile and next-window prediction
    forecast_kw, forecast_breach, forecast_conf = _check_window_transition(
        latest_data["forrixguard_current_demand_kw"],
        latest_data["forrixguard_allowed_kw"],
    )
    latest_data["fg_next_window_forecast_kw"] = forecast_kw
    latest_data["fg_forecast_breach_risk"]    = forecast_breach
    latest_data["fg_forecast_confidence"]     = forecast_conf

    # BESS dispatch recommendation (P3/P4)
    rec = _compute_bess_recommendation(latest_data)
    latest_data["fg_recommendation"] = rec
    if rec.get('auto_apply') and rec.get('action') == 'discharge_bess':
        asyncio.get_event_loop().create_task(_auto_apply_recommendation(rec))

    if "data_status" not in latest_data:
        latest_data["data_status"] = {}

    for key in [
        "forrixguard_current_demand_kw",
        "forrixguard_projected_demand_kw",
        "forrixguard_sanctioned_kw",
        "forrixguard_allowed_kw",
        "forrixguard_required_correction_kw",
        "forrixguard_estimated_penalty_kw",
        "forrixguard_savings_opportunity_kw",
        "forrixguard_time_left_seconds",
        "forrixguard_breach_risk",
        "forrixguard_event_cause",
        "forrixguard_telemetry_fresh",
        "forrixguard_telemetry_age_seconds",
        "forrixguard_recommendation_action",
        "forrixguard_recommendation_kw",
        "forrixguard_control_command_allowed",
        "forrixguard_demand_margin_kw",
        "fg_projected_demand_charge",
        "fg_breach_cost_if_hit",
        "fg_savings_today",
        "fg_next_window_forecast_kw",
        "fg_forecast_breach_risk",
        "fg_forecast_confidence",
    ]:
        latest_data["data_status"][key] = True

def process_forrixguard_event_message(message_id, fields):
    """Process ForrixGuard event-proof stream entries."""
    global latest_data, latest_events

    def as_int(name, default=0):
        try:
            return int(float(fields.get(name, default)))
        except Exception:
            return default

    if not latest_data:
        latest_data = build_default_data(load_forrixguard_config())

    details_raw = fields.get("details_json", "{}")
    try:
        details = json.loads(details_raw) if details_raw else {}
    except Exception:
        details = {"raw": details_raw}

    timestamp = as_int("ts", int(time.time() * 1000))
    event = {
        "stream_id": message_id,
        "timestamp": timestamp,
        "time": datetime.fromtimestamp(timestamp / 1000).isoformat(),
        "severity": fields.get("severity", "info"),
        "category": fields.get("category", "system"),
        "event_code": fields.get("event_code", "FORRIXGUARD_EVENT"),
        "source": fields.get("source", "forrixguard"),
        "message": fields.get("message", ""),
        "details": details,
    }

    latest_events.append(event)
    latest_events = latest_events[-100:]
    latest_data["forrixguard_events"] = list(reversed(latest_events[-20:]))

async def consume_telemetry():
    """Consume from telemetry stream"""
    global is_connected, redis_client, latest_data, latest_timeslots, latest_events, _timeslots_version
    streams = {
        "meters_stream": '0',
        "inverters_stream": '0',
        "battery_stream": '0',
        "mppts_stream": '0',
        "power_setpoint_stream": '0',
        "forrixguard_demand_stream": '0',
        "forrixguard_events_stream": '0',
    }
    
    # On startup, try to read recent historical data
    config = load_forrixguard_config()
    try:
        logger.info("Reading recent historical data from streams...")
        # Check if streams exist and get their length
        try:
            info = await redis_client.xinfo_stream("meters_stream")
            logger.info(f"Meters stream: {info.get('length', 0)} messages")
        except Exception:
            # Stream may not exist yet - this is expected on first run
            pass
        
        try:
            info = await redis_client.xinfo_stream("inverters_stream")
            logger.info(f"Inverters stream: {info.get('length', 0)} messages")
        except Exception:
            # Stream may not exist yet - this is expected on first run
            pass
        
        try:
            info = await redis_client.xinfo_stream("battery_stream")
            logger.info(f"Battery stream: {info.get('length', 0)} messages")
        except Exception:
            # Stream may not exist yet - this is expected on first run
            pass
        
        try:
            info = await redis_client.xinfo_stream("mppts_stream")
            logger.info(f"MPPTs stream: {info.get('length', 0)} messages")
        except Exception:
            # Stream may not exist yet - this is expected on first run
            pass
        
        # Load timeslots from Redis
        try:
            try:
                timeslots_info = await redis_client.xinfo_stream("timeslots_stream")
            except Exception:
                timeslots_info = None
            
            # Timeslot loading is handled later in the startup sequence (keeps latest per slot id)
        except Exception as e:
            if "no such key" not in str(e).lower() and "does not exist" not in str(e).lower():
                logger.error(f"Error reading timeslots stream: {e}")
        
        # Initialize defaults based on config so UI sees expected keys even before readings
        latest_data = build_default_data(config)

        # Read last 50 messages from each stream to populate initial data
        for stream_name in [
            "meters_stream",
            "inverters_stream",
            "battery_stream",
            "mppts_stream",
            "power_setpoint_stream",
            "forrixguard_demand_stream",
            "forrixguard_events_stream",
        ]:
            try:
                # Get the last 50 messages (most recent)
                recent_messages = await redis_client.xrevrange(stream_name, '+', '-', count=50)
                if recent_messages:
                    logger.info(f"Loaded {len(recent_messages)} recent messages from {stream_name}")
                    # Reverse to process from oldest to newest
                    recent_messages.reverse()
                    for message_id, fields in recent_messages:
                        streams[stream_name] = message_id  # Update last_id to the latest
                        if stream_name == 'battery_stream':
                            # Process battery stream messages
                            payload = json.dumps(fields)  # Convert fields to JSON
                            if payload:
                                process_message(payload)
                        elif stream_name == 'mppts_stream':
                            # Process MPPT stream messages (status/fault)
                            payload = json.dumps(fields)  # Convert fields to JSON
                            if payload:
                                process_message(payload)
                        elif stream_name == 'power_setpoint_stream':
                            try:
                                # Parse per-inverter setpoints
                                inv_id = int(fields.get('inv_id', 0))
                                phase_val = int(fields.get('phase', 0))
                                pwr_sp = int(fields.get('pwr_sp', 0))
                                ts = int(fields.get('ts', 0))
                                
                                # Map phase int to char
                                phase_char = ['A', 'B', 'C'][phase_val % 3]
                                
                                key = f"inv{inv_id}_phase{phase_char}_setpoint"
                                latest_data[key] = pwr_sp
                                
                                # Initialize data_status if not present
                                if "data_status" not in latest_data:
                                    latest_data["data_status"] = {}
                                latest_data["data_status"][key] = True  # Mark as new data
                                
                                latest_data['timestamp'] = ts
                            except Exception as e:
                                logger.error(f"Error processing historical power_setpoint_stream message: {e}")
                        elif stream_name == 'forrixguard_demand_stream':
                            try:
                                process_forrixguard_demand_message(fields)
                            except Exception as e:
                                logger.error(f"Error processing historical forrixguard_demand_stream message: {e}")
                        elif stream_name == 'forrixguard_events_stream':
                            try:
                                process_forrixguard_event_message(message_id, fields)
                            except Exception as e:
                                logger.error(f"Error processing historical forrixguard_events_stream message: {e}")
                        else:
                            payload = json.dumps(fields)  # Convert fields to JSON
                            if payload:
                                process_message(payload)
                    logger.info(f"Processed historical data from {stream_name}")
            except Exception as e:
                if "no such key" not in str(e).lower() and "does not exist" not in str(e).lower():
                    logger.error(f"Error reading from {stream_name}: {e}")
        
        logger.info("Initial data loading complete")
        
        # Initialize topology dynamically from ForrixGuard config
        config = load_forrixguard_config()
        if config and 'devices' in config:
            devices = config['devices']
            nodes = {}
            edges = []
            
            # Create nodes for each device
            meter_count = 0
            inverter_count = 0
            battery_count = 0
            mppt_count = 0
            
            for device in devices:
                device_type = device.get('type', 'UNKNOWN')
                model = device.get('model', 'Unknown')
                
                if device_type == 'METER':
                    node_id = f"Meter {meter_count}"
                    nodes[node_id] = {
                        "id": node_id,
                        "label": f"{node_id} ({model})",
                        "type": "device",
                        "group": "meter"
                    }
                    meter_count += 1
                elif device_type == 'INVERTER':
                    node_id = f"Inverter {inverter_count}"
                    nodes[node_id] = {
                        "id": node_id,
                        "label": f"{node_id} ({model})",
                        "type": "device",
                        "group": "inverter"
                    }
                    inverter_count += 1
                elif device_type == 'BATTERY':
                    node_id = f"Battery {battery_count}"
                    nodes[node_id] = {
                        "id": node_id,
                        "label": f"{node_id} ({model})",
                        "type": "device",
                        "group": "battery"
                    }
                    battery_count += 1
                elif device_type == 'MPPT':
                    node_id = f"MPPT {mppt_count}"
                    nodes[node_id] = {
                        "id": node_id,
                        "label": f"{node_id} ({model})",
                        "type": "device",
                        "group": "mppt"
                    }
                    mppt_count += 1
            
            # Create edges between meters and inverters
            for m in range(meter_count):
                for i in range(inverter_count):
                    edges.append({"source": f"Meter {m}", "target": f"Inverter {i}"})
            
            latest_topology.update({"nodes": nodes, "edges": edges})
            asyncio.create_task(send_to_clients('topology_update', latest_topology))
        
        # Read timeslots data
        try:
            timeslots_info = await redis_client.xinfo_stream("timeslots_stream")
            logger.info(f"Timeslots stream: {timeslots_info.get('length', 0)} messages")
            
            # Read all timeslots messages
            timeslots_messages = await redis_client.xrange("timeslots_stream", '-', '+')
            if timeslots_messages:
                # Build a map keyed by stream_id (authoritative unique identifier)
                # Each stream entry is independent - no overwriting by slot_id
                latest_map = {}
                for message_id, fields in timeslots_messages:
                    try:
                        if 'timeslot_json' in fields:
                            timeslot_data = json.loads(fields['timeslot_json'])
                            slot = timeslot_data.get('timeslot', {})
                            slot_id = slot.get('id')
                            if slot_id is not None:
                                # Key by stream_id to keep every version (not by slot_id)
                                stream_ts_ms = int(message_id.split('-')[0])
                                created_at = datetime.fromtimestamp(stream_ts_ms / 1000).isoformat()
                                latest_map[message_id] = {  # Key by stream_id
                                    'timeslot': slot,
                                    'stream_id': message_id,
                                    'created_at': created_at
                                }
                        elif 'delete_timeslot_json' in fields:
                            # Ignore delete entries - they are metadata, not current timeslots
                            delete_data = json.loads(fields['delete_timeslot_json'])
                            slot_id_to_delete = delete_data.get('slot_id')
                            logger.debug(f"Found delete entry for slot_id {slot_id_to_delete} (ignoring - already deleted from stream)")
                    except json.JSONDecodeError as e:
                        logger.error(f"Error parsing timeslot JSON: {e}")

                latest_timeslots = list(latest_map.values())
                _timeslots_version += 1
                asyncio.create_task(send_to_clients('timeslots_update', latest_timeslots))
        except Exception as e:
            logger.error(f"Error reading timeslots: {e}")
        
        # If still no data after processing historical, populate defaults
        if not latest_data:
            logger.info("No historical data found, populating with defaults...")
            latest_data = build_default_data(config)
            logger.info("Populated dashboard with default values")
    except Exception as e:
        logger.error(f"Error reading historical data: {e}")
        import traceback
        traceback.print_exc()
    
    while is_connected:
        try:
            entries = await redis_client.xread(streams, block=0)
            for stream_name, messages in entries:
                for message_id, fields in messages:
                    streams[stream_name] = message_id  # Update last_id
                    if stream_name == 'timeslots_stream':
                        # Handle Timeslot Updates
                        try:
                            if 'timeslot_json' in fields:
                                ts_data = json.loads(fields['timeslot_json'])
                                slot = ts_data.get('timeslot')
                                if slot:
                                    stream_ts_ms = int(message_id.split('-')[0])
                                    created_at = datetime.fromtimestamp(stream_ts_ms / 1000).isoformat()
                                    new_entry = {
                                        'timeslot': slot,
                                        'stream_id': message_id,
                                        'created_at': created_at
                                    }
                                    latest_timeslots.append(new_entry)
                                    _timeslots_version += 1
                                    logger.info(f"Received new timeslot via stream: {message_id}")
                                    # Broadcast update
                                    asyncio.create_task(send_to_clients('timeslots_update', latest_timeslots))
                            elif 'delete_timeslot_json' in fields:
                                # Handle explicit delete marker if used
                                pass
                        except Exception as e:
                            logger.error(f"Error processing timeslot stream message: {e}")
                    elif stream_name == 'power_setpoint_stream':
                        try:
                            # Parse per-inverter setpoints
                            inv_id = int(fields.get('inv_id', 0))
                            phase_val = int(fields.get('phase', 0))
                            pwr_sp = int(fields.get('pwr_sp', 0))
                            ts = int(fields.get('ts', 0))
                            
                            phase_char = ['A', 'B', 'C'][phase_val % 3]
                            key = f"inv{inv_id}_phase{phase_char}_setpoint"
                            
                            # Initialize data_status if not present
                            if "data_status" not in latest_data:
                                latest_data["data_status"] = {}
                            
                            # Mark this field as new data
                            latest_data[key] = pwr_sp
                            latest_data["data_status"][key] = True  # Mark as new data
                            latest_data['timestamp'] = ts
                            asyncio.create_task(send_to_clients('inverter_data', dict(latest_data)))
                        except Exception as e:
                            logger.error(f"Error processing power_setpoint_stream message: {e}", exc_info=True)
                    elif stream_name == 'forrixguard_demand_stream':
                        try:
                            process_forrixguard_demand_message(fields)
                            asyncio.create_task(send_to_clients('inverter_data', dict(latest_data)))
                        except Exception as e:
                            logger.error(f"Error processing forrixguard_demand_stream message: {e}", exc_info=True)
                    elif stream_name == 'forrixguard_events_stream':
                        try:
                            process_forrixguard_event_message(message_id, fields)
                            asyncio.create_task(send_to_clients('inverter_data', dict(latest_data)))
                        except Exception as e:
                            logger.error(f"Error processing forrixguard_events_stream message: {e}", exc_info=True)
                    elif stream_name == 'battery_stream':
                        # Process battery stream messages (now separate from meter/inverter streams)
                        payload = json.dumps(fields)  # Convert fields to JSON
                        if payload:
                            process_message(payload)
                    elif stream_name == 'mppts_stream':
                        # Process MPPT stream messages (status/fault data)
                        payload = json.dumps(fields)  # Convert fields to JSON
                        if payload:
                            process_message(payload)
                    elif stream_name in ['meters_stream', 'inverters_stream']:
                        payload = json.dumps(fields)  # Convert fields to JSON
                        if payload:
                            process_message(payload)
            await asyncio.sleep(1)
        except Exception as e:
            if "loading" in str(e).lower():
                logger.info("Redis is loading, waiting...")
                await asyncio.sleep(5)
            else:
                logger.error(f"consume_telemetry error: {type(e).__name__}: {e}", exc_info=True)
                await asyncio.sleep(1)

async def connect_redis():
    """Connect Redis client"""
    global redis_client
    local_client = None
    try:
        # Create a local async client and verify connectivity before assigning
        # Configure timeouts to prevent hanging on slow/unresponsive Redis instances
        local_client = await redis.from_url(
            f"redis://{REDIS_HOST}:{REDIS_PORT}",
            decode_responses=True,
            socket_timeout=5,
            socket_connect_timeout=5
        )
        # Test connection
        await local_client.ping()
        # Assign only after a successful ping to avoid leaving a broken client
        redis_client = local_client
        logger.info("Connected to Redis")
        return True
    except Exception as e:
        logger.error(f"Redis connection error: {e}")
        try:
            # Close the local client on failure if it was created
            if local_client:
                await local_client.close()
        except Exception:
            # Ignore close errors; we are already in a failure path
            pass
        return False

async def disconnect_redis():
    """Disconnect Redis client"""
    global redis_client
    try:
        if redis_client:
            try:
                await redis_client.close()
            except Exception as e:
                logger.debug(f"Error closing redis client: {e}")
            redis_client = None
            logger.info("Redis disconnected")
    except Exception as e:
        logger.error(f"Error disconnecting Redis: {e}")
NO_CACHE_HEADERS = {
    'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
    'Pragma': 'no-cache',
    'Expires': '0'
}

async def multi_dashboard(request):
    config = load_forrixguard_config()
    if config:
        devices = config.get('devices', [])
        meters = [d for d in devices if d['type'] == 'METER']
        inverters = [d for d in devices if d['type'] == 'INVERTER']
        bcmus = [d for d in devices if d['type'] == 'BATTERY']
        mppts = [d for d in devices if d['type'] == 'MPPT']
        dgs = [d for d in devices if d['type'] == 'DG']
        
        # Create pairs based on link-inverter-id
        pairs = []
        for inv in inverters:
            inv_device_id = devices.index(inv)  # Get the device_id (index in devices list)
            linked_bcm = None
            for bcm in bcmus:
                # Safely extract link from list (handle missing or empty link-inverter-id)
                links = bcm.get('link-inverter-id', [])
                link = links[0] if links else {}
                if link.get('device_id') == inv_device_id:
                    linked_bcm = bcm
                    break
            if linked_bcm:
                # Find linked MPPT (if any) via link-inverter-id in MPPT config
                # MPPT links to the same inverter as the battery
                inv_device_id = devices.index(inv)
                linked_mppt = None
                linked_mppt_index = None
                for mppt in mppts:
                    # Check if MPPT links to the same inverter (safely extract link)
                    mppt_links = mppt.get('link-inverter-id', [])
                    mppt_link = mppt_links[0] if mppt_links else {}
                    if mppt_link.get('device_id') == inv_device_id:
                        linked_mppt = mppt
                        linked_mppt_index = mppts.index(mppt)
                        break
                pair = {'inverter': inv, 'battery': linked_bcm, 'mppt': linked_mppt, 'mppt_index': linked_mppt_index}
                pairs.append(pair)
    else:
        meters = []
        pairs = []
        mppts = []
        dgs = []
    
    # Use pre-compiled template for better performance
    if not multi_dashboard_template:
        return web.Response(text="Template not loaded", status=500)
    
    num_pairs = len(pairs)
    num_meters = len(meters)
    num_mppts = len(mppts)
    total_devices = num_meters + num_pairs
    grid_modifier = ""
    
    if total_devices >= 5:
        if total_devices in [5, 9, 10, 13, 14, 15]:
            grid_modifier = "w-dense"
    
    # Build pair-to-MPPT mapping for JavaScript
    pair_mppt_mapping = [pair.get('mppt_index') for pair in pairs]
    
    # Note: MPPT data is displayed within battery expandable panels
    # Each pair has an explicit mppt_index (from link-inverter-id in config) or None if not linked
    # MPPT indices are independent of pair indices and derived from MPPT device order in config
    rendered_html = multi_dashboard_template.render(
        meters=meters,
        pairs=pairs,
        mppts=mppts,
        dgs=dgs,
        num_pairs=num_pairs,
        num_meters=num_meters,
        num_mppts=num_mppts,
        grid_modifier=grid_modifier,
        pair_mppt_mapping=pair_mppt_mapping
    )
    
    return web.Response(text=rendered_html, content_type='text/html', headers=NO_CACHE_HEADERS)

async def phase_power_comparison(request):
    return web.Response(text=phase_power_comparison_template.render(), content_type='text/html', headers=NO_CACHE_HEADERS)

async def time_slots_dashboard(request):
    return web.Response(text=time_slots_template.render(), content_type='text/html', headers=NO_CACHE_HEADERS)

async def get_latest(request):
    global last_api_call_time, redis_client, is_connected, latest_events
    current_time = time.time()
    update_real_telemetry_mapping(latest_data, load_forrixguard_config())
    
    # Update data_status based on timestamps: TRUE if updated since last API call
    if 'data_status' in latest_data and 'timestamps' in latest_data:
        for key in latest_data['data_status']:
            if key in latest_data['timestamps']:
                latest_data['data_status'][key] = (latest_data['timestamps'][key] > last_api_call_time)
            else:
                latest_data['data_status'][key] = False
    
    # Include Redis connection status in response so frontend knows if backend is healthy
    latest_data['redis_connected'] = is_connected
    latest_data['forrixguard_events'] = list(reversed(latest_events[-20:]))
    
    last_api_call_time = current_time
    return web.json_response(latest_data)

async def publish_settings(request):
    data = await request.json()
    
    # Check if this is timeslot data
    if 'timeslot' in data:
        return await publish_timeslot(data)
    elif 'delete_timeslot' in data:
        return await delete_timeslot(data['delete_timeslot'])
    elif 'smartPowerControl' in data:
        return await publish_smart_power_control(request, data['smartPowerControl'])
    
    # For now, just acknowledge other settings
    return web.json_response({"success": True, "message": "Settings published"})

def validate_smart_power_control(data):
    """Validate and sanitize Smart Power Control data"""
    validated = {}

    def _parse_bool(value, default=False):
        """Parse various representations of booleans safely."""
        if isinstance(value, bool):
            return value
        if value is None:
            return default
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("true", "1", "yes", "on"):
                return True
            if v in ("false", "0", "no", "off"):
                return False
            return default
        if isinstance(value, (int, float)):
            return value != 0
        return default
    
    # Boolean fields
    for field in ['powerFactorImproverEnabled', 'rampingChargePowerEnabled', 
                  'batteryHighLowVoltageProtectionEnabled', 'batteryHighLowTempProtectionEnabled']:
        validated[field] = _parse_bool(data.get(field), default=False)
    
    # Ramping SOC (0-100)
    soc = data.get('rampingChargePowerSoC', 90)
    validated['rampingChargePowerSoC'] = max(0, min(100, int(soc) if isinstance(soc, (int, float)) else 90))
    
    # Voltage fields (0.5-5.0V reasonable range for cell voltage)
    max_volt = data.get('maxBatteryCellVoltage', 3.6)
    validated['maxBatteryCellVoltage'] = max(0.5, min(5.0, float(max_volt) if isinstance(max_volt, (int, float)) else 3.6))
    
    min_volt = data.get('minBatteryCellVoltage', 2.8)
    validated['minBatteryCellVoltage'] = max(0.5, min(5.0, float(min_volt) if isinstance(min_volt, (int, float)) else 2.8))
    
    # Voltage difference (0-0.99V)
    volt_diff = data.get('maxCellVoltageDiff', 0.15)
    validated['maxCellVoltageDiff'] = max(0.0, min(0.99, float(volt_diff) if isinstance(volt_diff, (int, float)) else 0.15))
    
    # Temperature fields (-40 to 100°C reasonable range)
    max_temp = data.get('maxBatteryCellTemp', 55)
    validated['maxBatteryCellTemp'] = max(-40, min(100, int(max_temp) if isinstance(max_temp, (int, float)) else 55))
    
    min_temp = data.get('minBatteryCellTemp', 0)
    validated['minBatteryCellTemp'] = max(-40, min(100, int(min_temp) if isinstance(min_temp, (int, float)) else 0))
    
    # Temperature difference (0-50°C)
    temp_diff = data.get('maxCellTempDiff', 5)
    validated['maxCellTempDiff'] = max(0, min(50, int(temp_diff) if isinstance(temp_diff, (int, float)) else 5))
    
    # ID and timestamp (optional metadata, store as-is if present)
    if 'id' in data:
        validated['id'] = data['id']
    if 'timestamp' in data:
        validated['timestamp'] = str(data['timestamp'])
    
    return validated

async def publish_smart_power_control(request, smart_control_data):
    """Publish Smart Power Control settings to Redis key (admin only)"""
    global redis_client
    try:
        # Authorization check: verify user is admin
        token = request.cookies.get(COOKIE_NAME)
        session = SESSIONS.get(token)
        
        if not session:
            logger.warning("Smart Power Control save attempted without session")
            return web.json_response({"success": False, "message": "Not authenticated"}, status=401)
        
        # Extract role from session (handle both dict and legacy string formats)
        if isinstance(session, dict):
            user_role = session.get('role')
            username = session.get('username')
        else:
            # Legacy: session is just a username string
            username = session
            user_role = 'admin' if session == 'admin' else 'user'
        
        if user_role != 'admin':
            logger.warning(f"Smart Power Control save attempted by non-admin user: {username}")
            return web.json_response({"success": False, "message": "Admin access required"}, status=403)

        if not redis_client:
            return web.json_response({"success": False, "message": "Redis client not connected"})

        # Validate and sanitize input data
        try:
            validated_data = validate_smart_power_control(smart_control_data)
        except Exception as e:
            logger.error(f"Smart Power Control validation failed: {e}")
            return web.json_response({"success": False, "message": f"Validation error: {str(e)}"})

        # Persist as a simple Redis key (latest value wins)
        payload = {"smartPowerControl": validated_data}
        await redis_client.set('smart_power_control', json.dumps(payload))
        
        logger.info(f"Smart Power Control settings saved by admin user: {username}")
        return web.json_response({"success": True, "message": "Smart Power Control saved"})
    except Exception as e:
        logger.error(f"Error publishing Smart Power Control: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"success": False, "message": str(e)})

async def get_smart_power_control(request):
    """Fetch latest Smart Power Control settings from Redis key"""
    global redis_client
    try:
        if not redis_client:
            return web.json_response({"smartPowerControl": None})

        val = await redis_client.get('smart_power_control')
        if not val:
            return web.json_response({"smartPowerControl": None})

        try:
            payload = json.loads(val)
        except Exception:
            logger.warning("smart_power_control key contains invalid JSON; returning empty")
            return web.json_response({"smartPowerControl": None})

        # Expect payload shape { "smartPowerControl": {...} }
        return web.json_response(payload)
    except Exception as e:
        logger.error(f"Error reading Smart Power Control: {e}")
        return web.json_response({"smartPowerControl": None, "error": str(e)}, status=500)


async def publish_timeslot(timeslot_data):
    """Publish timeslot data to Redis stream - EDIT pattern: collect old data, delete by stream_id, create new with current timestamp"""
    global redis_client, latest_timeslots, _timeslots_version
    
    try:
        if not redis_client:
            return web.json_response({"success": False, "message": "Redis client not connected"})
        
        # Extract timeslot data
        timeslot = timeslot_data['timeslot']
        slot_id = timeslot['id']
        stream_id_to_edit = timeslot_data.get('stream_id')  # If editing, this is the old stream_id
        
        # Use the backend timeslot payload shape.
        timeslot_converted = {
            'id': timeslot['id'],
            'startDate': timeslot['startDate'],
            'endDate': timeslot['endDate'],
            'startTime': timeslot['startTime'],
            'endTime': timeslot['endTime'],
            'days': timeslot['days'],
            'desiredState': timeslot['desiredState']
        }
        timeslot_data['timeslot'] = timeslot_converted
        
        # STEP 1: If editing (stream_id_to_edit is provided), delete that specific stream entry
        if stream_id_to_edit:
            try:
                # Delete by matching the exact stream_id (not by slot_id)
                await redis_client.xdel('timeslots_stream', stream_id_to_edit)
            except Exception as e:
                logger.error(f"Error deleting old stream entry: {e}")
        
        # STEP 2: Create new entry with current timestamp
        # Wrap strictly as { "timeslot": ... } only, excluding stream_id from the JSON payload being saved
        final_payload = { "timeslot": timeslot_converted }
        stream_data = {
            'timeslot_json': json.dumps(final_payload)
        }

        result = await redis_client.xadd('timeslots_stream', stream_data, maxlen=500, approximate=True)

        # Update local latest_timeslots: remove existing and append new
        latest_timeslots = [t for t in latest_timeslots if t.get('stream_id') != stream_id_to_edit]
        _timeslots_version += 1
        
        # Add new timeslot with current stream_id and created_at timestamp
        stream_ts_ms = int(result.split('-')[0])
        created_at = datetime.fromtimestamp(stream_ts_ms / 1000).isoformat()
        new_timeslot_entry = {
            'timeslot': timeslot_converted,
            'stream_id': result,
            'created_at': created_at
        }
        latest_timeslots.append(new_timeslot_entry)
        _timeslots_version += 1

        # Send update to all connected clients
        asyncio.create_task(send_to_clients('timeslots_update', latest_timeslots))

        return web.json_response({"success": True, "message": f"Timeslot published successfully", "stream_id": result})
        
    except Exception as e:
        logger.error(f"Error publishing timeslot: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"success": False, "message": str(e)})

async def delete_timeslot(stream_id):
    """Delete a timeslot - delete by matching the exact stream_id (unique identifier)"""
    global redis_client, latest_timeslots, _timeslots_version
    
    try:
        if not redis_client:
            return web.json_response({"success": False, "message": "Redis client not connected"})

        # Remove from local latest_timeslots by stream_id
        initial_count = len(latest_timeslots)
        latest_timeslots = [t for t in latest_timeslots if t.get('stream_id') != stream_id]
        _timeslots_version += 1

        deleted_from_stream = 0

        try:
            # Delete the specific stream entry by its stream_id (authoritative unique ID)
            deleted_from_stream = await redis_client.xdel('timeslots_stream', stream_id)
            logger.info(f"Deleted stream entry: {stream_id}")
        except Exception as e:
            logger.error(f"Error deleting stream entry: {e}")

        # Notify clients if something changed
        if len(latest_timeslots) < initial_count or deleted_from_stream > 0:
            asyncio.create_task(send_to_clients('timeslots_update', latest_timeslots))
            msg = f"Deleted timeslot (stream_id: {stream_id})"
            logger.info(msg)
            return web.json_response({"success": True, "message": msg})
        else:
            return web.json_response({"success": False, "message": f"Timeslot not found"})
        
    except Exception as e:
        logger.error(f"Error deleting timeslot: {e}")
        import traceback
        traceback.print_exc()
        return web.json_response({"success": False, "message": str(e)})

async def get_datasheet(request):
    logger.info(f"API request: get_datasheet, size={len(latest_datasheet)}")
    return web.json_response(latest_datasheet)

async def get_topology(request):
    return web.json_response(latest_topology)

async def get_timeslots(request):
    etag = str(_timeslots_version)
    if request.headers.get('If-None-Match') == etag:
        return web.Response(status=304)
    resp = web.json_response(latest_timeslots)
    resp.headers['ETag'] = etag
    resp.headers['Cache-Control'] = 'no-cache'
    return resp

async def websocket_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    connected_websockets.add(ws)
    
    # Send initial data if available
    if latest_data:
        await ws.send_json({'type': 'inverter_data', 'data': latest_data})
    if latest_datasheet:
        await ws.send_json({'type': 'datasheet_update', 'data': latest_datasheet})
    if latest_topology.get("nodes"):
        await ws.send_json({'type': 'topology_update', 'data': latest_topology})
    if latest_timeslots:
        await ws.send_json({'type': 'timeslots_update', 'data': latest_timeslots})
    
    # Send firmware update history if available
    if HAS_FW_UPDATE:
        try:
            status = update_status
            if status.get('progress_messages'):
                progress_messages = list(status.get('progress_messages', []))
                for msg_entry in progress_messages:
                    await ws.send_json({
                        'type': 'fw_update_progress',
                        'data': {
                            'type': 'history',
                            'message': msg_entry.get('message', ''),
                            'timestamp': msg_entry.get('timestamp', datetime.now().isoformat())
                        }
                    })
            
            # Check if update is already complete and send final status
            # The frontend relies on this to show the "Done" button
            last_update = status.get('last_update')
            if not status.get('in_progress') and last_update:
                final_status = last_update.get('status')
                if final_status in ['completed_running', 'completed_rebooting', 'success']:
                     await ws.send_json({
                        'type': 'fw_update_progress',
                        'data': {
                            'type': 'status',
                            'message': 'STATUS:SUCCESS',
                            'timestamp': datetime.now().isoformat()
                        }
                    })
                elif final_status == 'failed':
                    await ws.send_json({
                        'type': 'fw_update_progress',
                        'data': {
                            'type': 'status',
                            'message': 'STATUS:FAILED',
                            'timestamp': datetime.now().isoformat()
                        }
                    })

        except Exception as e:
            logger.error(f"Error sending fw history: {e}")

    try:
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                    msg_type = data.get('type')
                    if msg_type == 'connect_device':
                        await handle_connect_device(data, ws)
                    elif msg_type == 'disconnect_device':
                        await handle_disconnect_device(ws)
                    elif msg_type == 'publish_settings':
                        await handle_publish_settings(data, ws)
                except json.JSONDecodeError:
                    # Ignore malformed JSON messages from client; protocol allows occasional bad input
                    continue
            elif msg.type == WSMsgType.ERROR:
                logger.error(f'WebSocket error {ws.exception()}')
    finally:
        connected_websockets.discard(ws)
    return ws

async def handle_connect_device(data, ws):
    """Handle device connection request"""
    global redis_client, current_device, is_connected, latest_datasheet, latest_topology, consumer_task
    try:
        device_id = data.get('deviceId', 'FORRIXGUARD000111')
        current_device = device_id
        is_connected = True
        
        # Clear datasheet and topology data for new connection
        latest_datasheet = []
        latest_topology = {"nodes": {}, "edges": [], "pins": {}}
        
        # Emit clear events to all connected clients
        await send_to_clients('datasheet_clear', {})
        await send_to_clients('topology_clear', {})
        logger.info("Cleared datasheet and topology data for new connection")
        
        # Start Redis if not already connected
        if not redis_client:
            success = await connect_redis()
            if not success:
                logger.error("Failed to connect to Redis")
                return
        
        if redis_client and (not consumer_task or consumer_task.done()):
            # Start consumer task only if not already running
            consumer_task = asyncio.create_task(consume_telemetry())
            logger.info("=== CONNECTED TO STREAMS ===")
        else:
            logger.info("Redis consumer already running")
    except Exception as e:
        logger.error(f"Error connecting: {e}")
        import traceback
        traceback.print_exc()

async def handle_disconnect_device(ws):
    """Handle device disconnection request"""
    global redis_client, current_device, is_connected, consumer_task
    try:
        is_connected = False
        if consumer_task and not consumer_task.done():
            consumer_task.cancel()
        consumer_task = None
        await disconnect_redis()
        redis_client = None
        logger.info("=== DISCONNECTED FROM STREAMS ===")
    except Exception as e:
        logger.error(f"Error disconnecting: {e}")

async def handle_publish_settings(data, ws):
    """Handle settings publish request from frontend"""
    global redis_client, current_device
    logger.debug("=== PUBLISH SETTINGS CALLED ===")
    logger.debug(f"Received data: {data}")
    logger.debug(f"Redis client exists: {redis_client is not None}")
    try:
        stream = f"control:{current_device}"
        payload = json.dumps(data)
        
        if redis_client:
            result = await redis_client.xadd(stream, {'data': payload})
            logger.debug("=== SETTINGS PUBLISHED ===")
            logger.debug(f"Stream: {stream}")
            logger.debug(f"Payload: {payload}")
            logger.debug(f"XADD result: {result}")
            logger.debug("===========================")
            await ws.send_json({'type': 'publish_response', 'data': {'success': True, 'message': 'Settings published successfully'}})
        else:
            logger.error("ERROR: Redis client not connected")
            await ws.send_json({'type': 'publish_response', 'data': {'success': False, 'message': 'Redis client not connected'}})
    except Exception as e:
        logger.error(f"Error publishing settings: {e}")
        import traceback
        traceback.print_exc()
        await ws.send_json({'type': 'publish_response', 'data': {'success': False, 'message': str(e)}})

async def load_initial_timeslots():
    """Load all existing timeslots from Redis stream on startup"""
    global redis_client, latest_timeslots, _timeslots_version
    try:
        with open('debug_timeslots.log', 'a') as f:
            f.write(f"[{datetime.now()}] Loading initial timeslots...\n")

        # Fetch all entries from beginning
        entries = await redis_client.xrange('timeslots_stream', min='-', max='+')
        loaded_slots = []
        for stream_id, data in entries:
            ts_json = data.get('timeslot_json')
            if ts_json:
                try:
                    payload = json.loads(ts_json)
                    ts_ms = int(stream_id.split('-')[0])
                    created_at = datetime.fromtimestamp(ts_ms / 1000).isoformat()
                    
                    entry = {
                        'timeslot': payload.get('timeslot'),
                        'stream_id': stream_id,
                        'created_at': created_at
                    }
                    loaded_slots.append(entry)
                except Exception as e:
                    with open('debug_timeslots.log', 'a') as f:
                        f.write(f"[{datetime.now()}] Error parsing slot {stream_id}: {e}\n")
        
        latest_timeslots = loaded_slots
        _timeslots_version += 1
        with open('debug_timeslots.log', 'a') as f:
             f.write(f"[{datetime.now()}] Loaded {len(latest_timeslots)} slots: {[t.get('stream_id') for t in latest_timeslots]}\n")
             
    except Exception as e:
        with open('debug_timeslots.log', 'a') as f:
            f.write(f"[{datetime.now()}] Error loading initial timeslots: {e}\n")


async def monthly_report_page(request):
    return web.Response(text=monthly_report_template.render(), content_type='text/html', headers=NO_CACHE_HEADERS)


def report_event_label(code):
    labels = {
        'DEMO_SCENARIO_STARTED': 'Demo Started',
        'DEMAND_RISING': 'Demand Rising',
        'DEMAND_BREACH_RISK': 'Peak Risk Warning',
        'DEMAND_RISK_CLEAR': 'Peak Risk Cleared',
        'DEMAND_WINDOW_CLOSED': 'Demand Window Closed',
        'DEMAND_WINDOW_BREACHED': 'Demand Window Breached',
        'BESS_DISPATCHED': 'Battery Support Started',
        'DG_STARTED': 'DG Runtime Logged',
        'LOAD_SHED_AVOIDED': 'Load Shedding Avoided',
        'METER_COMM_LOST': 'Meter Communication Lost',
        'SOLAR_UNDERPERFORMANCE': 'Solar Underperformance',
    }
    if code in labels:
        return labels[code]
    return str(code or 'Event').replace('_', ' ').title()


async def api_monthly_report(request):
    global redis_client

    now = datetime.now()
    try:
        year = int(request.rel_url.query.get('year', now.year))
        month = int(request.rel_url.query.get('month', now.month))
        if not (1 <= month <= 12) or year < 2020:
            raise ValueError()
    except ValueError:
        return web.json_response({'error': 'Invalid year or month'}, status=400)

    month_start = datetime(year, month, 1)
    if month == 12:
        month_end = datetime(year + 1, 1, 1)
    else:
        month_end = datetime(year, month + 1, 1)

    since_ms = int(month_start.timestamp() * 1000)
    until_ms = int(month_end.timestamp() * 1000) - 1

    events = []
    proof_events = []
    if redis_client:
        try:
            def field(name, default=0):
                return fields.get(name, fields.get(name.encode(), default))

            start_id = f"{since_ms}-0"
            end_id = f"{until_ms}-9999999999"
            raw = await redis_client.xrange('forrixguard_peak_events_stream', start_id, end_id, count=5000)
            for entry_id, fields in raw:
                ts = int(field('ts', 0))
                events.append({
                    'timestamp': ts,
                    'datetime': datetime.utcfromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M') if ts else '--',
                    'final_kw': round(float(field('final_kw', 0)), 1),
                    'sanctioned_kw': round(float(field('sanctioned_kw', 0)), 1),
                    'allowed_kw': round(float(field('allowed_kw', 0)), 1),
                    'correction_kw': round(float(field('correction_kw', 0)), 1),
                    'estimated_penalty_kw': round(float(field('estimated_penalty_kw', 0)), 1),
                    'savings_opportunity_kw': round(float(field('savings_opportunity_kw', 0)), 1),
                    'breach': int(field('breach', 0)),
                    'event_cause': field('event_cause', 'unknown'),
                    'telemetry_fresh': int(field('telemetry_fresh', 1)),
                    'telemetry_age_sec': int(field('telemetry_age_sec', 0)),
                })
        except Exception as e:
            logger.error(f"Error reading forrixguard_peak_events_stream: {e}")

        try:
            start_id = f"{since_ms}-0"
            end_id = f"{until_ms}-9999999999"
            raw = await redis_client.xrange('forrixguard_events_stream', start_id, end_id, count=5000)
            for entry_id, fields in raw:
                def event_field(name, default=''):
                    return fields.get(name, fields.get(name.encode(), default))

                details_raw = event_field('details_json', '{}')
                try:
                    details = json.loads(details_raw) if details_raw else {}
                except Exception:
                    details = {'raw': details_raw}

                ts = int(float(event_field('ts', 0) or 0))
                proof_events.append({
                    'timestamp': ts,
                    'datetime': datetime.utcfromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M') if ts else '--',
                    'severity': event_field('severity', 'info'),
                    'category': event_field('category', 'system'),
                    'event_code': event_field('event_code', 'FORRIXGUARD_EVENT'),
                    'source': event_field('source', 'forrixguard'),
                    'message': event_field('message', ''),
                    'details': details,
                })
        except Exception as e:
            logger.error(f"Error reading forrixguard_events_stream: {e}")

    monthly_peak_kw = max((e['final_kw'] for e in events), default=0)
    breach_count = sum(1 for e in events if e['breach'])
    max_correction_kw = max((e['correction_kw'] for e in events), default=0)
    total_estimated_penalty_kw = sum((e.get('estimated_penalty_kw', 0) for e in events), 0.0)
    total_savings_opportunity_kw = sum((e.get('savings_opportunity_kw', 0) for e in events), 0.0)
    event_counts = {}
    cause_counts = {}
    total_correction_kw = 0.0
    total_bess_kw = 0.0
    total_dg_runtime_min = 0.0
    avoided_breach_count = 0
    bess_action_count = 0
    solar_underperformance_count = 0
    meter_comm_lost_count = 0

    for event in proof_events:
        code = event.get('event_code', 'FORRIXGUARD_EVENT')
        details = event.get('details') or {}
        event_counts[code] = event_counts.get(code, 0) + 1
        cause = details.get('event_cause') or details.get('cause') or 'unknown'
        cause_counts[cause] = cause_counts.get(cause, 0) + 1

        try:
            total_correction_kw += float(details.get('correction_kw', 0) or 0)
        except Exception:
            pass

        if code == 'LOAD_SHED_AVOIDED':
            avoided_breach_count += 1
        elif code == 'BESS_DISPATCHED':
            bess_action_count += 1
            try:
                total_bess_kw += float(details.get('bess_kw', 0) or 0)
            except Exception:
                pass
        elif code == 'DG_STARTED':
            try:
                total_dg_runtime_min += float(details.get('dg_runtime_min', 0) or 0)
            except Exception:
                pass
        elif code == 'SOLAR_UNDERPERFORMANCE':
            solar_underperformance_count += 1
        elif code == 'METER_COMM_LOST':
            meter_comm_lost_count += 1

    missed_items = []
    if solar_underperformance_count:
        missed_items.append(f"{solar_underperformance_count} solar underperformance event(s)")
    if meter_comm_lost_count:
        missed_items.append(f"{meter_comm_lost_count} meter communication loss event(s)")
    if breach_count:
        missed_items.append(f"{breach_count} demand breach window(s)")
    missed_opportunity_summary = "; ".join(missed_items) if missed_items else "No missed opportunities recorded"

    sanctioned_kw = events[0]['sanctioned_kw'] if events else 0
    allowed_kw = events[0]['allowed_kw'] if events else 0

    # Build month navigation links (previous and next months)
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1

    summary = {
        'total_windows': len(events),
        'breach_count': breach_count,
        'event_count': len(proof_events),
        'avoided_breach_count': avoided_breach_count,
        'bess_action_count': bess_action_count,
        'total_bess_kw': round(total_bess_kw, 1),
        'dg_runtime_min': round(total_dg_runtime_min, 1),
        'total_correction_kw': round(total_correction_kw, 1),
        'solar_underperformance_count': solar_underperformance_count,
        'meter_comm_lost_count': meter_comm_lost_count,
        'missed_opportunity_summary': missed_opportunity_summary,
        'event_counts': event_counts,
        'cause_counts': cause_counts,
        'monthly_peak_kw': round(monthly_peak_kw, 1),
        'max_correction_kw': round(max_correction_kw, 1),
        'total_estimated_penalty_kw': round(total_estimated_penalty_kw, 1),
        'total_savings_opportunity_kw': round(total_savings_opportunity_kw, 1),
        'sanctioned_kw': round(sanctioned_kw, 1),
        'allowed_kw': round(allowed_kw, 1),
        'month_label': month_start.strftime('%B %Y'),
        'year': year,
        'month': month,
        'prev_year': prev_year,
        'prev_month': prev_month,
        'next_year': next_year,
        'next_month': next_month,
    }

    site_config = get_commissioning_summary()
    report = {'events': events, 'proof_events': proof_events, 'summary': summary, 'site_config': site_config}
    export_format = request.rel_url.query.get('format', '').lower()
    filename_base = f"forrixguard-monthly-report-{year:04d}-{month:02d}"

    if export_format == 'csv':
        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow(['ForrixGuard Monthly Proof Report', summary['month_label']])
        writer.writerow([])
        writer.writerow(['Summary'])
        for key, label in [
            ('monthly_peak_kw', 'Monthly Peak kW'),
            ('sanctioned_kw', 'Sanctioned kW'),
            ('allowed_kw', 'Allowed kW'),
            ('breach_count', 'Breach Events'),
            ('total_windows', 'Demand Windows'),
            ('max_correction_kw', 'Max Correction kW'),
            ('total_estimated_penalty_kw', 'Estimated Penalty kW'),
            ('total_savings_opportunity_kw', 'Savings Opportunity kW'),
            ('event_count', 'Proof Events'),
            ('avoided_breach_count', 'Load Shedding Avoided'),
            ('bess_action_count', 'BESS Actions'),
            ('dg_runtime_min', 'DG Runtime min'),
            ('total_correction_kw', 'Total Correction kW'),
            ('meter_comm_lost_count', 'Meter Issues'),
            ('solar_underperformance_count', 'Solar Events'),
            ('missed_opportunity_summary', 'Missed Opportunity Summary'),
        ]:
            writer.writerow([label, summary.get(key, '')])

        writer.writerow([])
        writer.writerow(['Demand Window Records'])
        writer.writerow([
            'Window Closed UTC', 'Final Demand kW', 'Sanctioned kW', 'Allowed kW',
            'Correction kW', 'Estimated Penalty kW', 'Savings Opportunity kW',
            'Cause', 'Telemetry Fresh', 'Telemetry Age sec', 'Breach'
        ])
        for event in events:
            writer.writerow([
                event.get('datetime', ''),
                event.get('final_kw', ''),
                event.get('sanctioned_kw', ''),
                event.get('allowed_kw', ''),
                event.get('correction_kw', ''),
                event.get('estimated_penalty_kw', ''),
                event.get('savings_opportunity_kw', ''),
                event.get('event_cause', ''),
                'YES' if event.get('telemetry_fresh') else 'NO',
                event.get('telemetry_age_sec', ''),
                'YES' if event.get('breach') else 'NO',
            ])

        writer.writerow([])
        writer.writerow(['Proof Timeline'])
        writer.writerow(['Proof Event UTC', 'Event', 'Severity', 'Source', 'Cause', 'Action', 'Result', 'Correction kW'])
        for event in proof_events:
            details = event.get('details') or {}
            writer.writerow([
                event.get('datetime', ''),
                report_event_label(event.get('event_code')),
                event.get('severity', ''),
                event.get('source', ''),
                details.get('event_cause') or details.get('cause', ''),
                details.get('action') or event.get('message', ''),
                details.get('result', ''),
                details.get('correction_kw', ''),
            ])

        return web.Response(
            text=output.getvalue(),
            content_type='text/csv',
            headers={
                'Content-Disposition': f'attachment; filename="{filename_base}.csv"',
                **NO_CACHE_HEADERS,
            },
        )

    if export_format == 'json':
        return web.json_response(
            report,
            headers={
                'Content-Disposition': f'attachment; filename="{filename_base}.json"',
                **NO_CACHE_HEADERS,
            },
        )

    return web.json_response(report)


async def forrixguard_setup_page(request):
    return web.Response(text=forrixguard_setup_template.render(), content_type='text/html', headers=NO_CACHE_HEADERS)

async def get_commissioning_api(request):
    return web.json_response(get_commissioning_summary())

async def save_commissioning_api(request):
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': 'Invalid JSON body'}, status=400)

    config = load_forrixguard_config() or {}
    config.setdefault('site', {})
    config.setdefault('maximumDemand', {})
    config.setdefault('control', {})
    config.setdefault('telemetryMapping', {})

    site = payload.get('site') or {}
    demand = payload.get('maximumDemand') or {}
    control = payload.get('control') or {}
    mapping = payload.get('telemetryMapping') or {}

    for key in ['siteId', 'customerName', 'market', 'timezone', 'tariffCategory']:
        if key in site:
            config['site'][key] = site[key]

    for key in ['sanctionedDemandKw', 'safetyMargin', 'windowSeconds', 'controlMarginKw']:
        if key in demand:
            try:
                value = float(demand[key])
                config['maximumDemand'][key] = int(value) if key == 'windowSeconds' else value
            except Exception:
                return web.json_response({'success': False, 'error': f'Invalid value for {key}'}, status=400)

    for key in ['mode', 'allowBessDispatch', 'allowLoadShedding', 'socReservePct', 'dgOptional']:
        if key in control:
            config['control'][key] = control[key]

    for section in ['gridImport', 'solar', 'load', 'bess', 'dg']:
        if section in mapping:
            config['telemetryMapping'].setdefault(section, {})
            config['telemetryMapping'][section].update(mapping[section])

    roles_by_type_index = payload.get('deviceRoles') or {}
    devices = config.get('devices', [])
    type_counts = {}
    for device in devices:
        dtype = device.get('type', 'UNKNOWN')
        type_index = type_counts.get(dtype, 0)
        key = f"{dtype}:{type_index}"
        if key in roles_by_type_index:
            device['role'] = roles_by_type_index[key]
        type_counts[dtype] = type_index + 1

    try:
        with open(FORRIXGUARD_CONFIG_PATH, 'w') as f:
            json.dump(config, f, indent=4)
            f.write('\n')
    except PermissionError:
        return web.json_response({
            'success': False,
            'error': f'No write permission for {FORRIXGUARD_CONFIG_PATH}. Run local dashboard with writable config or use config upload.'
        }, status=403)
    except Exception as e:
        logger.error(f"Failed to save commissioning config: {e}")
        return web.json_response({'success': False, 'error': str(e)}, status=500)

    return web.json_response({'success': True, 'commissioning': get_commissioning_summary()})

async def get_tariff_api(request):
    config = load_forrixguard_config() or {}
    tariff = (config.get('site') or {}).get('tariff') or {}
    return web.json_response({
        'currency':           tariff.get('currency', 'INR'),
        'demandChargePerKva': tariff.get('demandChargePerKva', 0.0),
        'dayRatePerKwh':      tariff.get('dayRatePerKwh', 0.0),
        'nightRatePerKwh':    tariff.get('nightRatePerKwh', 0.0),
        'peakStartHHMM':      tariff.get('peakStartHHMM', '0700'),
        'peakEndHHMM':        tariff.get('peakEndHHMM', '2300'),
    })


async def save_tariff_api(request):
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': 'Invalid JSON'}, status=400)

    config = load_forrixguard_config() or {}
    if 'site' not in config:
        config['site'] = {}
    config['site']['tariff'] = {
        'currency':           str(body.get('currency', 'INR')),
        'demandChargePerKva': float(body.get('demandChargePerKva', 0)),
        'dayRatePerKwh':      float(body.get('dayRatePerKwh', 0)),
        'nightRatePerKwh':    float(body.get('nightRatePerKwh', 0)),
        'peakStartHHMM':      str(body.get('peakStartHHMM', '0700')),
        'peakEndHHMM':        str(body.get('peakEndHHMM', '2300')),
    }
    try:
        import json as _json
        with open(FORRIXGUARD_CONFIG_PATH, 'w') as f:
            _json.dump(config, f, indent=4)
    except Exception as e:
        return web.json_response({'success': False, 'error': str(e)}, status=500)

    await broadcast({'type': 'tariff_update', 'tariff': config['site']['tariff']})
    return web.json_response({'success': True, 'tariff': config['site']['tariff']})


def _compute_bess_recommendation(data: dict) -> dict:
    """Compute a BESS dispatch recommendation from latest demand and battery state.
    Returns a recommendation dict (empty if no action recommended)."""
    config = load_forrixguard_config() or {}
    control = config.get('control') or {}
    mode = control.get('mode', 'MONITOR_ONLY')
    allow_bess = control.get('allowBessDispatch', False)
    soc_reserve = float(control.get('socReservePct', 35))

    if not allow_bess and mode == 'MONITOR_ONLY':
        return {}

    breach_risk     = data.get('forrixguard_breach_risk', False)
    correction_kw   = float(data.get('forrixguard_required_correction_kw') or 0)
    time_left_sec   = int(data.get('forrixguard_time_left_seconds') or 0)
    allowed_kw      = float(data.get('forrixguard_allowed_kw') or 0)
    projected_kw    = float(data.get('forrixguard_projected_demand_kw') or 0)

    if not breach_risk or correction_kw <= 0:
        return {}

    bess_soc = float(data.get('battery0_soc') or data.get('bat0_soc') or 0)
    if bess_soc <= soc_reserve:
        return {'action': 'bess_unavailable', 'reason': f'BESS SOC {bess_soc:.0f}% at or below reserve {soc_reserve:.0f}%'}

    tariff = (config.get('site') or {}).get('tariff') or {}
    demand_charge = float(tariff.get('demandChargePerKva', 0) or 0)
    breach_cost   = round(correction_kw * demand_charge, 0) if demand_charge > 0 else None
    bess_kwh_used = round(correction_kw * (time_left_sec / 3600), 3)
    day_rate      = float(tariff.get('dayRatePerKwh', 0) or 0)
    batt_value    = round(bess_kwh_used * day_rate, 2) if day_rate > 0 else None
    currency      = tariff.get('currency', 'INR')

    return {
        'action':         'discharge_bess',
        'correction_kw':  round(correction_kw, 1),
        'time_left_sec':  time_left_sec,
        'bess_soc':       bess_soc,
        'bess_kwh_used':  bess_kwh_used,
        'breach_cost':    breach_cost,
        'batt_value':     batt_value,
        'currency':       currency,
        'control_mode':   mode,
        'auto_apply':     mode == 'CLOSED_LOOP_CONTROL',
    }


_last_auto_apply_ts: float = 0.0

async def _auto_apply_recommendation(rec: dict):
    """P4: Auto-apply BESS dispatch in CLOSED_LOOP_CONTROL mode. Rate-limited to once per window."""
    global redis_client, _last_auto_apply_ts
    now = time.time()
    if now - _last_auto_apply_ts < 840:  # at most once per 14 min
        return
    _last_auto_apply_ts = now

    correction_kw = rec.get('correction_kw', 0)
    time_left_sec = rec.get('time_left_sec', 300)
    if correction_kw <= 0 or not redis_client:
        return

    from datetime import datetime, timedelta
    import json as _json
    now_dt   = datetime.now()
    end_dt   = now_dt + timedelta(seconds=time_left_sec)
    today    = now_dt.strftime('%Y-%m-%d')
    dow_map  = {0:'Monday',1:'Tuesday',2:'Wednesday',3:'Thursday',4:'Friday',5:'Saturday',6:'Sunday'}
    power_w  = int(correction_kw * 1000)
    timeslot = {
        'timeslot': {
            'startDate': today, 'endDate': today,
            'startTime': now_dt.strftime('%H:%M:%S'), 'endTime': end_dt.strftime('%H:%M:%S'),
            'days': [dow_map[now_dt.weekday()]],
            'desiredState': {
                'minPowerGoal': -power_w, 'maxPowerGoal': -power_w,
                'minPowerConstraint': -20000, 'maxPowerConstraint': 20000,
                'minPowerLimit': -20000, 'maxPowerLimit': 20000,
                'exportMinSocConstraint': 20, 'importMaxSocConstraint': 98,
            }
        }
    }
    try:
        await redis_client.xadd('timeslots_stream', {'timeslot_json': _json.dumps(timeslot)})
        logger.info(f"[CLOSED_LOOP] Auto-applied BESS dispatch: {correction_kw} kW for {time_left_sec}s")
        await broadcast({'type': 'auto_dispatch', 'correction_kw': correction_kw, 'duration_sec': time_left_sec})
    except Exception as e:
        logger.error(f"Auto-apply failed: {e}")


async def apply_recommendation_api(request):
    """Operator-approved: write a temporary BESS discharge timeslot to Redis."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({'success': False, 'error': 'Invalid JSON'}, status=400)

    correction_kw  = float(body.get('correction_kw', 0))
    time_left_sec  = int(body.get('time_left_sec', 900))
    if correction_kw <= 0:
        return web.json_response({'success': False, 'error': 'No correction kW specified'}, status=400)

    from datetime import datetime, timedelta
    now = datetime.now()
    end_time = now + timedelta(seconds=time_left_sec)
    today_str = now.strftime('%Y-%m-%d')
    start_hms = now.strftime('%H:%M:%S')
    end_hms   = end_time.strftime('%H:%M:%S')
    dow_map   = {0:'Monday',1:'Tuesday',2:'Wednesday',3:'Thursday',4:'Friday',5:'Saturday',6:'Sunday'}
    day_name  = dow_map[now.weekday()]

    power_w = int(correction_kw * 1000)
    timeslot = {
        'timeslot': {
            'startDate': today_str, 'endDate': today_str,
            'startTime': start_hms, 'endTime': end_hms,
            'days': [day_name],
            'desiredState': {
                'minPowerGoal': -power_w, 'maxPowerGoal': -power_w,
                'minPowerConstraint': -20000, 'maxPowerConstraint': 20000,
                'minPowerLimit': -20000, 'maxPowerLimit': 20000,
                'exportMinSocConstraint': 20, 'importMaxSocConstraint': 98,
            }
        }
    }

    global redis_client
    if redis_client:
        try:
            import json as _json
            await redis_client.xadd('timeslots_stream', {'timeslot_json': _json.dumps(timeslot)})
            logger.info(f"BESS recommendation applied: {correction_kw} kW discharge for {time_left_sec}s")
            return web.json_response({'success': True, 'applied_kw': correction_kw, 'duration_sec': time_left_sec})
        except Exception as e:
            return web.json_response({'success': False, 'error': str(e)}, status=500)
    else:
        return web.json_response({'success': False, 'error': 'Redis not connected'}, status=503)


async def get_load_profile_api(request):
    """Return the 96-slot (15-min) average load profile for a given day-of-week.
    ?dow=0  → Monday … ?dow=6 → Sunday.  Omit to return all 7 days."""
    dow_str = request.rel_url.query.get('dow')
    if dow_str is not None:
        try:
            dow = int(dow_str)
            if not 0 <= dow <= 6:
                return web.json_response({'error': 'dow must be 0-6'}, status=400)
            profile = LoadProfileStore.get_profile(dow)
            counts  = [LoadProfileStore.get_count(dow, s) for s in range(96)]
            return web.json_response({'dow': dow, 'profile_kw': profile, 'sample_counts': counts})
        except ValueError:
            return web.json_response({'error': 'invalid dow'}, status=400)
    else:
        all_profiles = {}
        for d in range(7):
            all_profiles[str(d)] = {
                'profile_kw':    LoadProfileStore.get_profile(d),
                'sample_counts': [LoadProfileStore.get_count(d, s) for s in range(96)],
            }
        return web.json_response({'profiles': all_profiles, 'total_slots': len(_lp_profile)})


async def test_commissioning_api(request):
    config = load_forrixguard_config() or {}
    data = latest_data or {}
    update_real_telemetry_mapping(data, config)

    checks = []
    mapping = data.get('forrixguard_telemetry_mapping') or {}
    for label, key, value_key in [
        ('Grid import telemetry', 'grid_import', 'forrixguard_grid_import_kw'),
        ('Solar telemetry', 'solar', 'forrixguard_solar_kw'),
        ('Load telemetry', 'load', 'forrixguard_load_kw'),
        ('BESS telemetry', 'bess', 'forrixguard_bess_kw'),
    ]:
        checks.append({
            'label': label,
            'mapping': mapping.get(key, 'Not mapped'),
            'value': data.get(value_key),
            'ok': data.get(value_key) is not None or key == 'load',
        })

    checks.append({
        'label': 'DG status',
        'mapping': mapping.get('dg', 'Not configured'),
        'value': data.get('forrixguard_dg_status', 'Not configured'),
        'ok': True,
    })

    return web.json_response({'success': True, 'checks': checks})

def parse_forrixguard_info(output):
    """Parse output from /usr/bin/forrixguard_info"""
    info = {
        "serial_number": "Unknown",
        "hw_version": "Unknown",
        "sw_version": "Unknown",
        "commit_id": "Unknown",
        "app_version": "Unknown",
        "interfaces": [],
        "services": {},
        "kernel": "Unknown",
        "yocto": "Unknown"
    }
    
    # Parse line by line
    lines = output.split('\n')
    
    for line in lines:
        line = line.strip()
        if not line: continue
        
        if "Serial Number" in line:
            if ":" in line: info["serial_number"] = line.split(':', 1)[1].strip()
        elif "HW Version" in line:
             if ":" in line: info["hw_version"] = line.split(':', 1)[1].strip()
        elif "SW Version" in line:
             if ":" in line: info["sw_version"] = line.split(':', 1)[1].strip()
        elif "Git Commit ID" in line:
             if ":" in line: info["commit_id"] = line.split(':', 1)[1].strip()
        elif "forrixguard-app version" in line:
             if ":" in line: 
                 raw_ver = line.split(':', 1)[1].strip()
                 # Parse version: remove prefix 'forrixguard-app-' and suffix starting with '+'
                 clean_ver = raw_ver.replace('forrixguard-app-', '')
                 if '+' in clean_ver:
                     clean_ver = clean_ver.split('+', 1)[0]
                 # Remove known architecture suffixes like '.armv8a', '.aarch64', '.x86_64'
                 arch_suffix_match = re.search(r'\.(armv[0-9]+[a-z]*|aarch64|x86_64)$', clean_ver)
                 if arch_suffix_match:
                     clean_ver = clean_ver[:arch_suffix_match.start()]
                 info["app_version"] = clean_ver
        elif "Kernel" in line:
             if ":" in line: info["kernel"] = line.split(':', 1)[1].strip()
        elif "Yocto version" in line:
             if ":" in line: info["yocto"] = line.split(':', 1)[1].strip()
        elif "service" in line and ":" in line:
            parts = line.split(':', 1)
            service_name = parts[0].replace(" service", "").strip()
            status = parts[1].strip()
            info["services"][service_name] = status
            
        # Network Intefaces Parsing
        # Pattern: mac address <iface> : [<mac>]
        # Pattern: ip address <iface> : [<ip>]
        mac_match = re.search(r'mac address (\w+)\s+:\s+\[(.*?)\]', line)
        if mac_match:
            iface = mac_match.group(1)
            mac = mac_match.group(2)
            # Find existing or create new
            found = False
            for i in info["interfaces"]:
                if i["name"] == iface:
                    i["mac"] = mac
                    found = True
                    break
            if not found:
                info["interfaces"].append({"name": iface, "mac": mac, "ip": "N/A"})
                
        ip_match = re.search(r'ip address (\w+)\s+:\s+\[(.*?)\]', line)
        if ip_match:
            iface = ip_match.group(1)
            ip = ip_match.group(2)
            # Find existing or create new
            found = False
            for i in info["interfaces"]:
                if i["name"] == iface:
                    i["ip"] = ip
                    found = True
                    break
            if not found:
                info["interfaces"].append({"name": iface, "mac": "N/A", "ip": ip})

    return info

def read_dynamic_leases():
    """Read dynamic IP leases from dnsmasq"""
    leases = {}
    lease_file = '/var/lib/misc/dnsmasq.lease'
    if os.path.exists(lease_file):
        try:
            with open(lease_file, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 3:
                        # Format: timestamp mac ip ...
                        mac = parts[1].lower()
                        ip = parts[2]
                        leases[mac] = ip
        except Exception as e:
            logger.error(f"Error reading dynamic leases: {e}")
    return leases

def read_static_leases():
    """Read static IP leases"""
    leases = {}
    lease_file = '/etc/forrixguard-static-ip.leases'
    if os.path.exists(lease_file):
        try:
            with open(lease_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('//') or line.startswith('#'):
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        # Format: mac ip [hostname]
                        mac = parts[0].lower()
                        ip = parts[1]
                        leases[mac] = ip
        except Exception as e:
            logger.error(f"Error reading static leases: {e}")
    return leases

def is_valid_ipv4(ip):
    """Validate IPv4 address format"""
    if not ip or not isinstance(ip, str):
        return False
    # Simple regex for IPv4: 4 groups of 1-3 digits separated by dots
    # More strict: check range 0-255
    pattern = r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'
    return re.match(pattern, ip) is not None

def get_device_list():
    """Get list of devices from ForrixGuard config and resolve IPs for LAN devices"""
    config = load_forrixguard_config()
    if not config or 'devices' not in config:
        return []

    dynamic_leases = read_dynamic_leases()
    static_leases = read_static_leases()
    
    device_list = []
    
    for device in config['devices']:
        # Extract basic info
        dev_info = {
            "type": device.get("type", "Unknown"),
            "model": device.get("model", "Unknown"),
            "interface": device.get("interface", "Unknown"),
            "address": device.get("address", "N/A"),
            "mac_address": "N/A",
            "ip_address": "N/A"
        }
        
        # Handle LAN devices
        interface = dev_info["interface"].lower()
        if interface.startswith("lan-"):
            mac = device.get("mac_address", "").lower()
            if mac:
                dev_info["mac_address"] = mac.upper()
                
                # Check if it's dynamic
                is_dynamic = mac in dynamic_leases
                dev_info["is_static"] = not is_dynamic

                # Try to resolve IP: static takes precedence over dynamic (or check both)
                # Req: "First search dynamic file for mac, if not find in that, look into static lease file"
                
                ip = dynamic_leases.get(mac)
                if not ip:
                    ip = static_leases.get(mac)
                    
                # Validate IP is proper IPv4
                if ip:
                    if is_valid_ipv4(ip):
                        dev_info["ip_address"] = ip
                    else:
                        dev_info["ip_address"] = "invalid ip address"
                else:
                    dev_info["ip_address"] = "Unknown" # Or provide logic for "configure" button in frontend
            else:
                 dev_info["mac_address"] = "Unknown"
        
        device_list.append(dev_info)
        
    return device_list

def get_commissioning_summary():
    """Build a commissioning snapshot from ForrixGuard.json for setup/demo use."""
    config = load_forrixguard_config() or {}
    demand = config.get('maximumDemand', {}) or {}
    control = config.get('control', {}) or {}
    site = config.get('site', {}) or {}
    devices = config.get('devices', []) or []

    device_roles = []
    for idx, device in enumerate(devices):
        role = device.get('role')
        if not role:
            dtype = str(device.get('type', 'DEVICE')).upper()
            if dtype == 'METER':
                role = 'Grid / PV / Load telemetry'
            elif dtype == 'INVERTER':
                role = 'Solar/BESS power interface'
            elif dtype == 'BATTERY':
                role = 'BESS dispatch and reserve'
            elif dtype == 'MPPT':
                role = 'Solar generation telemetry'
            else:
                role = 'Device telemetry'

        device_roles.append({
            'index': idx,
            'type': device.get('type', 'Unknown'),
            'model': device.get('model', 'Unknown'),
            'interface': device.get('interface', 'Unknown'),
            'adapter': device.get('adapter', 'Unknown'),
            'driver': device.get('driver', 'Unknown'),
            'role': role,
        })

    return {
        'site': {
            'site_id': site.get('siteId', 'site_001'),
            'customer': site.get('customerName', 'Pilot Customer'),
            'market': site.get('market', 'India/GCC'),
            'timezone': site.get('timezone', 'Asia/Kolkata'),
            'tariff': site.get('tariffCategory', 'C&I'),
        },
        'maximum_demand': {
            'sanctioned_kw': demand.get('sanctionedDemandKw', 800),
            'allowed_kw': round(float(demand.get('sanctionedDemandKw', 800)) * float(demand.get('safetyMargin', 0.95)), 1),
            'safety_margin': demand.get('safetyMargin', 0.95),
            'window_seconds': demand.get('windowSeconds', 900),
            'control_margin_kw': demand.get('controlMarginKw', 5),
        },
        'control': {
            'mode': control.get('mode', 'MONITOR_ONLY'),
            'allow_bess_dispatch': bool(control.get('allowBessDispatch', False)),
            'allow_load_shedding': bool(control.get('allowLoadShedding', False)),
            'soc_reserve_pct': control.get('socReservePct', 35),
            'dg_optional': bool(control.get('dgOptional', True)),
        },
        'device_roles': device_roles,
        'checklist': [
            {'label': 'Grid meter telemetry mapped', 'status': any(str(d.get('type', '')).upper() == 'METER' for d in devices)},
            {'label': 'Demand contract configured', 'status': bool(demand.get('sanctionedDemandKw'))},
            {'label': 'BESS reserve policy present', 'status': any(str(d.get('type', '')).upper() == 'BATTERY' for d in devices)},
            {'label': 'Dispatch mode selected', 'status': bool(control.get('mode'))},
            {'label': 'Event proof stream enabled', 'status': True},
        ],
    }

def save_static_ip(mac, ip):
    """Save a static IP lease for the given MAC address."""
    lease_file = '/etc/forrixguard-static-ip.leases'
    try:
        # Read existing leases
        leases = {}
        if os.path.exists(lease_file):
            with open(lease_file, 'r') as f:
                for line in f:
                    parts = line.strip().split()
                    if len(parts) >= 2:
                        leases[parts[0].lower()] = parts[1]
        
        # Update lease
        leases[mac.lower()] = ip
        
        # Write back to file
        with open(lease_file, 'w') as f:
            for m, i in leases.items():
                f.write(f"{m} {i}\n")
        return True
    except Exception as e:
        logger.error(f"Error saving static IP: {e}")
        return False

async def save_ip_lease_api(request):
    """API to save a static IP lease."""
    try:
        data = await request.json()
        mac = data.get('mac')
        ip = data.get('ip')
        
        if not mac or not ip:
            return web.json_response({"error": "Missing MAC or IP address"}, status=400)
            
        if not is_valid_ipv4(ip):
             return web.json_response({"error": "Invalid IPv4 address"}, status=400)

        if save_static_ip(mac, ip):
            return web.json_response({"status": "success"})
        else:
            return web.json_response({"error": "Failed to save lease"}, status=500)
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)

async def get_system_info_api(request):
    """Execute forrixguard_info or return mock data"""
    cmd = "/usr/bin/forrixguard_info"
    output = ""
    
    if os.path.exists(cmd):
        try:
            process = await asyncio.create_subprocess_exec(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            output = stdout.decode().strip()
        except Exception as e:
            logger.error(f"Error running forrixguard_info: {e}")
            output = "Error running command"
    else:
        # Mock Data matching user example
        output = """---- ForrixGuard System Information ----

Serial Number             : FORRIXGUARD000203

HW Version                : 1.0

SW Version[meta-cem]      : main 2025-10-23

Git Commit ID[meta-cem]   : 9651348b252372ac760f5f180cafad019183565a

forrixguard-app version           : forrixguard-app-1.0+git0+e40dbbc190-r0.armv8a

mac address eth0          : [3a:14:96:0e:0e:ed]
ip address eth0           : [192.168.1.1]

mac address eth1          : [ca:0f:f7:49:d8:d0]
ip address eth1           : [N/A]

mac address wlan0         : [56:9c:ea:bd:7f:6d]
ip address wlan0          : [172.16.14.180]

mac address wwan0         : [02:4b:b3:b9:eb:e5]
ip address wwan0          : [N/A]

forrixguard-app service           : inactive

forrixguard-ble-server service   : active

bluetooth service         : active

NetworkManager service    : active

Kernel                    : Linux imx93-cem 6.1.22-imx93+ge26d42855 #1 SMP PREEMPT Tue Jul 22 08:54:09 UTC 2025 aarch64 GNU/Linux

Yocto version             : 6.1-mickledore"""

    parsed = parse_forrixguard_info(output)
    
    # Add connected device list
    device_list = get_device_list()
    logger.debug(f"Returning {len(device_list)} devices from get_device_list()")
    for idx, dev in enumerate(device_list):
        logger.debug(f"Device {idx}: {dev['type']} - {dev['model']} ({dev['interface']}:{dev['address']})")
    parsed["devices"] = device_list
    parsed["commissioning"] = get_commissioning_summary()
    
    return web.json_response(parsed)

async def get_wifi_status_api(request):
    """Get current Wi-Fi status using nmcli"""
    status = {"connected": False, "ssid": None, "ip": None, "signal": 0}
    try:
        # Check active connection
        # Use shell true to allow grep pipe
        proc = await asyncio.create_subprocess_shell(
            "nmcli -t -f ACTIVE,SSID,SIGNAL,SECURITY dev wifi | grep '^yes'",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if stdout:
            # Format: yes:SSID:SIGNAL:SECURITY
            parts = stdout.decode().strip().split(':')
            if len(parts) >= 2:
                status["connected"] = True
                status["ssid"] = parts[1]
                status["signal"] = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
    except Exception as e:
        logger.error(f"Error checking wifi status: {e}")
        # Mock for development if nmcli fails
        if not os.path.exists("/usr/bin/nmcli"):
             status = {"connected": True, "ssid": "Home-Network-5G", "ip": "192.168.1.105", "signal": 85}

    return web.json_response(status)

async def get_wifi_details_api(request):
    """Get detailed IP, Gateway, and DNS for the active Wi-Fi connection"""
    details = {"ip": "", "gateway": "", "dns": "", "method": "auto"}
    try:
        # Dynamically find the connected Wi-Fi device (e.g., wlan0, mlan0, wlp2s0)
        find_dev_proc = await asyncio.create_subprocess_shell(
            "nmcli -t -f DEVICE,TYPE,STATE dev | grep -i ':wifi:connected' | head -n 1 | cut -d':' -f1",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        dev_stdout, _ = await find_dev_proc.communicate()
        wifi_dev = dev_stdout.decode().strip()

        if not wifi_dev:
            # No Wi-Fi device detected; return an explicit error instead of assuming a default interface
            logger.warning("No connected Wi-Fi device detected by nmcli in get_wifi_details_api")
            return web.json_response({"error": "No connected WiFi device detected"}, status=404)

        # Check active connection details for the dynamically found device (excluding IPV4.METHOD) securely
        proc = await asyncio.create_subprocess_exec(
            "nmcli", "-t", "-f", "IP4.ADDRESS,IP4.GATEWAY,IP4.DNS,GENERAL.CONNECTION", "dev", "show", wifi_dev,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        conn_name = ""
        
        if proc.returncode != 0:
            logger.error(f"nmcli dev show failed for device {wifi_dev} with return code {proc.returncode}")
        elif stdout:
            lines = stdout.decode().strip().split('\n')
            for line in lines:
                if line.startswith('IP4.ADDRESS[1]:'):
                    # nmcli outputs CIDR like 192.168.1.150/24
                    ip_cidr = line.split(':', 1)[1].strip()
                    details["ip"] = ip_cidr.split('/')[0] if '/' in ip_cidr else ip_cidr
                elif line.startswith('IP4.GATEWAY:'):
                    details["gateway"] = line.split(':', 1)[1].strip()
                elif line.startswith('IP4.DNS[1]:'):
                    details["dns"] = line.split(':', 1)[1].strip()
                elif line.startswith('GENERAL.CONNECTION:'):
                    conn_name = line.split(':', 1)[1].strip()
                    
        if conn_name:
            # Now fetch the method from the connection profile securely
            method_proc = await asyncio.create_subprocess_exec(
                "nmcli", "-t", "-f", "ipv4.method", "connection", "show", conn_name,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            method_stdout, _ = await method_proc.communicate()
            if method_stdout:
                method_line = method_stdout.decode().strip()
                if method_line.startswith('ipv4.method:'):
                    details["method"] = method_line.split(':', 1)[1].strip()
    except Exception as e:
        logger.error(f"Error getting wifi details: {e}")
        # Mock for development
        if not os.path.exists("/usr/bin/nmcli"):
            details = {"ip": "192.168.1.150", "gateway": "192.168.1.1", "dns": "192.168.1.1", "method": "auto"}

    return web.json_response(details)

async def set_wifi_static_ip_api(request):
    """Set the active Wi-Fi connection to use a static IP"""
    try:
        data = await request.json()
        ssid = data.get('ssid')
        ip = data.get('ip')
        gateway = data.get('gateway')
        dns = data.get('dns')
        
        if not all([ssid, ip, gateway, dns]):
            return web.json_response({"success": False, "message": "Missing required fields (ssid, ip, gateway, dns)"})

        if '/' in ip:
            ip_cidr = ip
        else:
            prefix = None
            try:
                # 1. Try to find the active Wi-Fi device to get the runtime prefix (works for DHCP)
                find_dev_proc = await asyncio.create_subprocess_shell(
                    "nmcli -t -f DEVICE,TYPE,STATE dev | grep -i ':wifi:connected' | head -n 1 | cut -d':' -f1",
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
                dev_stdout, _ = await find_dev_proc.communicate()
                wifi_dev = dev_stdout.decode().strip()

                if wifi_dev:
                    # 2. Query the device for its current IP/Prefix
                    proc = await asyncio.create_subprocess_exec(
                        "nmcli", "-g", "IP4.ADDRESS", "device", "show", wifi_dev,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode == 0:
                        for line in stdout.decode().splitlines():
                            line = line.strip()
                            if '/' in line:
                                parts = line.split('/', 1)
                                if len(parts) == 2 and parts[1].isdigit():
                                    prefix = parts[1]
                                    break
                
                # 3. Fallback to connection profile if device lookup failed
                if not prefix:
                    proc = await asyncio.create_subprocess_exec(
                        "nmcli", "-g", "IP4.ADDRESS", "connection", "show", ssid,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                    stdout, _ = await proc.communicate()
                    if proc.returncode == 0:
                        for line in stdout.decode().splitlines():
                            line = line.strip()
                            if '/' in line:
                                parts = line.split('/', 1)
                                if len(parts) == 2 and parts[1].isdigit():
                                    prefix = parts[1]
                                    break
            except Exception as e:
                logger.warning(f"Error detecting network prefix: {e}")
                prefix = None
            
            if not prefix:
                # Preserve previous behavior as a last-resort default.
                prefix = "24"
            ip_cidr = f"{ip}/{prefix}"

        commands = [
            (
                "nmcli", "connection", "modify", ssid,
                "ipv4.method", "manual",
                "ipv4.addresses", ip_cidr,
                "ipv4.gateway", gateway,
                "ipv4.dns", dns
            ),
            (
                "nmcli", "connection", "up", ssid
            )
        ]

        for cmd in commands:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return web.json_response({"success": False, "message": f"Command failed: {' '.join(cmd)}\nError: {stderr.decode()}"})

        return web.json_response({"success": True, "message": "Static IP configured successfully"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def set_wifi_dhcp_api(request):
    """Set the active Wi-Fi connection to use DHCP (auto)"""
    try:
        data = await request.json()
        ssid = data.get('ssid')
        
        if not ssid:
            return web.json_response({"success": False, "message": "Missing required field (ssid)"})

        commands = [
            (
                "nmcli", "connection", "modify", ssid,
                "ipv4.method", "auto",
                "ipv4.addresses", "",
                "ipv4.gateway", "",
                "ipv4.dns", ""
            ),
            (
                "nmcli", "connection", "up", ssid
            )
        ]

        for cmd in commands:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                return web.json_response({"success": False, "message": f"Command failed: {' '.join(cmd)}\nError: {stderr.decode()}"})

        return web.json_response({"success": True, "message": "DHCP configured successfully"})
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def scan_wifi_api(request):
    """Scan for Wi-Fi networks"""
    networks = []
    try:
        # Rescan first
        await asyncio.create_subprocess_shell("nmcli dev wifi rescan")
        await asyncio.sleep(0.5) # Wait brief for scan
        
        proc = await asyncio.create_subprocess_shell(
            "nmcli -t -f SSID,SIGNAL,SECURITY,IN-USE dev wifi list",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        if stdout:
            seen_ssids = set()
            for line in stdout.decode().split('\n'):
                if not line: continue
                # SSID:SIGNAL:SECURITY:IN-USE
                # Use rsplit to correctly handle SSIDs containing colons
                parts = line.rsplit(':', 3)
                if len(parts) == 4:
                    ssid = parts[0]
                    # basic filtering for valid ssid
                    if not ssid or len(ssid) < 2: continue
                    if ssid in seen_ssids: continue
                    seen_ssids.add(ssid)
                    
                    signal = parts[1]
                    security = parts[2]
                    in_use = parts[-1] == '*'
                    
                    networks.append({
                        "ssid": ssid,
                        "signal": int(signal) if signal.isdigit() else 0,
                        "security": security,
                        "in_use": in_use
                    })
    except Exception as e:
        logger.error(f"Error scanning wifi: {e}")
        # Mock
        networks = [
            {"ssid": "Home-Network-5G", "signal": 90, "security": "WPA2", "in_use": True},
            {"ssid": "Guest-WiFi", "signal": 60, "security": "WPA2", "in_use": False},
            {"ssid": "Office-Mesh", "signal": 45, "security": "WPA3", "in_use": False}
        ]
        
    return web.json_response(networks)

async def connect_wifi_api(request):
    """Connect to a Wi-Fi network"""
    try:
        data = await request.json()
        ssid = data.get('ssid')
        password = data.get('password')
        
        if not ssid:
            return web.json_response({"success": False, "message": "SSID required"})
            
        cmd = ("nmcli", "dev", "wifi", "connect", ssid, "password", password)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        
        if proc.returncode == 0:
            return web.json_response({"success": True, "message": f"Connected to {ssid}"})
        else:
            return web.json_response({"success": False, "message": f"Failed to connect: {stderr.decode()}"})
            
    except Exception as e:
        return web.json_response({"success": False, "message": str(e)})

async def maintain_redis_connection():
    """Background task to maintain Redis connection"""
    global redis_client, is_connected, consumer_task
    
    while True:
        if not redis_client:
            # No connection, attempt to connect
            logger.info("Attempting to connect to Redis...")
            success = await connect_redis()
            if success:
                is_connected = True
                
                # Load existing usage data
                try:
                    await load_initial_timeslots()
                except Exception as e:
                    logger.error(f"Error loading initial timeslots: {e}")
                
                if not consumer_task or consumer_task.done():
                    consumer_task = asyncio.create_task(consume_telemetry())
                    logger.info("Started Redis consumer")
            else:
                logger.warning("Redis connection failed, retrying in 7 seconds...")
                is_connected = False
        else:
            # Connection exists, verify it's still alive by pinging
            try:
                await asyncio.wait_for(redis_client.ping(), timeout=3)
                # Ping successful, connection is healthy
                # Check if consumer task is still running
                if not consumer_task or consumer_task.done():
                    if consumer_task and consumer_task.done():
                        logger.warning("Consumer task died unexpectedly, restarting...")
                    consumer_task = asyncio.create_task(consume_telemetry())
            except (asyncio.TimeoutError, Exception) as e:
                # Ping failed, connection is broken
                logger.warning(f"Redis connection lost: {e}")
                is_connected = False
                try:
                    await redis_client.close()
                except Exception:
                    pass
                redis_client = None
                # Consumer task will be stopped when redis_client becomes None
                if consumer_task and not consumer_task.done():
                    consumer_task.cancel()
                consumer_task = None
        
        await asyncio.sleep(7) # Check interval

async def startup(app):
    global multi_dashboard_template, phase_power_comparison_template
    global time_slots_template, forrixguard_setup_template, login_template, monthly_report_template
    # Sync serial number before starting
    sync_serial_from_fuse()

    # Load and compile Jinja2 templates
    try:
        env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(os.path.join(SCRIPT_DIR, 'templates')),
            autoescape=jinja2.select_autoescape(['html', 'xml'])
        )
        env.globals['APP_VERSION'] = get_forrixguard_app_version()
        env.globals['MONITOR_VERSION'] = get_forrixguard_monitor_version()

        multi_dashboard_template = env.get_template('multi_device_dashboard.html')
        phase_power_comparison_template = env.get_template('phase_power_comparison.html')
        time_slots_template = env.get_template('time_slots.html')
        forrixguard_setup_template = env.get_template('forrixguard_setup.html')
        login_template = env.get_template('login.html')
        monthly_report_template = env.get_template('monthly_report.html')

        logger.info("Templates loaded and compiled with context-aware autoescape")
    except Exception as e:
        logger.critical(f"FATAL: Failed to load templates: {e}")
        raise RuntimeError(f"Cannot start application - template loading failed: {e}")
    
    # Start background connection maintainer without blocking
    asyncio.create_task(maintain_redis_connection())
    asyncio.create_task(LoadProfileStore.load_from_redis())

async def login_page(request):
    return web.Response(text=login_template.render(), content_type='text/html', headers=NO_CACHE_HEADERS)

async def api_login(request):
    try:
        data = await request.json()
        username = data.get('username')
        password = data.get('password')
        
        users = load_users()
        if username in users:
            hashed = hashlib.sha256(password.encode()).hexdigest()
            if users[username] == hashed:
                token = secrets.token_hex(16)
                role = 'admin' if username == 'admin' else 'user'
                # Provide properly formatted display name since CSS capitalization was removed
                display_name = username.capitalize()
                SESSIONS[token] = {'username': username, 'role': role, 'display_name': display_name}
                resp = web.json_response({'success': True, 'role': role, 'username': username, 'display_name': display_name})
                resp.set_cookie(COOKIE_NAME, token, max_age=86400, httponly=True)
                return resp
                
        return web.json_response({'success': False, 'message': 'Invalid credentials'})
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})

async def api_user_info(request):
    """Get current user info"""
    token = request.cookies.get(COOKIE_NAME)
    if token and token in SESSIONS:
        session = SESSIONS[token]
        # Handle legacy sessions if any (string vs dict) - though restart clears memory
        if isinstance(session, str):
             # Upgrade legacy session on fly? Or just return basic
             role = 'admin' if session == 'admin' else 'user'
             display_name = session.capitalize()
             return web.json_response({'username': session, 'role': role, 'display_name': display_name})
        # Ensure display_name exists in session dict (backwards compatibility)
        if 'display_name' not in session:
            session['display_name'] = session['username'].capitalize()
        return web.json_response(session)
    return web.json_response({'error': 'Not logged in'}, status=401)

async def api_logout(request):
    token = request.cookies.get(COOKIE_NAME)
    if token and token in SESSIONS:
        del SESSIONS[token]
    resp = web.json_response({'success': True})
    resp.del_cookie(COOKIE_NAME)
    return resp

async def api_change_password(request):
    try:
        data = await request.json()
        current_pass = data.get('currentPassword')
        new_username = data.get('newUsername') # Optional
        new_pass = data.get('newPassword')     # Optional
        
        token = request.cookies.get(COOKIE_NAME)
        session = SESSIONS.get(token)
        
        if not session:
             return web.json_response({'success': False, 'message': 'Not logged in'})
        
        # Handle migration or legacy session (string) vs dict
        if isinstance(session, str):
            username = session
            role = 'admin' if session == 'admin' else 'user'
            # Upgrade session structure in memory
            SESSIONS[token] = {'username': username, 'role': role}
        else:
            username = session['username']
            role = session['role']
        
        users = load_users()
        
        def validate_credential(val, name):
            if not val:
                return None
            if len(val) < 6 or len(val) > 20:
                return f"{name} must be between 6 and 20 characters"
            if not any(c.isalpha() for c in val):
                return f"{name} must contain at least one letter"
            return None

        # Verify current password
        hashed_current = hashlib.sha256(current_pass.encode()).hexdigest()
        if users.get(username) != hashed_current:
            return web.json_response({'success': False, 'message': 'Current password incorrect'})
            
        # 1. Handle Username Change
        if new_username and new_username != username:
            if role == 'admin':
                return web.json_response({'success': False, 'message': 'Admin username cannot be changed'})
            
            error = validate_credential(new_username, "Username")
            if error:
                return web.json_response({'success': False, 'message': error})
                
            if new_username in users:
                return web.json_response({'success': False, 'message': 'Username already taken'})
                
            # Move entry to new key
            users[new_username] = users.pop(username)
            # Update active session with new username and display_name
            SESSIONS[token]['username'] = new_username
            SESSIONS[token]['display_name'] = new_username.capitalize()
            username = new_username # Update local var for password step
            
        # 2. Handle Password Change
        if new_pass:
            error = validate_credential(new_pass, "Password")
            if error:
                return web.json_response({'success': False, 'message': error})
            users[username] = hashlib.sha256(new_pass.encode()).hexdigest()
        
        save_users(users)
        
        return web.json_response({'success': True, 'message': 'Credentials updated'})
        
    except Exception as e:
        return web.json_response({'success': False, 'message': str(e)})

# Update get_timeslots to log what it returns
async def _get_timeslots_removed(): pass  # replaced by ETag version above


# ============================================================================
# FIRMWARE UPDATE ENDPOINTS
# ============================================================================

async def get_firmware_status(request):
    """Get current firmware update status"""
    if not HAS_FW_UPDATE:
        return web.json_response({
            'success': False, 
            'message': 'Firmware update module not available'
        }, status=503)

    # Return a shallow copy with capped progress_messages to avoid exposing
    # internal mutable state and unbounded message history.
    status_copy = dict(update_status) if update_status is not None else {}
    msgs = status_copy.get('progress_messages')
    if isinstance(msgs, list):
        # Keep only the most recent 100 messages
        status_copy['progress_messages'] = msgs[-100:]
    return web.json_response(status_copy)


async def trigger_system_update(request):
    """Trigger system (rootfs) update"""
    if not HAS_FW_UPDATE:
        return web.json_response({
            'success': False, 
            'message': 'Firmware update module not available'
        }, status=503)
    try:
        data = await request.json()
        version = data.get('version')
        
        if not version:
            return web.json_response({
                'success': False, 
                'message': 'Version number required'
            }, status=400)
        
        # Create notification callback that broadcasts via WebSocket
        async def notify_callback(msg):
            await send_to_clients('fw_update_progress', {
                'type': 'system',
                'message': msg,
                'timestamp': datetime.now().isoformat()
            })
        
        # Start update in background
        asyncio.create_task(
            handle_system_update(notify_callback=notify_callback, version=version)
        )
        
        return web.json_response({
            'success': True,
            'message': f'System update to {version} initiated. This will reboot the system.',
            'type': 'system',
            'version': version
        })
        
    except json.JSONDecodeError:
        return web.json_response({
            'success': False, 
            'message': 'Invalid JSON'
        }, status=400)
    except Exception as e:
        return web.json_response({
            'success': False, 
            'message': f'Error: {str(e)}'
        }, status=500)


async def trigger_forrixguard_app_update(request):
    """Trigger ForrixGuard App update"""
    if not HAS_FW_UPDATE:
        return web.json_response({
            'success': False, 
            'message': 'Firmware update module not available'
        }, status=503)
    
    try:
        data = await request.json()
        version = data.get('version')
        
        if not version:
            return web.json_response({
                'success': False, 
                'message': 'Version number required'
            }, status=400)
        
        # Create notification callback that broadcasts via WebSocket
        async def notify_callback(msg):
            await send_to_clients('fw_update_progress', {
                'type': 'forrixguard_app',
                'message': msg,
                'timestamp': datetime.now().isoformat()
            })
        
        # Start update in background
        asyncio.create_task(
            handle_forrixguard_app_update(notify_callback=notify_callback, version=version)
        )
        
        return web.json_response({
            'success': True,
            'message': f'ForrixGuard App update to {version} initiated. Service will restart automatically.',
            'type': 'forrixguard_app',
            'version': version
        })
        
    except json.JSONDecodeError:
        return web.json_response({
            'success': False, 
            'message': 'Invalid JSON'
        }, status=400)
    except Exception as e:
        return web.json_response({
            'success': False, 
            'message': f'Error: {str(e)}'
        }, status=500)


async def trigger_uboot_update(request):
    """Trigger U-Boot bootloader update"""
    if not HAS_FW_UPDATE:
        return web.json_response({
            'success': False, 
            'message': 'Firmware update module not available'
        }, status=503)
    try:
        data = await request.json()
        version = data.get('version')
        
        if not version:
            return web.json_response({
                'success': False, 
                'message': 'Version number required'
            }, status=400)
        
        # Create notification callback that broadcasts via WebSocket
        async def notify_callback(msg):
            await send_to_clients('fw_update_progress', {
                'type': 'uboot',
                'message': msg,
                'timestamp': datetime.now().isoformat()
            })
        
        # Start update in background
        asyncio.create_task(
            handle_uboot_update(notify_callback=notify_callback, version=version)
        )
        
        return web.json_response({
            'success': True,
            'message': f'U-Boot update to {version} initiated. This will reboot the system.',
            'type': 'uboot',
            'version': version
        })
        
    except json.JSONDecodeError:
        return web.json_response({
            'success': False, 
            'message': 'Invalid JSON'
        }, status=400)
    except Exception as e:
        return web.json_response({
            'success': False, 
            'message': f'Error: {str(e)}'
        }, status=500)



async def reboot_system(request):
    """
    Reboot the system
    """
    try:
        logger.info("Received request to reboot system")
        
        # We start the reboot in the background to allow the API to return success first
        asyncio.create_task(asyncio.create_subprocess_shell("sleep 1 && /sbin/reboot"))
        
        return web.json_response({'success': True, 'message': 'System is rebooting...'})
        
    except Exception as e:
        logger.error(f"Exception during reboot: {e}")
        return web.json_response({
            'success': False, 
            'message': f'Error: {str(e)}'
        }, status=500)

async def restart_forrixguard_app(request):
    """
    Restart the ForrixGuard App service
    Stops the service, waits 1 second, then starts it again
    """
    try:
        # Check if auth required - for now assuming open access on local network 
        # or relying on existing dashboard security if implemented
        
        logger.info("Received request to restart forrixguard-app")
        
        # Stop the service
        logger.info("Stopping forrixguard-app service...")
        stop_proc = subprocess.run(["systemctl", "stop", "forrixguard-app"], capture_output=True, text=True)
        if stop_proc.returncode != 0:
            logger.error(f"Failed to stop forrixguard-app: {stop_proc.stderr}")
            return web.json_response({
                'success': False, 
                'message': f'Failed to stop service: {stop_proc.stderr}'
            }, status=500)
            
        # Wait a moment
        await asyncio.sleep(1)
        
        # Start the service
        logger.info("Starting forrixguard-app service...")
        start_proc = subprocess.run(["systemctl", "start", "forrixguard-app"], capture_output=True, text=True)
        if start_proc.returncode != 0:
            logger.error(f"Failed to start forrixguard-app: {start_proc.stderr}")
            return web.json_response({
                'success': False, 
                'message': f'Failed to start service: {start_proc.stderr}'
            }, status=500)
            
        return web.json_response({'success': True, 'message': 'ForrixGuard App restarted successfully'})
        
    except Exception as e:
        logger.error(f"Exception during restart: {e}")
        return web.json_response({
            'success': False, 
            'message': f'Error: {str(e)}'
        }, status=500)



async def upload_forrixguard_json(request):
    """
    Upload a new ForrixGuard configuration file.
    Only accessible by admin users.
    Stops forrixguard-app service before writing the file.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token or token not in SESSIONS or SESSIONS[token].get('role') != 'admin':
        return web.json_response({'success': False, 'message': 'Admin privileges required'}, status=403)

    try:
        reader = await request.multipart()
        field = await reader.next()
        
        if field is None or field.name != 'file':
            return web.json_response({'success': False, 'message': 'No file field provided'}, status=400)

        filename = field.filename
        if not filename.endswith('.json'):
            return web.json_response({'success': False, 'message': 'Invalid file type. Only .json files are allowed.'}, status=400)

        content = await field.read(decode=True)
        
        # Normalize line endings: CRLF -> LF
        try:
            content_str = content.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
            # Re-encode to bytes for writing, using the same variable name
            content = content_str.encode('utf-8')
        except UnicodeDecodeError:
            # If it's not valid UTF-8, it might be corrupted or in another encoding
            return web.json_response({'success': False, 'message': 'Invalid file encoding. UTF-8 is required.'}, status=400)

        # Validate JSON content and perform minimal schema/type checks
        try:
            config = json.loads(content_str)
        except json.JSONDecodeError:
            return web.json_response({'success': False, 'message': 'Invalid JSON format'}, status=400)

        # Minimal schema validation to avoid crashes in forrixguard-app
        if not isinstance(config, dict):
            return web.json_response({
                'success': False, 
                'message': 'Invalid JSON structure: top-level object is required.'
            }, status=400)
            
        # Required fields that must be strings, as forrixguard-app expects (e.g., strcmp on valuestring)
        required_string_fields = ['device_type']
        for field_name in required_string_fields:
            value = config.get(field_name)
            if not isinstance(value, str):
                return web.json_response({
                    'success': False, 
                    'message': f"Invalid configuration: '{field_name}' must be a string."
                }, status=400)

        logger.info(f"Received new ForrixGuard configuration upload from {SESSIONS[token].get('username')}")

        # Stop forrixguard-app service before writing
        logger.info("Stopping forrixguard-app service for configuration update...")
        stop_result = subprocess.run(["systemctl", "stop", "forrixguard-app"], capture_output=True, text=True)
        
        if stop_result.returncode != 0:
            logger.error(f"Failed to stop forrixguard-app service (return code {stop_result.returncode}): {stop_result.stderr}")
            return web.json_response({
                'success': False, 
                'message': 'Failed to stop forrixguard-app service. Configuration was not updated.'
            }, status=500)
        
        # Write file atomically
        target_path = FORRIXGUARD_CONFIG_PATH
        temp_path = f"{target_path}.tmp"
        
        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
            # Write to temporary file
            with open(temp_path, 'wb') as f:
                f.write(content)
                f.flush()
                # Ensure it's written to disk
                os.fsync(f.fileno())
            
            # Atomic replacement
            os.replace(temp_path, target_path)
            
            # Delete serial sync flag to force re-sync on next startup
            sync_flag = '/tmp/.serial_synced'
            if os.path.exists(sync_flag):
                try:
                    os.remove(sync_flag)
                    logger.info("Cleared serial sync flag for new configuration")
                except Exception as e:
                    logger.warning(f"Failed to clear serial sync flag: {e}")

            logger.info(f"Successfully updated {target_path} atomically")

        except Exception as e:
            logger.error(f"Error during configuration write: {e}")
            if os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except:
                    pass
            
            # Attempt to restart the service if we failed to update the config
            logger.info("Configuration update failed, attempting to restart forrixguard-app...")
            subprocess.run(["systemctl", "start", "forrixguard-app"], capture_output=True, text=True)
            
            return web.json_response({
                'success': False, 
                'message': f'Error writing configuration: {str(e)}. Attempted to restart the service.'
            }, status=500)
        
        return web.json_response({
            'success': True, 
            'message': 'Configuration uploaded successfully. The forrixguard-app service is currently stopped.'
        })

    except Exception as e:
        logger.error(f"Error during configuration upload: {e}")
        return web.json_response({'success': False, 'message': f'Error: {str(e)}'}, status=500)


async def export_forrixguard_json(request):
    """
    Download the current ForrixGuard configuration file.
    Only accessible by admin users.
    """
    token = request.cookies.get(COOKIE_NAME)
    if not token or token not in SESSIONS or SESSIONS[token].get('role') != 'admin':
        return web.json_response({'success': False, 'message': 'Admin privileges required'}, status=403)

    if not os.path.exists(FORRIXGUARD_CONFIG_PATH):
        return web.json_response({'success': False, 'message': 'Configuration file not found'}, status=404)

    try:
        return web.FileResponse(
            path=FORRIXGUARD_CONFIG_PATH,
            headers={
                'Content-Disposition': f'attachment; filename="ForrixGuard.json"'
            }
        )
    except Exception as e:
        logger.error(f"Error during configuration export: {e}")
        return web.json_response({'success': False, 'message': f'Error: {str(e)}'}, status=500)


if __name__ == '__main__':
    app.on_startup.append(startup)
    
    # Auth Routes
    app.router.add_get('/login', login_page)
    app.router.add_post('/api/login', api_login)
    app.router.add_post('/api/logout', api_logout)
    app.router.add_post('/api/change-password', api_change_password)
    app.router.add_get('/api/me', api_user_info)
    app.router.add_get('/api/user-info', api_user_info)  # Alias for template compatibility

    # Redirect / to /multi as home landing page
    app.router.add_get('/', multi_dashboard)
    app.router.add_get('/multi', multi_dashboard)
    app.router.add_get('/phase-power-comparison', phase_power_comparison)
    app.router.add_get('/power-time-settings', time_slots_dashboard)
    app.router.add_get('/api/data', get_latest)
    app.router.add_post('/api/publish', publish_settings)
    app.router.add_get('/api/timeslots', get_timeslots)
    app.router.add_get('/api/smart-power-control', get_smart_power_control)

    # ForrixGuard Setup Routes
    app.router.add_get('/forrixguard-setup', forrixguard_setup_page)
    app.router.add_get('/monthly-report', monthly_report_page)
    app.router.add_get('/api/monthly-report', api_monthly_report)
    app.router.add_get('/api/commissioning', get_commissioning_api)
    app.router.add_post('/api/commissioning', save_commissioning_api)
    app.router.add_post('/api/commissioning/test', test_commissioning_api)
    app.router.add_get('/api/tariff', get_tariff_api)
    app.router.add_post('/api/tariff', save_tariff_api)
    app.router.add_get('/api/load-profile', get_load_profile_api)
    app.router.add_post('/api/apply-recommendation', apply_recommendation_api)
    app.router.add_get('/api/system-info', get_system_info_api)
    app.router.add_get('/api/wifi/status', get_wifi_status_api)
    app.router.add_get('/api/wifi/details', get_wifi_details_api)
    app.router.add_get('/api/wifi/scan', scan_wifi_api)
    app.router.add_post('/api/wifi/connect', connect_wifi_api)
    app.router.add_post('/api/wifi/static', set_wifi_static_ip_api)
    app.router.add_post('/api/wifi/dhcp', set_wifi_dhcp_api)
    app.router.add_post('/api/save-ip-lease', save_ip_lease_api)

    # System Control Routes
    app.router.add_post('/api/system/restart-app', restart_forrixguard_app)
    app.router.add_post('/api/system/reboot', reboot_system)
    app.router.add_post('/api/system/upload-forrixguard-json', upload_forrixguard_json)
    app.router.add_get('/api/system/export-forrixguard-json', export_forrixguard_json)

    # Firmware Update Routes
    app.router.add_get('/api/firmware/status', get_firmware_status)
    app.router.add_post('/api/firmware/update/system', trigger_system_update)
    app.router.add_post('/api/firmware/update/forrixguard-app', trigger_forrixguard_app_update)
    app.router.add_post('/api/firmware/update/uboot', trigger_uboot_update)

    app.router.add_static('/static/', path=os.path.join(SCRIPT_DIR, 'static'), name='static')
    app.router.add_get('/ws', websocket_handler)
    logger.info("Starting 3-Phase Inverter Dashboard...")
    logger.info("Dashboard starts DISCONNECTED - select device and press Connect")
    logger.info(f"Redis host: {REDIS_HOST}:{REDIS_PORT}")
    logger.info("Open browser to: http://0.0.0.0:5000")
    
    web.run_app(app, host='0.0.0.0', port=5000)
