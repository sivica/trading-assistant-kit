# SETUP: step-by-step (for you or your AI agent)

> Agent instructions: follow these steps in order. Stop and ask the human before anything
> that costs money, moves funds, or places an order. Never print a private key. Never ask for
> the human's main wallet key or seed phrase. Placeholders look like `<LIKE_THIS>`.

## 0. Before you start
- Check that Polymarket is allowed where **you and the server** are. If it is blocked, skip
  the Polymarket part. Do not use a VPN or proxy to get around it.
- Decide your budget. Suggested: $50-100 on Hyperliquid, $20-50 on Polymarket.

## 1. Rent and harden a VPS (Hetzner example)
1. Create a Hetzner Cloud account, add your **SSH public key** (`ssh-keygen -t ed25519` on your
   computer if you have none), and create the smallest shared-CPU server (e.g. CX22, Ubuntu
   24.04) in a region where Polymarket is allowed.
2. First login: `ssh root@<SERVER_PUBLIC_IP>`. Then:
   ```bash
   apt update && apt -y upgrade && apt -y install python3-venv python3-pip git unattended-upgrades
   # key-only SSH
   sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
   sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
   systemctl restart ssh
   ```
3. Install Tailscale and join your tailnet:
   ```bash
   curl -fsSL https://tailscale.com/install.sh | sh
   tailscale up --ssh=false
   tailscale ip -4        # note the 100.x address
   ```
4. Add a host alias on your own computer (`~/.ssh/config`):
   ```
   Host <YOUR_SSH_HOST_ALIAS>
     HostName <SERVER_TAILSCALE_IP_OR_MAGICDNS_NAME>
     User root
   ```
   Test `ssh <YOUR_SSH_HOST_ALIAS>` over Tailscale **before** the next step.
5. Close public SSH: in the Hetzner Cloud **Firewall**, allow no inbound ports (Tailscale works
   outbound). Optionally also `ufw default deny incoming; ufw allow in on tailscale0; ufw enable`.
   Keep the Hetzner web console as an emergency way in.

## 2. Copy this repo to the server
```bash
git clone <THIS_REPO_URL> /root/kit     # or scp the folder
```

## 3. Polymarket stop-loss bot (`pmbot`)

### 3.1 Install
```bash
useradd --system --create-home --shell /usr/sbin/nologin pmbot
cp -r /root/kit/polymarket-stoploss /opt/pm-stoploss-bot
python3 -m venv /opt/pm-stoploss-bot/venv
/opt/pm-stoploss-bot/venv/bin/pip install -r /opt/pm-stoploss-bot/requirements.txt
install -m 755 /opt/pm-stoploss-bot/bin/pmbot /usr/local/bin/pmbot
install -d -o pmbot -g pmbot -m 700 /home/pmbot/.pmbot
install -o pmbot -g pmbot -m 600 /opt/pm-stoploss-bot/config.example.yaml /home/pmbot/.pmbot/config.yaml
cd /opt/pm-stoploss-bot && venv/bin/python -m unittest discover -s tests   # should say OK
```

### 3.2 Wallet: Deposit Wallet + pUSD
Background: since **May 2026**, new plain wallets (EOAs) can no longer trade on Polymarket
directly. New accounts trade through a **Deposit Wallet**, a smart wallet owned by a key.
Polymarket trades in **pUSD** (1:1 wrapped USDC). `pmbot setup` handles all of this.

1. `pmbot geoblock` must say trading is allowed from the server.
2. `pmbot setup` (as root; the wrapper runs it as `pmbot`). It:
   - creates a **new bot key** in `/home/pmbot/.pmbot/wallet.key` (chmod 600, never printed),
   - prints the bot key's **public address**,
   - tells you to **back up** `wallet.key` offline (it controls the Deposit Wallet funds; copy it
     with `scp` to an encrypted place, never paste it into a chat or website),
   - tells you to fund it. Send **USDC.e on Polygon** (or native USDC, then run
     `pmbot convert-usdc`) to that address. Only send what you are willing to bet.
3. Run `pmbot setup` again. It wraps USDC.e into pUSD, creates the Polymarket API
   credentials and a **Builder API key**, deploys the Deposit Wallet (gasless), moves the pUSD
   into it, and sets the one-time **trading approvals** (also gasless).
   - If it can't create the builder key itself: on polymarket.com open **Settings -> Builders**,
     create a key, then run `pmbot set-builder-key` and paste Key / Secret / Passphrase into the
     hidden prompts (they stay on the server).
   - If a buy later fails with "allowance is not enough", run `pmbot fix-approvals`.
4. `pmbot balance` and `pmbot status` should now show your pUSD.
5. Add more money later with `pmbot fund`.

Rules to know: the **minimum order is 5 shares** (at 80 cents that is $4), and `pmbot buy`
refuses bets above `max_buy_usd` (default $5).

### 3.3 Dry run, then live
```bash
cp /opt/pm-stoploss-bot/systemd/pm-stoploss.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now pm-stoploss
journalctl -u pm-stoploss -f          # watch: it should log HOLD / "would sell" lines
pmbot status                          # positions, stops, confirm timers, game times
```
Place a tiny test bet (preview first, then confirm). Note: `pmbot buy` places a **real** order
even while the service is in dry-run; dry-run only stops the automatic stop-loss sells:
```bash
echo NO  | pmbot buy <MARKET_URL> --outcome Yes --usd 4 --max-price 0.80 --stop 0.55   # preview only
echo YES | pmbot buy <MARKET_URL> --outcome Yes --usd 4 --max-price 0.80 --stop 0.55   # real order
```
After a day of sensible logs, set `dry_run: false` in `/home/pmbot/.pmbot/config.yaml` and
`systemctl restart pm-stoploss`. Per-market stop overrides go under `markets:` in the same file
(see comments in `config.example.yaml`).

Optional instant alerts: copy `bot.env.example` to `/home/pmbot/.pmbot/bot.env`
(owner pmbot, chmod 600) with your Telegram bot token and chat id.

## 4. Hyperliquid

### 4.1 Create an API (agent) wallet
1. In the Hyperliquid app: **More -> API** (app.hyperliquid.xyz/API). Enter a name, click
   **Generate**, then **Authorize API Wallet** and sign with your main wallet. Set the longest
   validity you are comfortable with and note the expiry date.
2. Copy the API wallet **private key** once, straight into a password manager. It can trade but
   **cannot withdraw**. Never share your main wallet key with anyone or any AI.
3. Your main account's **public** address is `HL_ACCOUNT_ADDRESS` everywhere in this kit.

### 4.2 Order examples (on your computer or the server)
```bash
python3 -m venv ~/hl-venv && ~/hl-venv/bin/pip install hyperliquid-python-sdk
export HL_ACCOUNT_ADDRESS=<YOUR_MAIN_HYPERLIQUID_ADDRESS>
~/hl-venv/bin/python examples/hyperliquid/hl_check.py      # read-only, no key
# order scripts need HL_API_WALLET_KEY in the environment; load it from a chmod-600 file,
# never type it into chat or shell history. Each script asks you to type YES.
```

### 4.3 Trailing-stop helper (`hltrail`)
```bash
useradd --system --create-home --shell /usr/sbin/nologin hlbot
cp -r /root/kit/hl-trailstop /opt/hltrail
python3 -m venv /opt/hltrail/venv && /opt/hltrail/venv/bin/pip install -r /opt/hltrail/requirements.txt
install -m 755 /opt/hltrail/bin/hltrail /usr/local/bin/hltrail
install -d -o hlbot -g hlbot -m 700 /home/hlbot/.hltrail
install -o hlbot -g hlbot -m 600 /opt/hltrail/config.example.yaml /home/hlbot/.hltrail/config.yaml
# edit account_address in that file (public address only)
cd /opt/hltrail && venv/bin/python test_hltrail.py          # all tests passed
cp /opt/hltrail/systemd/hl-trailstop.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now hl-trailstop
sudo -u hlbot hltrail status
```
In dry-run it needs **no key**: it reads positions from the public API and logs what it would
move to `/home/hlbot/.hltrail/actions.log`. It only trails positions that already have a
reduce-only stop, after they are `activate_pct` in profit.

To go live (after a day or two of dry-run logs you agree with):
```bash
install -o hlbot -g hlbot -m 600 /opt/hltrail/env.example /home/hlbot/.hltrail/env
# put the API wallet key in that file with an editor (not echo, which lands in shell history)
# set dry_run: false in /home/hlbot/.hltrail/config.yaml
systemctl restart hl-trailstop
```
It refuses to start live if the env file is missing or not chmod 600.

Alternative: Hyperliquid has a native trailing stop order
(<https://hyperliquid.gitbook.io/hyperliquid-docs/support/faq/trailing-stops>) that runs on the
exchange. Prefer it once your SDK supports it.

## 5. The AI side
1. Paste `ASSISTANT.md` into your agent as its system prompt; fill in the placeholders.
2. Give the agent SSH access to the server alias (over Tailscale) if it can run commands.
3. Create the read-only routines from `ROUTINES.md`.
4. Start `picks-log.csv` from `picks-log.example.csv` (it is git-ignored).

## 6. Checklist before trusting it
- [ ] `ssh` works only over Tailscale; public port 22 is closed.
- [ ] Password login disabled; key files are chmod 600 and owned by `pmbot` / `hlbot`.
- [ ] Both test suites pass on the server.
- [ ] Both services ran in dry-run for at least a day and the logs make sense.
- [ ] Only small amounts are in the Polymarket bot wallet and on Hyperliquid.
- [ ] The AI asks for Approve before every order, and routines never trade.
