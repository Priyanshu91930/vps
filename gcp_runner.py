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
import subprocess
import sys
import time
import logging
import urllib.request
from pyrogram import Client, filters
from pyrogram.types import Message

# ─────────────────────────────────────────────
#  CONFIGURATION
# ─────────────────────────────────────────────
API_ID    = int(os.environ.get("API_ID",    "27686895"))
API_HASH  = os.environ.get("API_HASH",       "0e996bd3891969ec5dfebf8bb3e39e94")
BOT_TOKEN = os.environ.get("BOT_TOKEN",      "8615130694:AAF5Y29rp3_pmtHj5dgqS4picI03Kx6Uvxo")
ADMIN     = int(os.environ.get("ADMIN",      "1246987713"))

# Path to store startup commands on the VPS
STARTUP_FILE = "startup_commands.txt"
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

# Default startup commands requested by the user
DEFAULT_STARTUP = (
    "docker rm -f anihubfilter_bot\n"
    "cd ~/anihubfilter\n"
    "git pull\n"
    "docker build -t anihubfilter .\n"
    "docker run -d --name anihubfilter_bot anihubfilter\n"
    "docker logs --tail 100 anihubfilter_bot"
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
    """Runs a single command on GCP Cloud Shell via gcloud SSH and returns output."""
    gcloud_cmd = [
        "gcloud", "cloud-shell", "ssh",
        "--authorize-session",
        f"--command={cmd}",
        "--quiet",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *gcloud_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout
            )
        except asyncio.TimeoutError:
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
    """Uploads the startup script to GCP Cloud Shell via gcloud SCP."""
    scp_cmd = [
        "gcloud", "cloud-shell", "scp",
        f"localhost:{STARTUP_FILE}",
        "cloudshell:~/startup.sh",
        "--quiet"
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *scp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await proc.wait()
        return proc.returncode == 0
    except Exception as e:
        log.error(f"Failed to upload startup script via SCP: {e}")
        return False

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

async def send_alert(app: Client, text: str):
    """Sends notification to the ADMIN."""
    try:
        await app.send_message(ADMIN, text)
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
#  TELEGRAM BOT CLIENT
# ─────────────────────────────────────────────
app = Client(
    "gcp_terminal_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN,
)

# ── /start ──────────────────────────────────
@app.on_message(filters.command("start") & filters.user(ADMIN))
async def cmd_start(_, msg: Message):
    await msg.reply_text(
        "🖥️ **GCP Cloud Shell Terminal (Owner Panel)**\n\n"
        "Just type any shell command and I'll run it on your GCP Cloud Shell.\n\n"
        "**Special Commands:**\n"
        "`/connect`  — Connect to GCP Cloud Shell\n"
        "`/status`   — Connection & status check\n"
        "`/specs`    — VPS & GCP CPU/RAM/Disk/Speed specifications\n"
        "`/bots`     — List running Python scripts on VPS and GCP\n"
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

# ── /connect ────────────────────────────────
@app.on_message(filters.command("connect") & filters.user(ADMIN))
async def cmd_connect(_, msg: Message):
    global gcp_connected
    wait_msg = await msg.reply_text("🔌 Connecting to GCP Cloud Shell...")
    alive = await check_gcp_alive()
    if alive:
        gcp_connected = True
        await wait_msg.edit_text("✅ **Connected to GCP Cloud Shell!**\nSend any command to run it on the shell.")
    else:
        gcp_connected = False
        await wait_msg.edit_text("❌ Could not connect to GCP Cloud Shell.\nMake sure `gcloud auth login` was done on this VPS.")

# ── /status ─────────────────────────────────
@app.on_message(filters.command("status") & filters.user(ADMIN))
async def cmd_status(_, msg: Message):
    global gcp_connected, startup_running
    wait_msg = await msg.reply_text("🔍 Checking status...")
    alive = await check_gcp_alive()
    gcp_connected = alive
    emoji = "✅" if alive else "❌"
    status = "Connected" if alive else "Disconnected / Timed Out"
    startup_status = "Active/Running" if startup_running else "Inactive/Stopped"
    await wait_msg.edit_text(
        f"**GCP Cloud Shell Status**\n\n"
        f"{emoji} Status: `{status}`\n"
        f"⚡ Auto-startup container state: `{startup_status}`\n"
        f"🕒 Checked at: `{time.strftime('%H:%M:%S')}`"
    )

# ── /specs ──────────────────────────────────
@app.on_message(filters.command("specs") & filters.user(ADMIN))
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

# ── /bots ───────────────────────────────────
@app.on_message(filters.command("bots") & filters.user(ADMIN))
async def cmd_bots(_, msg: Message):
    wait_msg = await msg.reply_text("🤖 Scanning active Python/Docker containers...")
    
    # Active python files on VPS
    try:
        vps_procs = subprocess.check_output("ps -ef | grep python3", shell=True, text=True)
        vps_list = [line.strip() for line in vps_procs.split('\n') if "gcp_runner.py" not in line and "grep" not in line and line.strip()]
        vps_report = "\n".join(vps_list) if vps_list else "No other active python scripts."
    except Exception as e:
        vps_report = f"Error scanning VPS: {e}"

    # Active containers/python files on GCP
    gcp_alive = await check_gcp_alive()
    if gcp_alive:
        gcp_docker = await run_on_gcp("docker ps", timeout=15)
        gcp_procs = await run_on_gcp("ps -ef | grep python3", timeout=15)
        gcp_list = [line.strip() for line in gcp_procs.split('\n') if "grep" not in line and line.strip()]
        gcp_report = "\n".join(gcp_list) if gcp_list else "No active python scripts."
    else:
        gcp_docker = "GCP Offline"
        gcp_report = "GCP Offline"

    report = (
        "🖥️ **Active VPS Python Processes:**\n"
        f"```\n{vps_report}\n```\n"
        "☁️ **GCP Active Docker Containers:**\n"
        f"```\n{gcp_docker}\n```\n"
        "☁️ **GCP Active Python Processes:**\n"
        f"```\n{gcp_report}\n```"
    )
    await wait_msg.edit_text(report)

# ── /storage ────────────────────────────────
@app.on_message(filters.command("storage") & filters.user(ADMIN))
async def cmd_storage(_, msg: Message):
    wait_msg = await msg.reply_text("📦 Fetching storage info...")
    output = await run_on_gcp("df -h", timeout=30)
    await wait_msg.edit_text(f"**💾 GCP Cloud Shell Storage:**\n```\n{output}\n```")

# ── /ls ─────────────────────────────────────
@app.on_message(filters.command("ls") & filters.user(ADMIN))
async def cmd_ls(_, msg: Message):
    wait_msg = await msg.reply_text("📂 Listing files...")
    output = await run_on_gcp("ls -lah ~/", timeout=30)
    await wait_msg.edit_text(f"**📂 GCP Home Directory:**\n```\n{output}\n```")

# ── /kill ───────────────────────────────────
@app.on_message(filters.command("kill") & filters.user(ADMIN))
async def cmd_kill(_, msg: Message):
    wait_msg = await msg.reply_text("🔴 Stopping Docker containers & killing Python scripts on GCP...")
    docker_kill = await run_on_gcp("docker stop $(docker ps -a -q) &>/dev/null; docker rm $(docker ps -a -q) &>/dev/null", timeout=30)
    pkill = await run_on_gcp("pkill -f python3; echo 'Done'", timeout=30)
    await wait_msg.edit_text(f"🔴 **Stop Result:**\nContainers stopped.\nPython processes killed:\n```\n{pkill}\n```")

# ── /viewstartup ────────────────────────────
@app.on_message(filters.command("viewstartup") & filters.user(ADMIN))
async def cmd_viewstartup(_, msg: Message):
    cmds = load_startup_commands()
    await msg.reply_text(
        f"⚙️ **Current Auto-Startup Commands:**\n\n"
        f"```bash\n{cmds}\n```\n\n"
        f"Use `/setstartup <commands>` to edit."
    )

# ── /setstartup ─────────────────────────────
@app.on_message(filters.command("setstartup") & filters.user(ADMIN))
async def cmd_setstartup(_, msg: Message):
    # Retrieve commands from command arguments
    if len(msg.command) < 2:
        await msg.reply_text("❌ Please provide commands. Usage: `/setstartup cd ~/bot && git pull && ...`")
        return
    
    # Grab everything after the command
    new_cmds = msg.text.split(None, 1)[1]
    save_startup_commands(new_cmds)
    await msg.reply_text("✅ **Auto-Startup Commands saved successfully!**\nThese will execute automatically on reconnect/startup.")

# ── /runstartup ─────────────────────────────
@app.on_message(filters.command("runstartup") & filters.user(ADMIN))
async def cmd_runstartup(_, msg: Message):
    wait_msg = await msg.reply_text("⚡ Manually launching startup sequence on GCP...")
    # Upload the script via SCP
    uploaded = await upload_startup_script()
    if not uploaded:
        await wait_msg.edit_text("❌ Failed to upload startup script to GCP Cloud Shell.")
        return
        
    await run_on_gcp("chmod +x ~/startup.sh", timeout=15)
    
    # Run in background on GCP using nohup so it stays alive even if we disconnect!
    output = await run_on_gcp("nohup bash ~/startup.sh > ~/startup.log 2>&1 & echo 'Launched in background!'", timeout=30)
    await wait_msg.edit_text(f"✅ **Startup Launched:**\n`{output}`\nCheck log output anytime by typing `cat ~/startup.log`.")

# ── TERMINAL: Any other text = shell command ─
@app.on_message(filters.text & filters.user(ADMIN) & ~filters.command(
    ["start", "help", "connect", "status", "specs", "bots", "storage", "ls", "kill", "viewstartup", "setstartup", "runstartup"]
))
async def terminal(_, msg: Message):
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
async def startup_daemon(app: Client):
    """
    Monitors GCP connection.
    If GCP is disconnected/reconnected, it automatically wakes it up
    and executes the saved startup commands to rebuild/restart the docker bot.
    """
    global gcp_connected, startup_running
    log.info("Startup Daemon task started.")
    await asyncio.sleep(5) 

    while True:
        # Check connection state
        alive = await check_gcp_alive()
        
        if not alive:
            gcp_connected = False
            startup_running = False
            
            # Probe it once and get the actual error output to show in log
            err_check = await run_on_gcp("echo __alive__", timeout=30)
            log.info(f"GCP Offline. Connection check output/error: {err_check}")
            log.info("Attempting to wake it up...")
            
            # Executing a simple command automatically boots Cloud Shell
            await run_on_gcp("echo waking_up", timeout=90)
            
            # Let it boot up
            await asyncio.sleep(15)
            continue
        
        # Determine if we need to run startup scripts
        if alive and not startup_running:
            gcp_connected = True
            log.info("GCP Online. Executing startup commands...")
            
            # Run startup sequence
            await upload_startup_script()
            await run_on_gcp("chmod +x ~/startup.sh && nohup bash ~/startup.sh > ~/startup.log 2>&1 &", timeout=30)
            startup_running = True
        
        # Periodic check loop
        await asyncio.sleep(HEARTBEAT_INTERVAL)

# ─────────────────────────────────────────────
#  DAEMON: 1-Hour Inactivity Prevention (Keepalive)
# ─────────────────────────────────────────────
async def keepalive_daemon(app: Client):
    """
    Bypasses the 1-hour inactivity timeout of GCP Cloud Shell by
    periodically executing a dummy command to simulate continuous activity.
    """
    log.info("Keepalive Daemon task started.")
    while True:
        await asyncio.sleep(KEEPALIVE_INTERVAL)
        if gcp_connected:
            # Send small write/read command to signal user activity on VM
            log.info("Sending keep-alive ping to GCP Cloud Shell...")
            await run_on_gcp("echo keepalive > /dev/null", timeout=15)

# ─────────────────────────────────────────────
#  HEARTBEAT: Disconnect Alert Task
# ─────────────────────────────────────────────
async def heartbeat_alert_daemon(app: Client):
    """
    Background loop that detects connection status changes and immediately
    sends alert messages to the Telegram ADMIN.
    """
    global gcp_connected
    prev_state = False
    
    # Send startup message
    await send_alert(app, 
        "🤖 **GCP Terminal Bot is online on VPS!**\n"
        "Monitoring GCP Cloud Shell connection..."
    )

    while True:
        await asyncio.sleep(5) # Fast 5-second check
        
        alive = gcp_connected # get state updated by startup_daemon
        
        if alive and not prev_state:
            await send_alert(app, "✅ **GCP Cloud Shell session has been established/restored!**")
            prev_state = True
        elif not alive and prev_state:
            await send_alert(app, "❌ **GCP Cloud Shell connection dropped (12-hour reset or network drop).** Auto-reconnecting...")
            prev_state = False

# ─────────────────────────────────────────────
#  MAIN ENTRY POINT
# ─────────────────────────────────────────────
async def main():
    async with app:
        log.info("Starting background tasks...")
        # Start all tasks in background
        asyncio.create_task(startup_daemon(app))
        asyncio.create_task(keepalive_daemon(app))
        asyncio.create_task(heartbeat_alert_daemon(app))
        
        log.info("Bot is running. Waiting for Telegram messages...")
        await asyncio.get_event_loop().create_future()  # run forever

if __name__ == "__main__":
    asyncio.run(main())
