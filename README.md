# Discord Sound Bot

Plays a per-user assigned sound when someone joins a voice channel or moves
between voice channels. Sounds and user assignments are managed through a
small built-in web dashboard.

## 1. Create the Discord bot application

1. Go to https://discord.com/developers/applications and click **New Application**.
2. Under **Bot**, click **Reset Token** / **Copy** to get your bot token — save it, you'll need it below as `DISCORD_TOKEN`.
3. On the same Bot page, turn on **Server Members Intent** under "Privileged Gateway Intents". (Voice states is on by default and doesn't need a toggle.)
4. Under **OAuth2 → URL Generator**, check scope `bot`, then under Bot Permissions check **View Channels**, **Connect**, and **Speak**. Copy the generated URL and open it to invite the bot to your server(s).

## 2. Environment variables

| Variable | Description |
|---|---|
| `DISCORD_TOKEN` | Bot token from step 1 |
| `WEB_USERNAME` | Login username for the web dashboard |
| `WEB_PASSWORD` | Login password for the web dashboard |
| `SESSION_SECRET` | Any long random string, used to sign login sessions |
| `COOLDOWN_SECONDS` | Per-user cooldown to ignore duplicate join/move events (default `3`) |
| `IDLE_DISCONNECT_SECONDS` | How long to stay connected after the last sound before leaving the channel (default `10`) |
| `WEB_PORT` | Port the dashboard listens on inside the container (default `8080`) |

## 3. Unraid container setup

- **Repository**: `ghcr.io/<your-github-username>/<repo-name>:latest` (lowercase)
- **Port**: map container `8080` to whatever host port you want, or leave it internal-only and reach it through Nginx Proxy Manager
- **Volumes**:
  - `/app/sounds` → an appdata path, e.g. `/mnt/user/appdata/discord-soundbot/sounds`
  - `/app/config` → e.g. `/mnt/user/appdata/discord-soundbot/config`
- **Environment variables**: all of the ones listed above

Once running, visit the dashboard (directly by IP:port, or via an NPM proxy entry) and log in with `WEB_USERNAME`/`WEB_PASSWORD` to upload mp3s and assign them to users.

## 4. Updating

Push a change to `main` on GitHub → the included Actions workflow rebuilds and pushes a new image to GHCR automatically → Unraid's Docker tab will show an update available on the container → click **Apply Update**.

## Local development (optional)

```bash
cp .env.example .env
# fill in .env, then:
pip install -r requirements.txt
export $(cat .env | xargs)
python -m bot.main
```
