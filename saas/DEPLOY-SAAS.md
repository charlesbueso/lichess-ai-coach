# SaaS Deployment & Account Setup — TO THE MOON 🚀

This is your end-to-end runbook to ship the Discord SaaS to production today.
Order matters: complete each section before moving on.

> The SaaS layer is open source and lives on `main` under `saas/`. Anyone can
> self-host; the moat is the hosted convenience + the Stripe/Discord credentials,
> not the code.

---

## 0. Prerequisites checklist (you, the operator)

Have these tabs open / installed:
- [ ] DigitalOcean account with a payment method ([digitalocean.com](https://www.digitalocean.com/))
- [ ] Domain registrar account (Cloudflare Registrar, Namecheap, Porkbun…)
- [ ] Stripe account (live mode access — needs business details)
- [ ] Discord account
- [ ] Groq account ([console.groq.com](https://console.groq.com/))
- [ ] Sentry account (free tier) ([sentry.io](https://sentry.io/))
- [ ] PostHog Cloud account (free tier) ([app.posthog.com](https://app.posthog.com/))
- [ ] Local SSH key (`~/.ssh/id_ed25519` or similar)
- [ ] `git` configured locally with this repo

---

## 1. Repo layout (already done ✓)

The SaaS code lives on `main` under `saas/`. The OSS single-tenant entry point
(`main.py`, `storage.py`, `config.py`) stays at the repo root for self-hosters.

```bash
git checkout main
git pull
```

> Optional: protect `main` on GitHub (Settings → Branches) to prevent force
> pushes / deletions. For a solo OSS project, allowing force pushes is fine.

---

## 2. Buy a domain

**Recommended:** Cloudflare Registrar (at-cost pricing, free DNS).

1. [Cloudflare Dashboard → Domains → Register Domain](https://dash.cloudflare.com/?to=/:account/registrar/register).
2. Search for one of:
   - `chesscoach.gg` (~$70/yr — premium TLD but credible)
   - `lichesscoach.app` (~$15/yr)
   - `coachpawn.com` (~$10/yr)
3. Complete the purchase. DNS propagates within minutes on Cloudflare.

> Pick the name now; you'll point it at the droplet in step 4.

---

## 3. Provision the DigitalOcean droplet

1. **Create → Droplets**.
2. **Choose an image:** Ubuntu 24.04 (LTS) x64.
3. **Choose a plan:** Basic → Regular SSD → **$6/mo (1 GB / 1 vCPU / 25 GB)**.
4. **Datacenter region:** pick the closest to your users (e.g. NYC3, FRA1, SFO3).
5. **Authentication:** SSH key — paste your public key. Disable password login.
6. **Hostname:** `coach-prod`.
7. **Backups:** ✓ Enable weekly snapshots (+$1.20/mo).
8. **Create.** Wait ~60 seconds; copy the public IPv4.

DNS:
- Cloudflare → your domain → **DNS** → add an `A` record:
  - Name: `@`
  - IPv4: `<your droplet IP>`
  - Proxy: **DNS only** (grey cloud) — Caddy needs to terminate TLS itself.
- Add a second `A` record `Name: www` → same IP (optional).

Test:
```powershell
ssh root@coach-prod.<your-domain>
# Trust the host key, you should land in Ubuntu.
```

---

## 4. Run the provisioning script

On the droplet (as root):

```bash
# 1. Pull just the provision script first (the rest comes via git in the script).
curl -fsSL -o provision.sh \
    https://raw.githubusercontent.com/<your-gh>/lichess-ai-coach/main/saas/deploy/provision.sh

# 2. Run it. DOMAIN must match the domain you bought.
DOMAIN=chesscoach.gg \
REPO_URL=https://github.com/<your-gh>/lichess-ai-coach.git \
BRANCH=main \
bash provision.sh
```

> Public repo → HTTPS clone works without keys. If you ever switch to a private
> fork, use a deploy key:
> ```bash
> ssh-keygen -t ed25519 -f ~/.ssh/coach_deploy -N ''
> cat ~/.ssh/coach_deploy.pub   # paste into GitHub → Repo Settings → Deploy keys
> # Then re-run provision.sh with REPO_URL=git@github.com:<you>/lichess-ai-coach.git
> ```

The script:
- Installs Python, Postgres 16, Caddy, ufw, fonts.
- Creates `coach` system user, clones the repo to `/opt/lichess-ai-coach`.
- Creates DB role `coach`, DB `coach`, applies the schema.
- Writes `/etc/coach.env` with placeholders + a generated DB password and SESSION_SECRET.
- Installs systemd unit + Caddyfile + nightly `pg_dump` cron.

**Write down the printed DB password.**

---

## 5. Set up the SaaS accounts (parallelisable)

You'll fill `/etc/coach.env` with values from these accounts. Open them in tabs.

### 5a. Discord application

1. [Discord Developer Portal](https://discord.com/developers/applications) → **New Application**. Name: `Lichess Coach`.
2. **General Information**:
   - App icon: upload a chess-y SVG/PNG.
   - **Terms of Service URL**: `https://chesscoach.gg/terms`
   - **Privacy Policy URL**: `https://chesscoach.gg/privacy`
3. **OAuth2 → General**:
   - **Redirects** → add `https://chesscoach.gg/discord/callback`. Save.
4. **Bot**:
   - **Reset Token** → copy the token. Save it for `DISCORD_TOKEN`.
   - **Privileged Gateway Intents**: leave **all OFF** (we don't need Message Content).
   - **Public Bot**: leave ON for now (we'll switch to "code grant required" once approved at 100 guilds).
5. Back to **OAuth2 → General**:
   - **Client ID** → copy → `DISCORD_CLIENT_ID`.
   - **Client Secret → Reset Secret** → copy → `DISCORD_CLIENT_SECRET`.

### 5b. Stripe

1. [Stripe Dashboard](https://dashboard.stripe.com/) → finish business activation. Use **live mode**.
2. **Products → Add product**:
   - Name: `Lichess Coach`.
   - Pricing: **Recurring**, **$5.00 USD** monthly. **Save product**.
   - Copy the **Price ID** (`price_...`) → `STRIPE_PRICE_ID`.
3. **Developers → API keys** → **Reveal live secret key** → copy → `STRIPE_SECRET_KEY`.
4. **Developers → Webhooks → Add endpoint**:
   - URL: `https://chesscoach.gg/stripe/webhook`
   - Events to send (Select events):
     - `checkout.session.completed`
     - `customer.subscription.created`
     - `customer.subscription.updated`
     - `customer.subscription.deleted`
     - `customer.subscription.trial_will_end`
     - `invoice.payment_failed`
   - **Add endpoint** → copy **Signing secret** (`whsec_...`) → `STRIPE_WEBHOOK_SECRET`.
5. **Settings → Customer Portal**:
   - Enable: cancel subscriptions, update card, download invoices.
   - **Save**.

### 5c. Groq

1. [Groq Console](https://console.groq.com/keys) → **Create API Key** → copy → `GROQ_API_KEY`.

### 5d. Sentry

1. [sentry.io](https://sentry.io/) → create project → Platform: **Python**. Name: `coach-prod`.
2. Copy the **DSN** → `SENTRY_DSN`.

### 5e. PostHog Cloud

1. [app.posthog.com](https://app.posthog.com/) → create project → name: `coach`.
2. **Project settings → API keys** → copy the **Project API key** → `POSTHOG_KEY`.
3. (US cloud is the default; matches `POSTHOG_HOST` in `.env`.)

---

## 6. Fill `/etc/coach.env` and start

On the droplet:

```bash
sudo nano /etc/coach.env
# Paste in the values from step 5. Save (Ctrl-O, Ctrl-X).

sudo systemctl start lichess-coach-saas
sudo systemctl status lichess-coach-saas
sudo journalctl -u lichess-coach-saas -f
```

Expected log lines:
```
INFO coach.db: Postgres pool initialised
INFO coach.bot: Synced 8 global slash commands
INFO coach.bot: Logged in as Lichess Coach#1234 (id=...)
INFO uvicorn.error: Uvicorn running on http://127.0.0.1:8000
INFO coach.main: Poll cycle: 0 active tenants
```

Browser test:
- `https://chesscoach.gg/` → landing page renders, TLS green.
- `https://chesscoach.gg/healthz` → `{"ok": true}`.

If TLS fails, Caddy logs are at `/var/log/caddy/access.log` and `journalctl -u caddy`. Most common cause: DNS not propagated yet — wait 5 min and `systemctl reload caddy`.

---

## 7. End-to-end smoke test (Stripe TEST mode first)

1. Switch the droplet env to Stripe **test mode** keys for the smoke test:
   - In Stripe, toggle **Test mode** (top right).
   - Re-do the steps in 5b.2–5b.4 in test mode → get test `sk_test_...`, `whsec_...`, `price_...`.
   - Edit `/etc/coach.env` → swap to test keys → `sudo systemctl restart lichess-coach-saas`.
2. From your browser:
   - Visit `https://chesscoach.gg/` → click **Start 7-day free trial**.
   - Stripe Checkout → use test card `4242 4242 4242 4242`, any future date, any CVC.
   - Land on the `/connect` page → click **Add to Discord**.
   - Discord asks which server → pick a test server you own → **Authorize**.
   - You should land on `/success` and receive a DM from the bot.
3. In your test server, run `/setup lichess:<your_username> channel:#some-channel`.
4. Play a quick Lichess game (any time control, even casual).
5. Within ~10 minutes (or whatever `POLL_INTERVAL_MINUTES` is) you should see the analysis post.

If anything fails: `journalctl -u lichess-coach-saas -f` is your best friend.

---

## 8. Switch to live mode

1. Swap `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_ID` back to **live** values.
2. `sudo systemctl restart lichess-coach-saas`.
3. Make a real $5 purchase from a different card to confirm everything works on the production keys.
4. Cancel the subscription via `/billing` to avoid charging yourself; verify the bot stops posting on the next poll cycle.

---

## 9. Lichess ToS courtesy email (do this BEFORE marketing)

Lichess is fine with commercial usage of their public API at our scale, but
they appreciate a heads-up.

- Email `contact@lichess.org`:
  > Subject: Commercial usage notice — lichess-ai-coach
  >
  > Hi Lichess team,
  >
  > I'm running a small commercial Discord bot (`Lichess Coach`) that pulls
  > each subscriber's public games via `/api/games/user/{username}` once every
  > 10 minutes. Expected steady-state: well under 1 req/s globally.
  > User-Agent: `lichess-ai-coach/1.0 (+https://chesscoach.gg; you@chesscoach.gg)`.
  >
  > Please let me know if you'd like me to throttle further or if there's
  > anything else I should be aware of.
  >
  > Thanks for the platform. ♞

---

## 10. Day-2 ops

```bash
# Status / logs
sudo systemctl status lichess-coach-saas
sudo journalctl -u lichess-coach-saas -f
sudo journalctl -u lichess-coach-saas --since "1 hour ago"

# Restart after config / code changes
sudo systemctl restart lichess-coach-saas

# Update code from main branch
cd /opt/lichess-ai-coach
sudo -u coach git pull
sudo -u coach .venv/bin/pip install -r saas/requirements.txt
sudo systemctl restart lichess-coach-saas

# Apply a new SQL migration
sudo -u postgres psql -d coach -f /opt/lichess-ai-coach/saas/migrations/000X_xyz.sql

# Manual DB backup (cron runs nightly at 03:30)
sudo -u postgres /usr/local/bin/pg_backup_coach.sh

# Restore from a dump
gunzip -c /var/backups/coach/coach-2026-05-06-0330.sql.gz | sudo -u postgres psql -d coach
```

---

## 11. Pre-launch sanity checklist

- [ ] `https://chesscoach.gg` loads with valid TLS
- [ ] `/privacy` and `/terms` render
- [ ] Stripe live webhook returns 200 (Stripe Dashboard → Webhooks → recent deliveries)
- [ ] Test purchase end-to-end: card → install → `/setup` → game posts
- [ ] `/billing` opens the customer portal
- [ ] `/ask` returns within 30 s and stays under cap (try 11 times → 11th is rejected)
- [ ] Sentry captures a forced exception: `curl https://chesscoach.gg/__force_500` (won't exist; pick any 404 → check Sentry)
- [ ] PostHog shows a `checkout_started` event after clicking the CTA
- [ ] `journalctl` has no red errors during a 30-minute idle window
- [ ] `pg_dump` cron produced today's dump in `/var/backups/coach/`
- [ ] DigitalOcean weekly snapshot is enabled

LFG. 🚀
