# Putting BUFFSQUAD on the Windows VPS

The site and the Discord bot run on the VPS. Cloudflare sits in front: visitors reach
`https://app.buffsquad.net` through a Cloudflare tunnel, so the VPS opens no ports.
Every minute the VPS checks GitHub; when there's a new version it tests it, switches to
it, and goes back to the old one if anything fails.

Your time: about an hour, plus waiting for DNS in part 1.

## Part 1: Cloudflare and your domain (about 15 minutes, then a wait)

1. Make a free account at <https://dash.cloudflare.com/sign-up>.
2. Click **Add a domain** (or **Add site**), type `buffsquad.net`, pick the **Free** plan.
3. Cloudflare scans your current DNS records. Click **Continue**.
4. Cloudflare shows **two nameservers** (like `ada.ns.cloudflare.com`). Keep that page open.
5. In another tab, sign in to **Squarespace** → **Domains** → **buffsquad.net** → **DNS**
   → **Domain nameservers** → **Use custom nameservers**. If it says DNSSEC is on, turn DNSSEC off first.
6. Replace the Squarespace nameservers with Cloudflare's two. Save.
7. Back in Cloudflare, click **Check nameservers**. Cloudflare emails you when the domain is
   active: usually within an hour, sometimes up to a day. Carry on with part 2 meanwhile.

The domain stays registered (and renewed) at Squarespace. Only the DNS moves to Cloudflare.

## Part 2: Discord app (about 15 minutes)

Turn on Developer Mode first: Discord → **User Settings** → **Advanced** → **Developer Mode**.
Then right-click things to **Copy ID**. Keep the values below in a note; part 4 needs them.

1. Go to <https://discord.com/developers/applications> → **New Application** → name it `BUFFSQUAD` → **Create**.
2. **OAuth2** page:
   - Copy the **Client ID** → `DISCORD_CLIENT_ID`
   - Click **Reset Secret**, copy it → `DISCORD_CLIENT_SECRET`
   - Under **Redirects**, add `https://app.buffsquad.net/auth/discord/callback` and **Save**.
3. **Bot** page:
   - Click **Reset Token**, copy it → `DISCORD_BOT_TOKEN` (shown once; never share it)
   - Turn **on** **Message Content Intent**, **Save**.
4. Invite the bot: **OAuth2** → **URL Generator** → scope **bot** → permissions
   **View Channels** and **Read Message History** → open the generated link → pick your server.
5. Copy these IDs:
   - Right-click your **server icon** → **Copy Server ID** → `DISCORD_GUILD_ID`
   - Right-click **your own name** → **Copy User ID** (and any other admins') → `ADMIN_DISCORD_IDS`, separated by commas
   - Right-click the **checkout channel** → **Copy Channel ID** (you can also add channels later in the admin panel)

## Part 3: Cloudflare tunnel (about 5 minutes, after part 1 shows the domain as active)

1. In Cloudflare, open **Zero Trust** (left menu) → **Networks** → **Tunnels** → **Create a tunnel**.
2. Pick **Cloudflared**, name it `buffsquad`, **Save tunnel**.
3. Choose **Windows**. Cloudflare shows a command ending in a long token
   (`cloudflared.exe service install eyJ...`). Copy just the token: the long `eyJ...` part.
4. **Next** → **Public hostname**:
   - Subdomain: `app` · Domain: `buffsquad.net`
   - Service type: `HTTP` · URL: `localhost:8000`
   - **Save tunnel**.

Cloudflare's menu names change now and then; you're looking for creating a tunnel and adding a public hostname.

## Part 4: Set up the VPS (about 25 minutes, mostly waiting)

1. Connect to the VPS with Remote Desktop.
2. Open the Start menu, type **PowerShell**, right-click **Windows PowerShell** → **Run as administrator**.
3. Paste these three lines (put your tunnel token from part 3 between the quotes):

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass -Force
   irm https://raw.githubusercontent.com/sub7m19/BUFFSQUAD_ACO/main/deploy/windows/setup.ps1 -OutFile $env:TEMP\buffsquad-setup.ps1
   & $env:TEMP\buffsquad-setup.ps1 -TunnelToken "PASTE-TOKEN-HERE"
   ```

   It installs Python, Git, the app, the tunnel and the scheduled tasks, then prints
   **Setup finished**. It's safe to run again if something stops it.
4. Open the settings file:

   ```powershell
   notepad C:\buffsquad\buffsquad.env
   ```

   Fill in the Discord values from part 2, save, then restart:

   ```powershell
   C:\buffsquad\bin\restart.cmd
   ```

5. **Back up the encryption key.** Copy the `BUFFSQUAD_SECRET_KEY=...` line from that file into
   a password manager. Without it, saved cards and passwords can't be read, even from backups.

## Part 5: Check it works (about 10 minutes)

1. Open `https://app.buffsquad.net/dashboard` and sign in with Discord.
2. Open `https://app.buffsquad.net/admin`. Overview should say **Bot online**.
3. **Channels** tab: add your checkout channel if you didn't put it in the settings file.
4. **Settings** tab: set your fees.
5. Post a test checkout from one of your bots and watch it appear under **Checkouts**.

## Part 6: Lock down Remote Desktop (about 10 minutes)

Remote Desktop open to the whole internet is the most common way Windows servers get broken into.

- In your VPS provider's firewall (or Windows Defender Firewall → **Inbound Rules** →
  **Remote Desktop**), allow port 3389 **only from your home IP** (find it at <https://ifconfig.me>).
- Use a long, unique password for the Windows administrator account.
- Leave Windows Update on.

## Day to day

| Task | How |
|---|---|
| Deploy a change | Push to `main` on GitHub. Live within about a minute |
| Restart after editing settings | `C:\buffsquad\bin\restart.cmd` |
| See what happened with deploys | `C:\buffsquad\logs\deploy.log` |
| App and bot log | `C:\buffsquad\logs\app.log` |
| Why a deploy was skipped | `C:\buffsquad\logs\last-tests.log` |
| Is it up, which version | `https://app.buffsquad.net/api/health` |
| Backups | `C:\buffsquad\data\backups` (nightly at 4:00 AM, newest 14 kept) |

A version whose tests fail, or that doesn't start, is skipped and the site stays on the
previous one. Pushing a fix deploys normally.
