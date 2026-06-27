# GCP Cloud Shell Telegram Terminal Bot

Control your **Google Cloud Shell** like a terminal — directly from Telegram!

---

## How it works
1. You run `gcp_runner.py` on your **AWS VPS**.
2. The bot connects to Telegram.
3. You send any shell command in the Telegram chat → it runs on **GCP Cloud Shell** → output comes back to you.

---

## Setup on AWS VPS

### Step 1: Install dependencies
```bash
pip install pyrogram tgcrypto
```

### Step 2: Install Google Cloud SDK
```bash
sudo apt-get update
sudo apt-get install apt-transport-https ca-certificates gnupg curl -y
curl https://packages.cloud.google.com/apt/doc/apt-key.gpg | sudo gpg --dearmor -o /usr/share/keyrings/cloud.google.gpg
echo "deb [signed-by=/usr/share/keyrings/cloud.google.gpg] https://packages.cloud.google.com/apt cloud-sdk main" | sudo tee /etc/apt/sources.list.d/google-cloud-sdk.list
sudo apt-get update && sudo apt-get install google-cloud-cli -y
```

### Step 3: Authenticate gcloud (do this ONCE)
```bash
gcloud auth login --no-launch-browser
```
Paste the generated URL in your browser, login with your Google account, copy the code back.

### Step 4: Upload `gcp_runner.py` to VPS and run
```bash
python3 gcp_runner.py
```

> **Tip:** To keep it running even after you close SSH, use `screen` or `tmux`:
> ```bash
> screen -S gcpbot
> python3 gcp_runner.py
> # Press Ctrl+A then D to detach
> ```

---

## Bot Commands

| Command | What it does |
|---|---|
| `/start` | Welcome + command list |
| `/connect` | Check & connect to GCP Cloud Shell |
| `/status` | Show current connection status |
| `/storage` | Show disk usage (`df -h`) |
| `/ls` | List files in GCP home directory |
| `/kill` | Kill all Python processes on GCP |
| Any other text | **Runs directly as a shell command on GCP Cloud Shell** |

## Auto Alerts (sent without commands)
- ✅ GCP Connected
- ❌ GCP Disconnected / Timed Out
- ⚠️ Storage > 80% Warning (checked every 5 minutes)
