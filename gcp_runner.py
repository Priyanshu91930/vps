"""
GCP Cloud Shell Telegram Terminal Bot
--------------------------------------
Run this on your AWS VPS.
Any message you send to the Telegram bot will be executed as a shell command
directly on your GCP Cloud Shell instance. Output is returned live.

Features:
- Owner security (ADMIN-only commands and terminal)
- Automatic reconnection on 12-hour session timeout or network drops
- Startup commands auto-save/load on VPS (`startup_commands.txt`)
- Auto-run of startup commands when GCP boots/reconnects
- Keepalive daemon to prevent 1-hour inactivity shutdown
- Commands: /start, /connect, /status, /specs, /bots, /storage, /ls, /kill, /setstartup, /viewstartup, /runstartup
"""

import asyncio
import os
import signal
import subprocess
import sys
import time
import logging
import urllib.request
from pyrogram import Client, filters, idle
from pyrogram.types import Message
from pyrogram.handlers import MessageHandler

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
API_ID    = int(os.environ.get("API_ID",    "27686895"))
API_HASH  = os.environ.get("API_HASH",       "0e996bd3891969ec5dfebf8bb3e39e94")
BOT_TOKEN = os.environ.get("BOT_TOKEN",      "8615130694:AAF5Y29rp3_pmtHj5dgqS4picI03Kx6Uvxo")
ADMIN_RAW = os.environ.get("ADMIN", "1246987713")
try:
    ADMIN = int(ADMIN_RAW)
except ValueError:
    ADMIN = ADMIN_RAW

# Path to store startup commands on the VPS
STARTUP_FILE = os.path.abspath("startup_commands.txt")
# Heartbeat interval for connection check (seconds)
HEARTBEAT_INTERVAL = 30 
# Keepalive interval to prevent inactivity timeout (seconds)
KEEPALIVE_INTERVAL = 300  # 5 minutes
# Storage warning threshold (%)
STORAGE_WARN_PERCENT = 80
# Max characters per Telegram message
MAX_MSG_LEN = 4000

# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ─────────────────────────────────────────────
#  STATE
# ─────────────────────────────────────────────
gcp_connected = False   # whether GCP Cloud Shell is connected
startup_running = False # whether startup script is executing/active
app = None              # Will be initialized inside main()

# Global state for interactive auth
auth_process = None
waiting_for_auth_code = False
auth_msg_to_reply = None

# Default startup commands requested by the user
DEFAULT_STARTUP = (
    "if [ ! -d \"$HOME/anihubfilter\" ]; then git clone https://github.com/Priyanshu91930/anihubfilter.git \"$HOME/anihubfilter\"; fi && "
    "cd \"$HOME/anihubfilter\" && git pull && pip3 install --user -r requirements.txt && pkill -f \"anihubfilter\" || true && "
    "nohup python3 -c \"import os, sys; os.chdir(os.path.expanduser('~/anihubfilter')); sys.path.insert(0, os.getcwd()); import bot\" > ~/anihubfilter.log 2>&1 &"
)

# ─────────────────────────────────────────────
#  FILE PERSISTENCE LOGIC
# ─────────────────────────────────────────────
def load_startup_commands() -> str:
    if os.path.exists(STARTUP_FILE):
        try:
            with open(STARTUP_FILE, "r", encoding="utf-8") as f:
                return f.read().strip()
        except Exception as e:
            log.error(f"Error reading startup file: {e}")
    # Write default if not found
    with open(STARTUP_FILE, "w", encoding="utf-8") as f:
        f.write(DEFAULT_STARTUP)
    return DEFAULT_STARTUP

def save_startup_commands(commands: str):
    with open(STARTUP_FILE, "w", encoding="utf-8") as f:
        f.write(commands.strip())

# ─────────────────────────────────────────────
#  CORE GCP EXECUTION HELPERS
# ─────────────────────────────────────────────
async def run_on_gcp(cmd: str, timeout: int = 60) -> str:
    """Runs a command on GCP Cloud Shell via gcloud SSH, with Process Group isolation to prevent locks."""
    gcloud_cmd = [
        "gcloud", "cloud-shell", "ssh",
        "--authorize-session",
        f"--command={cmd}",
        "--quiet",
    ]
    
    # Use preexec_fn=os.setsid to assign a unique process group ID (PGID) to this execution.
    # On Windows, we omit this as os.setsid is not supported.
    kwargs = {}
    if os.name != "nt":
        kwargs["preexec_fn"] = os.setsid

    try:
        proc = await asyncio.create_subprocess_exec(
            *gcloud_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
            # Force-kill the entire process group (parent + child ssh tunnels) to release loops immediately
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            return f"⏱ Command timed out after {timeout}s."

        output = stdout.decode("utf-8", errors="replace").strip()
        error  = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0 and not output:
            return f"❌ Error (exit {proc.returncode}):\n{error}"

        return output or error or "(no output)"

    except FileNotFoundError:
        return "❌ `gcloud` not found. Please install Google Cloud SDK on this VPS."
    except Exception as e:
        return f"❌ Exception: {e}"

async def upload_startup_script() -> bool:
    """Uploads the startup script to GCP Cloud Shell via gcloud SCP, isolated under setsid group."""
    scp_cmd = [
        "gcloud", "cloud-shell", "scp",
        f"localhost:{STARTUP_FILE}",
        "cloudshell:~/startup.sh",
        "--quiet"
    ]
    
    kwargs = {}
    if os.name != "nt":
        kwargs["preexec_fn"] = os.setsid

    try:
        proc = await asyncio.create_subprocess_exec(
            *scp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **kwargs
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=30
            )
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            if proc.returncode == 0:
                log.info("[SCP] Startup script uploaded successfully to GCP.")
                return True
            else:
                log.error(f"[SCP] Failed to upload. Exit code {proc.returncode}. Out: {out} | Err: {err}")
                return False
        except asyncio.TimeoutError:
            if os.name != "nt":
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            else:
                proc.kill()
            log.error("[SCP] Command timed out after 30s.")
            return False
    except Exception as e:
        log.error(f"[SCP] Exception during upload: {e}")
        return False

async def is_gcloud_logged_in() -> bool:
    """Checks if gcloud is authenticated on the VPS."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "list", "--format=value(account)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return bool(stdout.decode().strip())
    except Exception:
        return False

async def get_gcloud_accounts() -> list[str]:
    """Gets a list of all logged-in gcloud accounts on the VPS."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "list", "--format=value(account)",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        accounts = [line.strip() for line in stdout.decode().split("\n") if line.strip()]
        return accounts
    except Exception:
        return []

async def rotate_gcloud_account() -> bool:
    """Rotates to the next logged-in gcloud account in the list."""
    accounts = await get_gcloud_accounts()
    if len(accounts) <= 1:
        return False
        
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "config", "get-value", "account",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        active = stdout.decode().strip()
        
        if active in accounts:
            current_idx = accounts.index(active)
            next_idx = (current_idx + 1) % len(accounts)
        else:
            next_idx = 0
            
        next_account = accounts[next_idx]
        log.info(f"🔄 Rotating gcloud account from {active} to {next_account}")
        
        proc_switch = await asyncio.create_subprocess_exec(
            "gcloud", "config", "set", "account", next_account,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc_switch.communicate()
        return True
    except Exception as e:
        log.error(f"Error during gcloud account rotation: {e}")
        return False

async def get_active_account() -> str:
    """Gets the currently active gcloud account email on the VPS."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "config", "get-value", "account",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        return stdout.decode().strip()
    except Exception:
        return ""


async def check_gcp_alive() -> bool:
    """Probe to check if GCP is responsive."""
    result = await run_on_gcp("echo __alive__", timeout=20)
    return "__alive__" in result

def split_output(text: str) -> list[str]:
    """Split output to fit Telegram's max message limit."""
    chunks = []
    while len(text) > MAX_MSG_LEN:
        chunks.append(text[:MAX_MSG_LEN])
        text = text[MAX_MSG_LEN:]
    if text:
        chunks.append(text)
    return chunks

async def send_alert(client: Client, text: str):
    """Sends notification to the ADMIN."""
    try:
        await client.send_message(ADMIN, text)
    except Exception as e:
        log.error(f"Failed to send alert: {e}")

# Speed check helper
def test_vps_download_speed() -> str:
    url = "https://speed.cloudflare.com/__down?bytes=5000000"
    start = time.time()
    try:
        temp_file = "/tmp/speed_test_vps.bin" if os.name != "nt" else "speed_test_vps.bin"
        urllib.request.urlretrieve(url, temp_file)
        duration = time.time() - start
        speed_mb = (5 / duration)
        return f"{speed_mb:.2f} MB/s ({speed_mb * 8:.2f} Mbps)"
    except Exception as e:
        return f"Error: {e}"
    finally:
        try:
            os.remove(temp_file)
        except:
            pass

# ─────────────────────────────────────────────
#  TELEGRAM BOT HANDLERS
# ─────────────────────────────────────────────
async def debug_log_messages(_, msg: Message):
    sender_id = msg.from_user.id if msg.from_user else "Unknown"
    log.info(f"📥 [TELEGRAM] Message received from User ID: {sender_id} | Text: {msg.text}")
    log.info(f"⚙️ [CONFIG] Configured ADMIN ID is: {ADMIN}")

async def cmd_start(_, msg: Message):
    await msg.reply_text(
        "🖥️ **GCP Cloud Shell Terminal (Owner Panel)**\n\n"
        "Just type any shell command and I'll run it on your GCP Cloud Shell.\n\n"
        "**Special Commands:**\n"
        "`/connect`  — Connect to GCP Cloud Shell\n"
        "`/status`   — Connection & status check\n"
        "`/specs`    — VPS & GCP CPU/RAM/Disk/Speed specifications\n"
        "`/bots`     — List running Python/Docker processes\n"
        "`/storage`  — Show disk usage (`df -h`)\n"
        "`/ls`       — List home directory files\n"
        "`/kill`     — Kill all Python processes on GCP\n"
        "\n"
        "**Startup Config Commands:**\n"
        "`/viewstartup` — View saved auto-startup commands\n"
        "`/setstartup <cmds>` — Set/update auto-startup commands\n"
        "`/runstartup` — Manually trigger auto-startup commands right now\n\n"
        "Everything else you type → runs directly on GCP Shell 🚀"
    )

async def cmd_connect(_, msg: Message):
    global gcp_connected
    wait_msg = await msg.reply_text("🔌 Connecting to GCP Cloud Shell...")
    alive = await check_gcp_alive()
    if alive:
        gcp_connected = True
        await wait_msg.edit_text("✅ **Connected to GCP Cloud Shell!**\nSend any command to run it on the shell.")
    else:
        gcp_connected = False
        logged_in = await is_gcloud_logged_in()
        if logged_in:
            await wait_msg.edit_text("⚡ **GCP Cloud Shell is offline or starting up.**\nGoogle is booting your Cloud Shell session. Please wait 1-2 minutes and try `/connect` again.")
        else:
            await wait_msg.edit_text("❌ **GCP Connection Failed.**\nYour login credentials on the VPS have expired. Please run `/addaccount` on this bot to login again.")

async def cmd_status(_, msg: Message):
    global gcp_connected, startup_running
    wait_msg = await msg.reply_text("🔍 Checking status...")
    alive = await check_gcp_alive()
    gcp_connected = alive
    emoji = "✅" if alive else "❌"
    status = "Connected" if alive else "Disconnected / Timed Out"
    startup_status = "Active/Running" if startup_running else "Inactive/Stopped"
    
    # Get active account and all accounts
    accounts = await get_gcloud_accounts()
    active_account = "None"
    try:
        proc = await asyncio.create_subprocess_exec(
            "gcloud", "config", "get-value", "account",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        active_account = stdout.decode().strip() or "None"
    except Exception:
        pass

    accounts_str = "\n".join([f"• `{acc}`" + (" ⭐ (Active)" if acc == active_account else "") for acc in accounts]) or "No logged-in accounts"

    await wait_msg.edit_text(
        f"**GCP Cloud Shell Status**\n\n"
        f"{emoji} Status: `{status}`\n"
        f"⚡ Auto-startup container state: `{startup_status}`\n"
        f"🕒 Checked at: `{time.strftime('%H:%M:%S')}`\n\n"
        f"👤 **GCP Accounts Pool ({len(accounts)}):**\n"
        f"{accounts_str}"
    )

async def cmd_addaccount(_, msg: Message):
    global auth_process, waiting_for_auth_code, auth_msg_to_reply
    if auth_process is not None:
        await msg.reply_text("⚠️ An authentication process is already running. Please reply with the code first, or wait.")
        return
        
    wait_msg = await msg.reply_text("🔑 Initiating Google Cloud login process...")
    try:
        auth_process = await asyncio.create_subprocess_exec(
            "gcloud", "auth", "login", "--no-launch-browser",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        
        url = None
        for _ in range(10):
            line = await auth_process.stderr.readline()
            line_str = line.decode(errors="replace").strip()
            log.info(f"[GCLOUD AUTH] {line_str}")
            if "https://" in line_str:
                for word in line_str.split():
                    if word.startswith("https://"):
                        url = word
                        break
            if url:
                break
                
        if url:
            waiting_for_auth_code = True
            auth_msg_to_reply = wait_msg
            await wait_msg.edit_text(
                "🔑 **Google Cloud Authentication**\n\n"
                "1. Click the link below and sign in with your Google account:\n"
                f"🔗 {url}\n\n"
                "2. Copy the authorization code and **reply directly to this message** with the code."
            )
        else:
            await wait_msg.edit_text("❌ Could not retrieve authentication URL. Please check VPS logs.")
            try:
                auth_process.kill()
            except:
                pass
            auth_process = None
            
    except Exception as e:
        await wait_msg.edit_text(f"❌ Exception starting auth: {e}")
        if auth_process:
            try:
                auth_process.kill()
            except:
                pass
            auth_process = None

async def cmd_specs(_, msg: Message):
    wait_msg = await msg.reply_text("📊 Gathering specifications (VPS & GCP) + running speed tests...")
    
    # VPS stats
    try:
        vps_cpus = os.cpu_count()
        vps_ram = subprocess.check_output("free -h | grep Mem", shell=True, text=True).strip()
        vps_disk = subprocess.check_output("df -h / | tail -n 1", shell=True, text=True).strip()
    except Exception:
        vps_cpus = vps_ram = vps_disk = "N/A"
    
    vps_speed = test_vps_download_speed()

    # GCP stats
    gcp_alive = await check_gcp_alive()
    if gcp_alive:
        gcp_cpus = (await run_on_gcp("nproc", timeout=15)).strip()
        gcp_ram = (await run_on_gcp("free -h | grep Mem", timeout=15)).strip()
        gcp_disk = (await run_on_gcp("df -h / | tail -n 1", timeout=15)).strip()
        
        # Test GCP download speed using curl
        gcp_speed_raw = await run_on_gcp('curl -s -o /dev/null -w "%{speed_download}" https://speed.cloudflare.com/__down?bytes=5000000', timeout=20)
        try:
            bytes_sec = float(gcp_speed_raw.strip())
            gcp_speed = f"{bytes_sec / (1024*1024):.2f} MB/s"
        except Exception:
            gcp_speed = "Error running speed test"
    else:
        gcp_cpus = gcp_ram = gcp_disk = gcp_speed = "Offline / Disconnected"

    report = (
        "🖥️ **AWS VPS Specs & Speed:**\n"
        f"• **CPUs:** `{vps_cpus}`\n"
        f"• **RAM details:** `{vps_ram}`\n"
        f"• **Disk details:** `{vps_disk}`\n"
        f"• **Download Speed:** `{vps_speed}`\n\n"
        "☁️ **GCP Cloud Shell Specs & Speed:**\n"
        f"• **CPUs:** `{gcp_cpus}`\n"
        f"• **RAM details:** `{gcp_ram}`\n"
        f"• **Disk details:** `{gcp_disk}`\n"
        f"• **Download Speed:** `{gcp_speed}`"
    )
    await wait_msg.edit_text(report)

async def cmd_bots(_, msg: Message):
    wait_msg = await msg.reply_text("🤖 Scanning active Python processes...")
    
    # Active python files on VPS
    try:
        vps_procs = subprocess.check_output("ps -ef | grep python3", shell=True, text=True)
        vps_list = [line.strip() for line in vps_procs.split('\n') if "gcp_runner.py" not in line and "grep" not in line and line.strip()]
        vps_report = "\n".join(vps_list) if vps_list else "No other active python scripts."
    except Exception as e:
        vps_report = f"Error scanning VPS: {e}"

    # Active python processes on GCP
    gcp_alive = await check_gcp_alive()
    if gcp_alive:
        gcp_procs = await run_on_gcp("ps -ef | grep python3", timeout=15)
        gcp_list = [line.strip() for line in gcp_procs.split('\n') if "grep" not in line and line.strip()]
        gcp_report = "\n".join(gcp_list) if gcp_list else "No active python scripts."
    else:
        gcp_report = "GCP Offline"

    report = (
        "🖥️ **Active VPS Python Processes:**\n"
        f"```\n{vps_report}\n```\n"
        "☁️ **GCP Active Python Processes:**\n"
        f"```\n{gcp_report}\n```"
    )
    await wait_msg.edit_text(report)

async def cmd_storage(_, msg: Message):
    wait_msg = await msg.reply_text("📦 Fetching storage info...")
    output = await run_on_gcp("df -h", timeout=30)
    await wait_msg.edit_text(f"**💾 GCP Cloud Shell Storage:**\n```\n{output}\n```")

async def cmd_ls(_, msg: Message):
    wait_msg = await msg.reply_text("📂 Listing files...")
    output = await run_on_gcp("ls -lah ~/", timeout=30)
    await wait_msg.edit_text(f"**📂 GCP Home Directory:**\n```\n{output}\n```")

async def cmd_kill(_, msg: Message):
    wait_msg = await msg.reply_text("🔴 Stopping Python bots across ALL GCP accounts in the pool...")
    
    accounts = await get_gcloud_accounts()
    if not accounts:
        await wait_msg.edit_text("❌ No logged-in accounts found in the pool.")
        return
        
    original_account = await get_active_account()
    results = []
    
    for acc in accounts:
        try:
            proc_switch = await asyncio.create_subprocess_exec(
                "gcloud", "config", "set", "account", acc,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc_switch.communicate()
            
            pkill_res = await run_on_gcp("pkill -f anihubfilter; pkill -f renamer2gb; pkill -f stealbot_bot; echo 'Done'", timeout=25)
            results.append(f"• `{acc}`: {pkill_res.strip()}")
            
            # Sleep 5 seconds to avoid SSH tunnel overlap and rate limiting
            await asyncio.sleep(5)
        except Exception as e:
            results.append(f"• `{acc}`: Failed to switch/kill ({e})")
            
    if original_account:
        try:
            proc_restore = await asyncio.create_subprocess_exec(
                "gcloud", "config", "set", "account", original_account,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            await proc_restore.communicate()
        except:
            pass
            
    report = "🔴 **Bots stopped on all GCP accounts:**\n\n" + "\n".join(results)
    await wait_msg.edit_text(report)

async def cmd_viewstartup(_, msg: Message):
    cmds = load_startup_commands()
    await msg.reply_text(
        f"⚙️ **Current Auto-Startup Commands:**\n\n"
        f"```bash\n{cmds}\n```\n\n"
        f"Use `/setstartup <commands>` to edit."
    )

async def cmd_setstartup(_, msg: Message):
    if len(msg.command) < 2:
        await msg.reply_text("❌ Please provide commands. Usage: `/setstartup cd ~/bot && git pull && ...`")
        return
    new_cmds = msg.text.split(None, 1)[1]
    save_startup_commands(new_cmds)
    await msg.reply_text("✅ **Auto-Startup Commands saved successfully!**\nThese will execute automatically on reconnect/startup.")

async def cmd_runstartup(_, msg: Message):
    wait_msg = await msg.reply_text("⚡ Manually launching startup sequence on GCP...")
    uploaded = await upload_startup_script()
    if not uploaded:
        await wait_msg.edit_text("❌ Failed to upload startup script to GCP Cloud Shell.")
        return
    await run_on_gcp("chmod +x ~/startup.sh", timeout=15)
    output = await run_on_gcp("nohup bash ~/startup.sh < /dev/null > ~/startup.log 2>&1 & echo 'Launched in background!'", timeout=30)
    await wait_msg.edit_text(f"✅ **Startup Launched:**\n`{output}`\nCheck log output anytime by typing `cat ~/startup.log`.")

async def terminal(_, msg: Message):
    global auth_process, waiting_for_auth_code, auth_msg_to_reply
    
    # Check if we are waiting for auth code
    if waiting_for_auth_code and auth_process is not None:
        code = msg.text.strip()
        status_msg = await msg.reply_text("⏳ Sending authorization code to gcloud...")
        try:
            auth_process.stdin.write(f"{code}\n".encode())
            await auth_process.stdin.drain()
            
            stdout_data, stderr_data = await auth_process.communicate()
            out = stdout_data.decode(errors="replace").strip()
            err = stderr_data.decode(errors="replace").strip()
            
            if auth_process.returncode == 0:
                accounts = await get_gcloud_accounts()
                new_acc = accounts[-1] if accounts else "Unknown Account"
                await status_msg.edit_text(f"✅ **Authentication Successful!**\nAdded account: `{new_acc}`")
            else:
                await status_msg.edit_text(f"❌ **Authentication Failed!**\nError:\n```\n{out}\n{err}\n```")
        except Exception as e:
            await status_msg.edit_text(f"❌ Exception during auth completion: {e}")
        finally:
            waiting_for_auth_code = False
            auth_process = None
            auth_msg_to_reply = None
        return

    user_cmd = msg.text.strip()
    if not user_cmd:
        return
    wait_msg = await msg.reply_text(f"⚡ Running: `{user_cmd}`")
    output = await run_on_gcp(user_cmd, timeout=120)
    chunks = split_output(output)
    if not chunks:
        await wait_msg.edit_text("✅ Command executed. (no output)")
        return
    first = f"```\n{chunks[0]}\n```"
    await wait_msg.edit_text(first)
    for chunk in chunks[1:]:
        await msg.reply_text(f"```\n{chunk}\n```")

# ─────────────────────────────────────────────
#  DAEMON: Auto-Reconnection & Startup Executor
# ─────────────────────────────────────────────
async def startup_daemon(client: Client):
    global gcp_connected, startup_running
    log.info("Startup Daemon task started.")
    await asyncio.sleep(5) 

    retry_count = 0
    while True:
        alive = await check_gcp_alive()
        if not alive:
            gcp_connected = False
            startup_running = False
            err_check = await run_on_gcp("echo __alive__", timeout=30)
            log.info(f"GCP Offline. Connection check output/error: {err_check}")
            
            quota_exceeded = any(word in err_check.lower() for word in ["limit", "exceeded", "quota"])
            
            if quota_exceeded or retry_count >= 3:
                log.warning(f"⚠️ GCP connection issue detected (quota: {quota_exceeded}, retries: {retry_count}). Attempting account rotation...")
                
                # Kill bots on the CURRENT active account before rotating!
                try:
                    await run_on_gcp("pkill -f anihubfilter; pkill -f renamer2gb; pkill -f stealbot_bot", timeout=15)
                except Exception as e:
                    log.error(f"Failed to kill bots on current account before rotating: {e}")

                rotated = await rotate_gcloud_account()
                if rotated:
                    retry_count = 0
                    await send_alert(client, "🔄 **GCP Cloud Shell Connection Failed/Quota Exceeded!**\nAutomatically switching to the next Google account in the pool...")
                    await asyncio.sleep(10)
                    continue
            
            retry_count += 1
            log.info("Attempting to wake it up...")
            await run_on_gcp("echo waking_up", timeout=90)
            await asyncio.sleep(15)
            continue
        
        retry_count = 0
        gcp_connected = True
        
        # Check if our processes are running on GCP
        running_processes = await run_on_gcp("ps -ef", timeout=20)
        anihub_running = "anihubfilter" in running_processes
        renamer_running = "renamer2gb" in running_processes
        setup_running = "pip3" in running_processes or "startup.sh" in running_processes
        
        if not (anihub_running and renamer_running):
            if setup_running:
                log.info("GCP Online. Bots are not running yet, but setup/installation (pip3/startup.sh) is currently in progress. Waiting...")
            else:
                log.info(f"GCP Online but bots are not running (anihub: {anihub_running}, renamer: {renamer_running}). Executing startup commands...")
                uploaded = await upload_startup_script()
                if uploaded:
                    await run_on_gcp("chmod +x ~/startup.sh && nohup bash ~/startup.sh < /dev/null > ~/startup.log 2>&1 &", timeout=30)
                    startup_running = True
                else:
                    log.error("Failed to upload startup script. Will retry next check.")
        else:
            # Both are already running
            startup_running = True
        
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ─────────────────────────────────────────────
#  DAEMON: 1-Hour Inactivity Prevention (Keepalive)
# ─────────────────────────────────────────────
async def keepalive_daemon(client: Client):
    log.info("Keepalive Daemon task started.")
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        if gcp_connected:
            log.info("Sending keep-alive ping to GCP Cloud Shell...")
            await run_on_gcp("echo keepalive > /dev/null", timeout=15)

# ─────────────────────────────────────────────
#  HEARTBEAT: Disconnect Alert Task
# ─────────────────────────────────────────────
async def heartbeat_alert_daemon(client: Client):
    global gcp_connected
    prev_state = False
    
    await send_alert(client, 
        "🤖 **GCP Terminal Bot is online on VPS!**\n"
        "Monitoring GCP Cloud Shell connection..."
    )

    while True:
        await asyncio.sleep(5)
        alive = gcp_connected
        if alive and not prev_state:
            await send_alert(client, "✅ **GCP Cloud Shell session has been established/restored!**")
            prev_state = True
        elif not alive and prev_state:
            await send_alert(client, "❌ **GCP Cloud Shell connection dropped (12-hour reset or network drop).** Auto-reconnecting...")
            prev_state = False

# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    global app
    load_startup_commands()

    # Initialize client dynamically within the active asyncio event loop
    app = Client(
        "gcp_terminal_bot",
        api_id=API_ID,
        api_hash=API_HASH,
        bot_token=BOT_TOKEN
    )

    # Register handlers programmatically to guarantee correct loop binding
    app.add_handler(MessageHandler(debug_log_messages), group=-1)
    
    # commands filter
    app.add_handler(MessageHandler(cmd_start, filters.command("start") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_connect, filters.command("connect") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_status, filters.command("status") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_specs, filters.command("specs") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_bots, filters.command("bots") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_storage, filters.command("storage") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_ls, filters.command("ls") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_kill, filters.command("kill") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_viewstartup, filters.command("viewstartup") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_setstartup, filters.command("setstartup") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_runstartup, filters.command("runstartup") & filters.user(ADMIN)))
    app.add_handler(MessageHandler(cmd_addaccount, filters.command("addaccount") & filters.user(ADMIN)))
    
    # terminal filter (all other text from owner)
    app.add_handler(MessageHandler(
        terminal, 
        filters.text & filters.user(ADMIN) & ~filters.command(
            ["start", "help", "connect", "status", "specs", "bots", "storage", "ls", "kill", "viewstartup", "setstartup", "runstartup", "addaccount"]
        )
    ))

    try:
        async with app:
            log.info("Clearing active webhook if any...")
            try:
                await app.delete_webhook()
                log.info("Webhook cleared successfully!")
            except Exception as e:
                log.warning(f"Could not clear webhook: {e}")

            log.info("Starting background tasks...")
            # Start all tasks in background
            asyncio.create_task(startup_daemon(app))
            asyncio.create_task(keepalive_daemon(app))
            asyncio.create_task(heartbeat_alert_daemon(app))
            
            log.info("Bot is running. Waiting for Telegram messages...")
            await idle()
    except (KeyboardInterrupt, asyncio.CancelledError):
        log.info("Stop signal received. Gracefully exiting...")
    except RuntimeError as e:
        if "attached to a different loop" not in str(e):
            raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
