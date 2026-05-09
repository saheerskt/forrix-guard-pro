#!/usr/bin/env python3
"""
Firmware Update Utility for ForrixGuard Monitor integrated with HTTP Dashboard.
Supports System (rootfs), ForrixGuard App, and U-Boot updates.
"""

import subprocess
import time
import logging
import os
import json
import asyncio
import aiohttp
from datetime import datetime
from typing import Callable, Optional
import shutil
import inspect

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('fw_update')

# Grouped Configuration
class HW:
    MODEL = "imx93-cem"
    REVISION = "1.0"
    MMC_BOOT0 = "/dev/mmcblk0boot0"
    FORCE_RO_PATH = "/sys/block/mmcblk0boot0/force_ro"


class URL:
    DOMAIN = "d30hwl3q517a32.cloudfront.net"
    BASE = f"https://{DOMAIN}"
    ROOTFS = f"{BASE}/rootfs/"
    UBOOT = f"{BASE}/uboot/"
    # For swupdate template
    SWU_FILE_TEMPLATE = "forrix-cem-image-swu-{hw_model}-{version}.swu"


class PATH:
    DOWNLOAD_DIR = "/tmp"
    # Authentication preservation
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
    AUTH_FILE = os.path.join(SCRIPT_DIR, 'auth.json')
    AUTH_BACKUP_DIR = os.path.join(SCRIPT_DIR, '.auth_backup')
    # Update lock
    LOCK = "/run/forrixguard-update.lock"
    # Verification
    SWUPDATE_PUBKEY = "/etc/swupdate/public.pem"


# Initialize lock path - ensure writable fallback
_lock_path = PATH.LOCK
_lock_dir = os.path.dirname(_lock_path)
if not os.access(_lock_dir, os.W_OK):
    _lock_path = os.path.join('/tmp', 'forrixguard-update.lock')
    logger.warning(f"Lock directory {_lock_dir} is not writable. Using fallback: {_lock_path}")


class NET:
    # Connectivity targets
    IPV4_TARGETS = ["8.8.8.8", "1.1.1.1"]
    IPV6_TARGETS = ["2001:4860:4860::8888", "2606:4700:4700::1111"]
    DNS_TARGET = "google.com"


# Initialize secure backup directory - avoid mutating class attributes at runtime
_auth_backup_dir = PATH.AUTH_BACKUP_DIR
_auth_backup_dir_usable = False

try:
    os.makedirs(_auth_backup_dir, mode=0o700, exist_ok=True)
    os.chmod(_auth_backup_dir, 0o700)
    _auth_backup_dir_usable = os.access(_auth_backup_dir, os.W_OK)
except OSError as e:
    logger.warning(f"Could not ensure secure permissions on AUTH_BACKUP_DIR {_auth_backup_dir}: {e}")
    _auth_backup_dir_usable = False

if not _auth_backup_dir_usable:
    fallback_dir = os.path.join('/tmp', 'forrixguard-monitor-auth-backup')
    try:
        os.makedirs(fallback_dir, mode=0o700, exist_ok=True)
        os.chmod(fallback_dir, 0o700)
        if os.access(fallback_dir, os.W_OK):
            logger.warning(f"Using fallback authentication backup directory at {fallback_dir}")
            _auth_backup_dir = fallback_dir
            _auth_backup_dir_usable = True
        else:
            logger.warning(f"Fallback AUTH_BACKUP_DIR at {fallback_dir} is not writable; auth backup will be disabled")
    except OSError as e:
        logger.warning(f"Could not create or set permissions on fallback AUTH_BACKUP_DIR {fallback_dir}: {e}")
        _auth_backup_dir_usable = False

AUTH_BACKUP = os.path.join(_auth_backup_dir, 'auth.json.bak') if _auth_backup_dir_usable else None

# Update status tracking
update_status = {
    'in_progress': False,
    'current_type': None,
    'current_version': None,
    'progress_messages': [],
    'last_update': None
}


class UpdateLock:
    """Context manager to ensure only one update runs at a time"""
    def __init__(self, lock_path: str, notify_callback: Optional[Callable] = None):
        self.lock_path = lock_path
        self.notify_callback = notify_callback
        self.acquired = False

    async def __aenter__(self):
        last_error = "Unknown error"
        for attempt in range(2):
            try:
                # Atomically create the lock file; fail if it already exists
                try:
                    fd = os.open(self.lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode=0o600)
                except (PermissionError, OSError) as e:
                    if isinstance(e, FileExistsError):
                        raise # Handled by the outer except FileExistsError
                    msg = f"Failed to create update lock file '{self.lock_path}': {e}"
                    await log_async(msg, self.notify_callback, to_ui=False)
                    last_error = msg
                    raise RuntimeError(msg)

                # Wrap the fd in a file object. fdopen takes ownership of fd.
                try:
                    f = os.fdopen(fd, 'w')
                except Exception:
                    os.close(fd)
                    raise

                try:
                    with f:
                        f.write(str(os.getpid()))
                except Exception:
                    if os.path.exists(self.lock_path):
                        os.remove(self.lock_path)
                    raise
                
                self.acquired = True
                update_status['in_progress'] = True
                return self

            except FileExistsError:
                # Lock exists, check if it's stale
                try:
                    with open(self.lock_path, 'r') as f:
                        pid_str = f.read().strip()
                    
                    if pid_str:
                        pid = int(pid_str)
                        os.kill(pid, 0) # Use signal 0 to check if process exists
                        # Process is alive
                        msg = f"Update already in progress (PID {pid}). Please wait."
                        last_error = msg
                        raise RuntimeError(msg)
                    else:
                        raise ValueError("Empty PID in update lock file.")
                except (ProcessLookupError, ValueError):
                    # Process is dead or file is junk - remove it and try one more time
                    if attempt == 0:
                        msg = "Removing stale or invalid update lock file."
                        await log_async(msg, self.notify_callback, to_ui=False)
                        try:
                            os.remove(self.lock_path)
                            continue 
                        except Exception as e:
                            last_error = f"Failed to remove stale lock file: {e}"
                            # Fall through to final error
                except PermissionError:
                    msg = "Update already in progress (system lock). Please wait."
                    last_error = msg
                    raise RuntimeError(msg)
                except Exception as e:
                    last_error = str(e)
                    if not isinstance(e, RuntimeError):
                        msg = f"Unexpected error checking update lock: {e}"
                        raise RuntimeError(msg)
                    raise
        
        # If we get here, acquisition failed after all attempts
        await log_async(last_error, self.notify_callback)
        raise RuntimeError(last_error)

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self.acquired:
            try:
                if os.path.exists(self.lock_path):
                    os.remove(self.lock_path)
            except OSError as e:
                # Log cleanup failures but do not override any original exception
                await log_async(f"Failed to remove update lock file '{self.lock_path}': {e}", self.notify_callback)
            finally:
                update_status['in_progress'] = False
                self.acquired = False


def mask_sensitive(text: str) -> str:
    """Mask sensitive URLs/domains from being broadcasted to UI"""
    if not text:
        return text
    # Mask known base URLs (rootfs and uboot)
    for base_url, label in [(URL.ROOTFS, "rootfs"), (URL.UBOOT, "uboot")]:
        http_base = base_url.replace("https://", "http://")
        https_base = base_url.replace("http://", "https://")
        mask = f"https://[FIRMWARE_SERVER]/{label}/"
        text = text.replace(http_base, mask)
        text = text.replace(https_base, mask)
    # Mask domain if it appears in any other context
    text = text.replace(URL.DOMAIN, "[SERVER_HIDDEN]")
    return text


def log(msg: str, notify_callback: Optional[Callable] = None, to_ui: bool = True):
    """Log message to both logger and callback"""
    logger.info(msg)
    
    if not to_ui:
        return
        
    safe_msg = mask_sensitive(msg)
    
    if notify_callback:
        # Check if callback is a coroutine and schedule it properly
        if inspect.iscoroutinefunction(notify_callback):
            # It's an async function, don't call it here
            # The caller (async function) should handle it
            pass
        else:
            # It's a regular function, call it
            notify_callback(safe_msg)
    # Also store in update_status for WebSocket broadcast
    update_status['progress_messages'].append({
        'timestamp': datetime.now().isoformat(),
        'message': safe_msg
    })


async def log_async(msg: str, notify_callback: Optional[Callable] = None, to_ui: bool = True):
    """Async version of log that can properly await async callbacks"""
    logger.info(msg)
    
    if not to_ui:
        return
        
    safe_msg = mask_sensitive(msg)
    
    if notify_callback:
        if inspect.iscoroutinefunction(notify_callback):
            await notify_callback(safe_msg)
        else:
            notify_callback(safe_msg)
    # Also store in update_status for WebSocket broadcast
    update_status['progress_messages'].append({
        'timestamp': datetime.now().isoformat(),
        'message': safe_msg
    })


def check_internet(notify_callback: Optional[Callable] = None) -> bool:
    """
    Check for internet connectivity via IPv4 or IPv6 and verify DNS resolution.
    Returns True if connectivity and DNS are available.
    """
    # Check for default IPv4 route
    route_v4 = subprocess.run(["ip", "route"], capture_output=True, text=True)
    has_v4_route = "default" in route_v4.stdout
    
    # Check for default IPv6 route
    route_v6 = subprocess.run(["ip", "-6", "route"], capture_output=True, text=True)
    has_v6_route = "default" in route_v6.stdout
    
    if not has_v4_route and not has_v6_route:
        log("No default route found (IPv4 or IPv6). Network is unreachable.", notify_callback)
        return False

    # 1. First try DNS resolution (most reliable test for dnf)
    log(f"Testing DNS resolution via {NET.DNS_TARGET}...", notify_callback)
    try:
        try:
            res = subprocess.run(["getent", "hosts", NET.DNS_TARGET], capture_output=True, text=True)
            if res.returncode == 0:
                # If resolution works, try a quick ping to verify path
                if subprocess.run(["ping", "-c", "1", "-W", "2", NET.DNS_TARGET], 
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                    log(f"DNS resolution and path to {NET.DNS_TARGET} work.", notify_callback)
                    return True
        except FileNotFoundError:
            # getent is not installed, continue to fallback ping tests
            pass
    except Exception as e:
        log(f"DNS resolution test error: {e}", notify_callback)

    # 2. Fallback to IP pings if DNS test failed or was inconclusive
    if has_v4_route:
        log("Testing IPv4 path connectivity...", notify_callback)
        for target in NET.IPV4_TARGETS:
            if subprocess.run(["ping", "-c", "1", "-W", "2", target], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                log(f"IPv4 connectivity via {target} works.", notify_callback)
                return True
    
    if has_v6_route:
        log("Testing IPv6 path connectivity...", notify_callback)
        for target in NET.IPV6_TARGETS:
            if subprocess.run(["ping6", "-c", "1", "-W", "2", target], 
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
                log(f"IPv6 connectivity via {target} works.", notify_callback)
                return True
    
    log("No full internet connectivity detected (DNS resolution or path probes failed).", notify_callback)
    return False


async def run_cmd_stream_async(cmd: list, notify_callback: Optional[Callable] = None, env: dict = None,
                               log_cmd_ui: bool = True, log_output_ui: bool = True) -> subprocess.CompletedProcess:
    """Run a command asynchronously and stream its stdout/stderr to the callback."""
    # Log to UI only if requested (log_async will also handle journal recording)
    if log_cmd_ui:
        await log_async(f"Running: {' '.join(cmd)}", notify_callback, to_ui=True)
    else:
        # If UI logging suppressed, still record the command in the journal
        logger.info(f"Running: {' '.join(cmd)}")
    
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,  # Merge stderr into stdout
        env=env
    )
    
    stdout_lines = []
    
    while True:
        line = await process.stdout.readline()
        if not line:
            break
        decoded_line = line.decode('utf-8', errors='replace').rstrip()
        stdout_lines.append(decoded_line)
        # Avoid spamming websocket with empty lines
        # Consistently log output via log_async
        if decoded_line.strip():
            # log_async handles journal logging (logger.info) AND UI logging
            await log_async(decoded_line, notify_callback, to_ui=log_output_ui)
            
    await process.wait()
    
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=process.returncode,
        stdout="\n".join(stdout_lines),
        stderr=""
    )


async def run_dnf_async(args: list, notify_callback: Optional[Callable] = None,
                      log_cmd_ui: bool = True, log_output_ui: bool = True) -> subprocess.CompletedProcess:
    """Run dnf package manager asynchronously with specified arguments and stream output"""
    cmd = ["dnf", "-y", "--enablerepo=*", *args]
    return await run_cmd_stream_async(cmd, notify_callback, log_cmd_ui=log_cmd_ui, log_output_ui=log_output_ui)


def dnf_success(result: subprocess.CompletedProcess, action: str = "", 
                notify_callback: Optional[Callable] = None) -> bool:
    """Check if dnf operation was successful"""
    if result.returncode != 0:
        log(f"dnf {action} failed with return code {result.returncode}.", notify_callback)
        return False
    
    output = (result.stdout or "") + (result.stderr or "")
    failure_indicators = ["Failed", "Error", "Cannot", "No match", "not found", "Nothing to do"]
    
    if any(indicator in output for indicator in failure_indicators):
        log(f"dnf {action} output indicates failure or no action: {output.strip()}", notify_callback)
        return False
    
    return True


async def cleanup_dnf_cache(notify_callback: Optional[Callable] = None):
    """Clean all dnf cache and temporary files to save space"""
    await log_async("Cleaning dnf cache and temporary files...", notify_callback, to_ui=False)
    try:
        # Run 'dnf clean all' which removes cache, metadata, and temporary files
        proc = await asyncio.create_subprocess_exec(
            "dnf", "clean", "all",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            await log_async("dnf cache cleaned successfully.", notify_callback, to_ui=False)
        else:
            err = stderr.decode('utf-8', errors='ignore') if stderr else ''
            await log_async(f"Warning: 'dnf clean all' exited {proc.returncode}: {err}", notify_callback, to_ui=False)
    except Exception as e:
        await log_async(f"Warning: Exception running 'dnf clean all': {e}", notify_callback, to_ui=False)


async def log_rpm_query_async(package: str, notify_callback: Optional[Callable] = None, prefix: str = ""):
    """Query and log current RPM package version"""
    process = await asyncio.create_subprocess_exec(
        "rpm", "-q", package,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    version = stdout.decode('utf-8').strip() if process.returncode == 0 else stderr.decode('utf-8').strip()
    msg = f"{prefix + ': ' if prefix else ''}{version}"
    await log_async(msg, notify_callback)


async def download_file_async(url: str, path: str, notify_callback: Optional[Callable] = None) -> bool:
    """Download a file asynchronously with progress reporting via aiohttp"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                if response.status != 200:
                    await log_async(f"Download failed with status {response.status}", notify_callback, to_ui=False)
                    return False
                
                content_length = response.content_length
                downloaded = 0
                last_reported_percent = -5  # Report every 5%
                last_reported_mb = 0.0  # Report every ~10MB when content-length missing
                
                with open(path, 'wb') as f:
                    async for chunk in response.content.iter_chunked(1024 * 1024):  # 1MB chunks
                        # Write chunk on a thread to avoid blocking the event loop
                        await asyncio.to_thread(f.write, chunk)
                        downloaded += len(chunk)
                        
                        if content_length:
                            percent = int((downloaded / content_length) * 100)
                            if percent >= last_reported_percent + 5:
                                await log_async(f"[•] Downloading... {percent}%", notify_callback)
                                last_reported_percent = percent
                        else:
                            # Fallback if content-length is missing — throttle updates to avoid spamming UI/logs
                            mb_downloaded = downloaded / (1024 * 1024)
                            # Report immediately for the first MB, then every 10MB thereafter
                            if (last_reported_mb == 0.0 and mb_downloaded >= 1.0) or (mb_downloaded >= last_reported_mb + 10.0):
                                await log_async(f"[•] Downloading... {mb_downloaded:.1f} MB", notify_callback)
                                last_reported_mb = mb_downloaded
                
                await log_async("[•] Downloading... 100%", notify_callback)
                return True
    except Exception as e:
        await log_async(f"Download exception: {e}", notify_callback, to_ui=False)
        return False


async def handle_system_update(notify_callback: Optional[Callable] = None, 
                               version: Optional[str] = None) -> bool:
    """
    Update rootfs (OS/Kernel) to specified version
    Downloads SWU image and runs swupdate
    """
    async with UpdateLock(_lock_path, notify_callback):
        update_status['current_type'] = 'system'
        update_status['current_version'] = version
        update_status['progress_messages'] = []
        
        async def fail_update(reason: str):
            """Helper to handle update failure"""
            await log_async(reason, notify_callback)
            update_status['last_update'] = {
                'type': 'system',
                'version': version,
                'timestamp': datetime.now().isoformat(),
                'status': 'failed',
                'error': reason
            }
            if notify_callback:
                logger.info("Sending STATUS:FAILED to callback")
                if inspect.iscoroutinefunction(notify_callback):
                    await notify_callback('STATUS:FAILED')
                else:
                    notify_callback('STATUS:FAILED')
            return False

        try:
            if not check_internet(notify_callback):
                return await fail_update("No internet connectivity. Aborting system update.")
            
            if not version:
                return await fail_update("A version is required for system update.")
            
            swu_file = URL.SWU_FILE_TEMPLATE.format(hw_model=HW.MODEL, version=version)
            swu_url = f"{URL.ROOTFS}{swu_file}"
            # Download and update in one step using swupdate's integrated downloader
            await log_async("Starting system update (download & install)...", notify_callback)
            
            swupdate_h_arg = f"{HW.MODEL}:{HW.REVISION}"
            # Pass downloader options as a single grouped string to avoid collision with top-level flags
            # (e.g. -w is 'webserver' at top-level but 'retrywait' inside -d)
            downloader_args = f"-u {swu_url} -r 5 -t 10 --retrywait 5"
            swupdate_cmd = [
                "swupdate",
                "-H", swupdate_h_arg,
                "-d", downloader_args
            ]
            
            swupdate = await run_cmd_stream_async(
                swupdate_cmd, 
                notify_callback,
                log_cmd_ui=False,
                log_output_ui=False
            )
            swupdate_returncode = swupdate.returncode
            
            if swupdate_returncode != 0:
                await log_async("swupdate failed, retrying with LC_CTYPE...", notify_callback)
                env = {**dict(os.environ), "LC_CTYPE": "en_US.utf8"}
                swupdate = await run_cmd_stream_async(
                    swupdate_cmd,
                    notify_callback, 
                    env=env,
                    log_cmd_ui=False,
                    log_output_ui=False
                )
                swupdate_returncode = swupdate.returncode
            
            if swupdate_returncode != 0:
                await log_async("swupdate failed.", notify_callback)
                await log_async(f"Output: {swupdate.stdout}", notify_callback, to_ui=False)
                return await fail_update("swupdate execution failed")
            
            await log_async("System update completed successfully.", notify_callback)
            await log_async("Rebooting system to apply update...", notify_callback)
            
            # Schedule reboot (not immediate to allow response to complete)
            subprocess.Popen(["sh", "-c", "sleep 2 && reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            update_status['last_update'] = {
                'type': 'system',
                'version': version,
                'timestamp': datetime.now().isoformat(),
                'status': 'completed_rebooting'
            }
            
            # Send final success message
            if notify_callback:
                if inspect.iscoroutinefunction(notify_callback):
                    await notify_callback('STATUS:SUCCESS')
                else:
                    notify_callback('STATUS:SUCCESS')
            
            return True
            
        except Exception as e:
            return await fail_update(f"Exception during system update: {e}")


async def handle_forrixguard_app_update(notify_callback: Optional[Callable] = None, 
                                version: Optional[str] = None) -> bool:
    """
    Update ForrixGuard App package to specified version using dnf
    Installs forrixguard-app-{version} package
    """
    async with UpdateLock(_lock_path, notify_callback):
        update_status['current_type'] = 'forrixguard_app'
        update_status['current_version'] = version
        update_status['progress_messages'] = []

        async def fail_update(reason: str):
            """Helper to handle update failure"""
            await log_async(reason, notify_callback)
            update_status['last_update'] = {
                'type': 'forrixguard_app',
                'version': version,
                'timestamp': datetime.now().isoformat(),
                'status': 'failed',
                'error': reason
            }
            if notify_callback:
                if inspect.iscoroutinefunction(notify_callback):
                    await notify_callback('STATUS:FAILED')
                else:
                    notify_callback('STATUS:FAILED')
            return False
        
        try:
            if not check_internet(notify_callback):
                return await fail_update("No internet connectivity. Aborting ForrixGuard app update.")
            
            if not version:
                return await fail_update("A version is required for ForrixGuard app update.")
            
            await log_async(f"Starting ForrixGuard app update to version {version}...", notify_callback)
            
            # Check current version (log without callback since we'll do it async)
            await log_rpm_query_async("forrixguard-app", None, "Current forrixguard-app version")
            
            # Backup auth.json if it exists
            # Backup auth.json - quiet to UI
            backup_success = False
            if PATH.AUTH_FILE and os.path.exists(PATH.AUTH_FILE):
                if AUTH_BACKUP:
                    await log_async(f"Backing up {PATH.AUTH_FILE} to {AUTH_BACKUP}...", notify_callback, to_ui=False)
                    try:
                        # Use shutil.copy in a thread to avoid spawning shell processes
                        await asyncio.to_thread(shutil.copy, PATH.AUTH_FILE, AUTH_BACKUP)
                        backup_success = True
                    except Exception as e:
                        return await fail_update(f"Authentication backup failed: {e}")
                else:
                    return await fail_update("No usable authentication backup path")

            # Install specific version - hide raw dnf command from UI
            pkg = f"forrixguard-app-{version}"
            await log_async(f"Installing package version {version}...", notify_callback)
            
            try:
                result = await run_dnf_async(
                    ["install", pkg], 
                    notify_callback,
                    log_cmd_ui=False,
                    log_output_ui=False
                )
            finally:
                # Restore auth.json if backup was successful
                if backup_success and AUTH_BACKUP and os.path.exists(AUTH_BACKUP):
                    await log_async(f"Restoring {PATH.AUTH_FILE} from backup...", notify_callback, to_ui=False)
                    try:
                        # Restore using shutil.copy in a thread instead of invoking shell commands
                        await asyncio.to_thread(shutil.copy, AUTH_BACKUP, PATH.AUTH_FILE)
                        # Cleanup backup
                        try:
                            os.remove(AUTH_BACKUP)
                        except Exception as e:
                            await log_async(f"Warning: Failed to remove backup file: {e}", notify_callback, to_ui=False)
                    except Exception as e:
                        await log_async(f"CRITICAL: Failed to restore auth.json: {e}", notify_callback)
                        # We don't return False here because the package update itself might have succeeded,
                        # but the user will need to log in again or fix permissions.

            if dnf_success(result, action=f"install {pkg}", notify_callback=None):
                await log_async("ForrixGuard app package installed successfully.", notify_callback)
                await log_rpm_query_async("forrixguard-app", None, "New forrixguard-app version")
                
                # Cleanup after successful installation to control disk usage
                await cleanup_dnf_cache(notify_callback)
                
                # Reboot device to apply update and ensure clean state (especially important if there were scriptlet failures during installation)
                await log_async("Scheduling system reboot to apply ForrixGuard app update...", notify_callback)
                subprocess.Popen(
                    ["sh", "-c", "sleep 2 && reboot"], 
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                )
                
                update_status['last_update'] = {
                    'type': 'forrixguard_app',
                    'version': version,
                    'timestamp': datetime.now().isoformat(),
                    'status': 'completed_rebooting'
                }
                
                # Send final success message
                if notify_callback:
                    if inspect.iscoroutinefunction(notify_callback):
                        await notify_callback('STATUS:SUCCESS')
                    else:
                        notify_callback('STATUS:SUCCESS')
                
                return True
            else:
                await log_async("dnf returned non-zero; inspecting installed package state...", notify_callback)
                # Check if the EXACT requested version is installed — rpm performs strict matching
                rpm_check = await asyncio.create_subprocess_exec(
                    "rpm", "-q", pkg, 
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
                )
                rpm_stdout, _ = await rpm_check.communicate()
                
                # If rpm -q {pkg} returns 0, it means that EXACT version is installed
                if rpm_check.returncode == 0:
                    installed = rpm_stdout.decode('utf-8').strip()
                    await log_async(f"rpm query after dnf: {installed}", notify_callback)
                    await log_async("Package appears installed despite dnf exit code — treating as success.", notify_callback)
                    await log_rpm_query_async("forrixguard-app", None, "Current forrixguard-app version (after dnf)")
                    
                    # Cleanup after successful installation (fallback case)
                    await cleanup_dnf_cache(notify_callback)

                    # Try to schedule a restart of the service even if dnf reported a scriptlet failure
                    await log_async("Package appears installed despite dnf exit code.", notify_callback)
                    await log_async("Rebooting system to apply update...", notify_callback)
                    subprocess.Popen(
                        ["sh", "-c", "sleep 2 && reboot"], 
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )

                    update_status['last_update'] = {
                        'type': 'forrixguard_app',
                        'version': version,
                        'timestamp': datetime.now().isoformat(),
                        'status': 'completed_rebooting'
                    }

                    if notify_callback:
                        if inspect.iscoroutinefunction(notify_callback):
                            await notify_callback('STATUS:SUCCESS')
                        else:
                            notify_callback('STATUS:SUCCESS')
                        
                    return True

                # Otherwise record the failure as before
                await log_rpm_query_async("forrixguard-app", None, "Current forrixguard-app version (after failure)")
                return await fail_update("Failed to update ForrixGuard app.")
                
        except Exception as e:
            return await fail_update(f"Exception during ForrixGuard app update: {e}")


async def handle_uboot_update(notify_callback: Optional[Callable] = None, 
                              version: Optional[str] = None) -> bool:
    """
    Update U-Boot bootloader to specified version
    Downloads binary image and writes to boot partition
    """
    async with UpdateLock(_lock_path, notify_callback):
        update_status['current_type'] = 'uboot'
        update_status['current_version'] = version
        update_status['progress_messages'] = []
        
        async def fail_update(reason: str):
            """Helper to handle update failure"""
            await log_async(reason, notify_callback)
            update_status['last_update'] = {
                'type': 'uboot',
                'version': version,
                'timestamp': datetime.now().isoformat(),
                'status': 'failed',
                'error': reason
            }
            if notify_callback:
                if inspect.iscoroutinefunction(notify_callback):
                    await notify_callback('STATUS:FAILED')
                else:
                    notify_callback('STATUS:FAILED')
            return False

        try:
            if not version:
                return await fail_update("No U-Boot version provided.")
            
            await log_async(f"Starting U-Boot update to version {version}...", notify_callback)
            
            uboot_image = f"u-boot-{version}.bin"
            uboot_path = os.path.join(PATH.DOWNLOAD_DIR, uboot_image)
            uboot_url = f"{URL.UBOOT}{uboot_image}"
            
            # Remove existing image
            if os.path.exists(uboot_path):
                try:
                    os.remove(uboot_path)
                    await log_async(f"Removed existing U-Boot image {uboot_path}", notify_callback, to_ui=False)
                except Exception as e:
                    return await fail_update(f"Failed to delete existing U-Boot image: {e}")
            
            # Download - clean progress to UI
            await log_async(f"Downloading U-Boot image version {version}...", notify_callback)
            download_success = await download_file_async(uboot_url, uboot_path, notify_callback)
            
            if not download_success:
                await log_async(f"Failed to download U-Boot image from {uboot_url}.", notify_callback, to_ui=False)
                return await fail_update(f"Error downloading U-Boot image.")
            
            # Download signature - clean progress to UI
            uboot_sig_image = f"{uboot_image}.sig"
            uboot_sig_path = f"{uboot_path}.sig"
            uboot_sig_url = f"{URL.UBOOT}{uboot_sig_image}"
            
            await log_async(f"Downloading U-Boot signature version {version}...", notify_callback)
            sig_download_success = await download_file_async(uboot_sig_url, uboot_sig_path, notify_callback)
            if not sig_download_success:
                await log_async(f"Failed to download U-Boot signature from {uboot_sig_url}.", notify_callback, to_ui=False)
                return await fail_update(f"Error downloading U-Boot signature.")

            # Perform Signature Verification
            await log_async("Verifying U-Boot image signature...", notify_callback)
            if not os.path.exists(PATH.SWUPDATE_PUBKEY):
                await log_async(f"Verification failed: Public key {PATH.SWUPDATE_PUBKEY} not found.", notify_callback)
                return await fail_update("Public key missing for U-Boot verification")

            verify_cmd = [
                "openssl", "dgst", "-sha256", 
                "-verify", PATH.SWUPDATE_PUBKEY, 
                "-signature", uboot_sig_path, 
                uboot_path
            ]
            
            verify_res = await run_cmd_stream_async(
                verify_cmd,
                notify_callback,
                log_cmd_ui=False,
                log_output_ui=False
            )
            
            if verify_res.returncode != 0:
                await log_async("U-Boot signature verification FAILED!", notify_callback)
                await log_async(f"Verification details: {verify_res.stdout}", notify_callback, to_ui=False)
                return await fail_update("U-Boot signature verification failed (Integrity/Authenticity check)")
            
            await log_async("U-Boot signature verified successfully.", notify_callback)
            
            await log_async("Download completed. Updating U-Boot...", notify_callback)
            
            # Backup current rootfs_active value - quiet to UI
            await log_async("Backing up current rootfs_active value...", notify_callback, to_ui=False)
            saved_rootfs_active = None
            
            try:
                result = subprocess.run(["fw_printenv", "rootfs_active"], capture_output=True, text=True)
                if result.returncode == 0 and "=" in result.stdout:
                    saved_rootfs_active = result.stdout.strip().split("=", 1)[1]
                    await log_async(f"Current rootfs_active: {saved_rootfs_active}", notify_callback, to_ui=False)
            except Exception as e:
                await log_async(f"Failed to backup rootfs_active: {e}", notify_callback, to_ui=False)
            
            # Set mmcblk0boot0 to writable
            try:
                with open(HW.FORCE_RO_PATH, 'w') as f:
                    f.write('0')
            except Exception as e:
                    return await fail_update(f"Failed to set device to writable: {e}")
            
            # Write U-Boot image - quiet to UI
            dd_cmd = ["dd", f"if={uboot_path}", f"of={HW.MMC_BOOT0}", "bs=512", "seek=0"]
            dd_result = await run_cmd_stream_async(
                dd_cmd, 
                notify_callback,
                log_cmd_ui=False,
                log_output_ui=False
            )
            
            returncode = dd_result.returncode
            
            if returncode != 0:
                await log_async(f"dd failed with return code {returncode}", notify_callback)
                # Set back to read-only
                try:
                    with open(HW.FORCE_RO_PATH, 'w') as f:
                        f.write('1')
                except Exception:
                    pass
                return await fail_update("dd write failed")
            
            # Restore rootfs_active value - quiet to UI
            if saved_rootfs_active:
                await log_async(f"Restoring rootfs_active to: {saved_rootfs_active}", notify_callback, to_ui=False)
                try:
                    subprocess.run(["fw_setenv", "rootfs_active", saved_rootfs_active], check=True)
                    await log_async("Successfully restored rootfs_active value", notify_callback, to_ui=False)
                except Exception as e:
                    await log_async(f"Failed to restore rootfs_active: {e}", notify_callback, to_ui=False)
            
            # Set back to read-only
            try:
                with open(HW.FORCE_RO_PATH, 'w') as f:
                    f.write('1')
            except Exception:
                pass
            
            await log_async("U-Boot update completed successfully.", notify_callback)
            await log_async("Rebooting system to apply U-Boot update...", notify_callback)
            
            update_status['last_update'] = {
                'type': 'uboot',
                'version': version,
                'timestamp': datetime.now().isoformat(),
                'status': 'completed_rebooting'
            }
            
            # Schedule reboot
            subprocess.Popen(["sh", "-c", "sleep 2 && reboot"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            # Send final success message
            if notify_callback:
                if inspect.iscoroutinefunction(notify_callback):
                    await notify_callback('STATUS:SUCCESS')
                else:
                    notify_callback('STATUS:SUCCESS')
            
            return True
            
        except Exception as e:
            # Set read-only just in case
            try:
                with open(HW.FORCE_RO_PATH, 'w') as f:
                    f.write('1')
            except Exception:
                pass
            return await fail_update(f"Exception during U-Boot update: {e}")


def get_update_status() -> dict:
    """Get current update status for API response"""
    return {
        'in_progress': update_status['in_progress'],
        'current_type': update_status['current_type'],
        'current_version': update_status['current_version'],
        'progress_messages': update_status['progress_messages'][-20:],  # Last 20 messages
        'last_update': update_status['last_update']
    }


if __name__ == '__main__':
    # Test callback function
    def test_callback(msg):
        print(f"[UPDATE] {msg}")
    
    print("Firmware Update Module loaded successfully")
    print(f"Update Status: {get_update_status()}")
