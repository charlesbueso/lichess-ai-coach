# Deployment Guide — Raspberry Pi Zero 2 W (24/7)

End-to-end guide to run the Lichess AI Coach on a fresh Raspberry Pi Zero 2 W with auto-recovery from power and Wi-Fi outages, plus Tailscale for remote SSH from anywhere.

> **You play chess on any device → Lichess records the game → the Pi polls Lichess every ~10 min → Groq analyzes → Discord post appears on your phone.** No port forwarding, no public IP, no reverse proxy. Discord on your phone is the "anywhere" interface.

---

## 0. What you need

**Hardware**
- Raspberry Pi Zero 2 W
- microSD card (16 GB+, Class 10 / A1)
- microSD reader for your PC
- **Quality 5V / 2.5A USB power supply** (cheap chargers cause brownouts → SQLite corruption)
- Wi-Fi network the Pi can join

**Accounts / secrets** (have these ready before starting)
- Lichess username (and optionally an API token)
- Groq API key — https://console.groq.com/keys
- Discord bot token + target channel ID — see [README.md](README.md) "Discord bot" section
- A **Tailscale** account (free) — https://login.tailscale.com/start

**Software on your PC**
- [Raspberry Pi Imager](https://www.raspberrypi.com/software/)
- An SSH client (Windows 10/11 has `ssh` built-in via PowerShell)

---

## 1. Flash Raspberry Pi OS Lite (headless, no monitor needed)

1. Insert the microSD into your PC.
2. Open **Raspberry Pi Imager**.
3. **Choose Device** → `Raspberry Pi Zero 2 W`.
4. **Choose OS** → `Raspberry Pi OS (other)` → **Raspberry Pi OS Lite (64-bit)**.
5. **Choose Storage** → your microSD.
6. Click **Next** → **Edit Settings** (this is the headless setup):

   **General tab**
   - Set hostname: `charles-pi`
   - Set username: `pi` (or your own)
   - Set password: choose a strong one
   - Configure wireless LAN: enter your Wi-Fi SSID + password, set country code (e.g. `US`)
   - Set locale: your timezone (e.g. `America/New_York`) and keyboard layout

   **Services tab**
   - Enable SSH → **Use password authentication** (or paste your public key if you have one)

7. **Save** → **Yes** to apply customisations → **Yes** to overwrite the card. Wait for write + verify (~5–10 min).
8. Eject the card, insert it into the Pi.
9. Plug in the power supply. The green LED should blink, then settle. **First boot takes 2–3 minutes** (it expands the filesystem and joins Wi-Fi).

---

## 2. SSH into the Pi from your PC

From PowerShell on Windows (or Terminal on macOS/Linux):

```bash
ssh -i ~/.ssh/id_charles_pi mcbooezojr@charles-pi.local
```

- First connection will ask to trust the host key → type `yes`.
- Uses your public key (no password prompt).

**If `charles-pi.local` doesn't resolve** (some networks block mDNS):
1. Open your router's admin page → DHCP client list → find `charles-pi` → note its IP (e.g. `192.168.0.131`).
2. `ssh -i ~/.ssh/id_charles_pi mcbooezojr@192.168.0.131`.

You should see a prompt like `mcbooezojr@charles-pi:~ $`.

---

## 3. Update the system

```bash
sudo apt update
sudo apt full-upgrade -y
sudo reboot
```

Wait ~1 minute, then SSH back in.

---

## 4. Install dependencies

```bash
sudo apt install -y python3-venv python3-pip git fonts-dejavu-core tzdata
```

`fonts-dejavu-core` is required by `local_gif.py` to render board GIFs.

Verify timezone (affects weekly review schedule):
```bash
timedatectl
# If wrong:  sudo timedatectl set-timezone America/New_York
```

**Bump swap to 512 MB** (insurance for Pillow rendering on a 512 MB device):
```bash
sudo sed -i 's/^CONF_SWAPSIZE=.*/CONF_SWAPSIZE=512/' /etc/dphys-swapfile
sudo systemctl restart dphys-swapfile
free -h    # confirm Swap is now ~512Mi
```

---

## 5. Install the app

```bash
cd ~
git clone https://github.com/charlesbueso/lichess-ai-coach.git lichess-ai-coach
# Or, if you don't have it on GitHub yet, copy the folder from your PC:
#   On your PC:  scp -r b:\repos\lichess-ai-coach mcbooezojr@charles-pi.local:~/lichess-ai-coach

cd ~/lichess-ai-coach
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

All wheels (`aiohttp`, `discord.py`, `Pillow`, `chess`, `python-dotenv`) install prebuilt on Bookworm 64-bit — no compiler needed.

**Configure secrets**:
```bash
cp .env.example .env
nano .env
```

Fill in: `LICHESS_USERNAME`, `GROQ_API_KEY`, `DISCORD_TOKEN`, `DISCORD_CHANNEL_ID`. Save with `Ctrl-O`, `Enter`, `Ctrl-X`.

```bash
chmod 600 .env
```

**Smoke test**:
```bash
python main.py
```

You should see `Logged in as <YourBot> (channel=...)`. In Discord, type `!help` — the bot replies. Press `Ctrl-C` to stop.

---

## 6. Run as a service (auto-start, auto-restart)

Create the systemd unit:

```bash
sudo tee /etc/systemd/system/lichess-coach.service > /dev/null <<'EOF'
[Unit]
Description=Lichess AI Coach
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=mcbooezojr
WorkingDirectory=/home/mcbooezojr/lichess-ai-coach
ExecStart=/home/mcbooezojr/lichess-ai-coach/.venv/bin/python main.py
Restart=always
RestartSec=15
Environment=PYTHONUNBUFFERED=1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF
```

> Username is `mcbooezojr` — already set correctly above.

Enable and start:
```bash
sudo systemctl daemon-reload
sudo systemctl enable --now lichess-coach
```

Check status and follow logs:
```bash
systemctl status lichess-coach
journalctl -u lichess-coach -f
```

You should see `Logged in as ...` within a few seconds. Press `Ctrl-C` to stop tailing (the service keeps running).

---

## 7. Outage handling (already automatic)

### Power outages
- `Restart=always` + `RestartSec=15` → if the process dies, systemd restarts it after 15 seconds.
- `enable` (step 6) → service auto-starts on boot. **Just plug the Pi back in after a power outage; it boots and the bot comes back online by itself in ~60–90 seconds.**
- Swap on the SD card + a clean shutdown handler protect SQLite, but **a quality 2.5A PSU is the single most important thing** — most "Pi corruption" stories are undervoltage, not software.

### Wi-Fi outages
- The poll loop in [main.py](main.py) wraps every Lichess fetch in `try/except`; failures are logged and the loop sleeps until the next cycle.
- `discord.py` auto-reconnects to Discord's gateway when the network returns.
- `Wants=network-online.target` makes systemd wait for Wi-Fi at boot.
- On Bookworm, NetworkManager auto-reconnects to the configured SSID as soon as it's reachable.

**Net effect**: lose Wi-Fi for an hour → the next poll after reconnection picks up any games played during the outage. No manual intervention.

### Optional: cap the journal size
```bash
sudo sed -i 's/^#SystemMaxUse=.*/SystemMaxUse=200M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

### Optional: unattended security updates
```bash
sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades   # answer Yes
```

---

## 8. Tailscale — SSH to the Pi from anywhere

Tailscale builds a private encrypted network between your devices (no port forwarding, no public IP). Free for personal use.

**On the Pi**:
```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up
```

The command prints a URL. Open it in any browser, sign in to your Tailscale account → the Pi joins your tailnet.

Get the Pi's tailnet name:
```bash
tailscale status
# look for the line ending in "charles-pi"
```

Optional but recommended — enable Tailscale SSH (no need to manage SSH keys):
```bash
sudo tailscale up --ssh
```

**On your laptop / phone**:
1. Install Tailscale: https://tailscale.com/download
2. Sign in to the same account.
3. From any network in the world:
   ```bash
   ssh -i ~/.ssh/id_charles_pi mcbooezojr@charles-pi
   # or with the full MagicDNS name shown by `tailscale status`
   ```

That's it — you can now SSH into the Pi from a coffee shop, a hotel, or your phone (Termius / Termux).

> **Do not** open SSH to the public internet. Tailscale replaces that.

---

## 9. Day-2 runbook

```bash
# Status
systemctl status lichess-coach

# Live logs
journalctl -u lichess-coach -f

# Recent logs
journalctl -u lichess-coach --since "1 hour ago"

# Restart
sudo systemctl restart lichess-coach

# Stop / start
sudo systemctl stop lichess-coach
sudo systemctl start lichess-coach

# Update code
cd ~/lichess-ai-coach
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart lichess-coach

# Edit secrets
nano ~/lichess-ai-coach/.env
sudo systemctl restart lichess-coach

# Backup state to your laptop (run on your laptop)
scp -i ~/.ssh/id_charles_pi mcbooezojr@charles-pi:~/lichess-ai-coach/data.db ./backups/data-$(date +%F).db

# Reset analysis history (start fresh)
sudo systemctl stop lichess-coach
rm ~/lichess-ai-coach/data.db
sudo systemctl start lichess-coach

# Reboot the Pi
sudo reboot

# Shutdown cleanly (do this before unplugging)
sudo shutdown now
```

---

## 10. Verification checklist

After step 6 you should be able to confirm all of these:

1. `systemctl is-active lichess-coach` → `active`
2. `journalctl -u lichess-coach -n 50` shows `Logged in as <Bot>`
3. `!help` in Discord returns the command list
4. Play a Lichess game (request analysis on it for best results) → within `POLL_INTERVAL_MINUTES`, a header card + opening/midgame/endgame sections appear in your Discord channel
5. `!last` in Discord re-renders the most recent game
6. `!ask <game_id> why was move 17 bad?` returns an LLM answer
7. `sudo reboot` → service auto-starts, `!help` works again within ~90 seconds
8. Unplug your Wi-Fi router for 30s → reconnect → next poll cycle succeeds (visible in logs)
9. `systemd-cgtop -n1` shows `lichess-coach.service` under ~80 MB RSS

---

## 11. Troubleshooting

**`charles-pi.local` doesn't resolve** → use the IP from your router's DHCP table.

**SSH "Permission denied"** → username/password mismatch. Re-flash and re-set in Imager, or boot with monitor + USB keyboard and run `sudo passwd pi`.

**`pip install` fails on `Pillow`** → confirm you're on **64-bit** Bookworm (`uname -m` should print `aarch64`). 32-bit Pi OS doesn't have prebuilt wheels for the Zero 2.

**Service keeps restarting** → `journalctl -u lichess-coach -n 100` to see the traceback. Most common: missing env var, wrong Discord token, or `MessageContentIntent` not enabled in the Discord developer portal.

**Discord bot online but never posts** → by design there's no backfill. Play a *new* game after first start.

**Board GIFs don't render** → `fonts-dejavu-core` not installed. `sudo apt install -y fonts-dejavu-core` and restart the service.

**Pi randomly reboots / SD card corruption** → undervoltage. Check `vcgencmd get_throttled` (anything other than `0x0` means power problems). Replace the PSU with a quality 5V/2.5A unit.

**Wi-Fi keeps dropping** → in `sudo nmtui` → activate connection → set "Automatically connect" and "Available to all users". Or move the Pi closer to the router (Zero 2 W antenna is small).

---

## 12. File map

- [main.py](main.py) — Discord bot + poller + weekly loop, all in one process
- [config.py](config.py) — env loader (`POLL_INTERVAL_MINUTES`, `WEEKLY_DAY`, `WEEKLY_HOUR`, `DB_PATH`)
- [lichess.py](lichess.py) — Lichess API client
- [llm.py](llm.py) — Groq calls
- [board.py](board.py) — PGN parsing, key-moment selection, board images
- [local_gif.py](local_gif.py) — local GIF renderer (uses `fonts-dejavu-core`)
- [storage.py](storage.py) — SQLite (`data.db`)
- [requirements.txt](requirements.txt) — pinned-ish deps
- [.env.example](.env.example) — copy to `.env` and fill in
- [README.md](README.md) — feature overview and Discord commands
