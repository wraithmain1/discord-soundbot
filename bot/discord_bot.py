"""
Core Discord bot logic.

Listens for voice-state updates (join / move between channels), looks up
the triggering user's assigned sound, and plays it through a per-guild
playback queue so simultaneous events in the same server play in order
instead of colliding.

Cooldown and idle-disconnect timing live in config/settings.json so they
can be changed from the web dashboard without restarting the container.
The env vars below only supply the starting defaults the first time the
bot ever runs, before that file exists.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import discord

logger = logging.getLogger("soundbot")

CONFIG_DIR = Path(os.environ.get("CONFIG_DIR", "/app/config"))
SOUNDS_DIR = Path(os.environ.get("SOUNDS_DIR", "/app/sounds"))
SOUND_MAP_PATH = CONFIG_DIR / "sound_map.json"
SETTINGS_PATH = CONFIG_DIR / "settings.json"

DEFAULT_COOLDOWN_SECONDS = float(os.environ.get("COOLDOWN_SECONDS", "3"))
DEFAULT_IDLE_DISCONNECT_SECONDS = float(os.environ.get("IDLE_DISCONNECT_SECONDS", "10"))

# Voice connections occasionally time out transiently (network blips between
# this server and Discord's voice media nodes) even when a retry moments
# later succeeds fine. These control how hard the bot retries before giving
# up on a single play attempt. Dashboard-editable, same as the two above -
# env vars only supply the starting defaults before settings.json exists.
DEFAULT_VOICE_CONNECT_MAX_ATTEMPTS = int(os.environ.get("VOICE_CONNECT_MAX_ATTEMPTS", "3"))
DEFAULT_VOICE_CONNECT_RETRY_DELAY_SECONDS = float(
    os.environ.get("VOICE_CONNECT_RETRY_DELAY_SECONDS", "2")
)


def load_sound_map() -> dict:
    """Return {discord_user_id_str: filename} from disk, or {} if missing/corrupt."""
    if not SOUND_MAP_PATH.exists():
        return {}
    try:
        with open(SOUND_MAP_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read sound map, treating as empty")
        return {}


def save_sound_map(mapping: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SOUND_MAP_PATH, "w") as f:
        json.dump(mapping, f, indent=2)


def load_settings() -> dict:
    """Return the current dashboard-editable timing/retry settings.

    Falls back to the env-var defaults if settings.json doesn't exist yet
    or can't be parsed.
    """
    defaults = {
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "idle_disconnect_seconds": DEFAULT_IDLE_DISCONNECT_SECONDS,
        "voice_connect_max_attempts": DEFAULT_VOICE_CONNECT_MAX_ATTEMPTS,
        "voice_connect_retry_delay_seconds": DEFAULT_VOICE_CONNECT_RETRY_DELAY_SECONDS,
    }
    if not SETTINGS_PATH.exists():
        return defaults
    try:
        with open(SETTINGS_PATH, "r") as f:
            data = json.load(f)
        return {
            "cooldown_seconds": float(data.get("cooldown_seconds", defaults["cooldown_seconds"])),
            "idle_disconnect_seconds": float(
                data.get("idle_disconnect_seconds", defaults["idle_disconnect_seconds"])
            ),
            "voice_connect_max_attempts": int(
                data.get("voice_connect_max_attempts", defaults["voice_connect_max_attempts"])
            ),
            "voice_connect_retry_delay_seconds": float(
                data.get(
                    "voice_connect_retry_delay_seconds",
                    defaults["voice_connect_retry_delay_seconds"],
                )
            ),
        }
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        logger.exception("Failed to read settings, using defaults")
        return defaults


def save_settings(
    cooldown_seconds: float,
    idle_disconnect_seconds: float,
    voice_connect_max_attempts: int,
    voice_connect_retry_delay_seconds: float,
) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(
            {
                "cooldown_seconds": cooldown_seconds,
                "idle_disconnect_seconds": idle_disconnect_seconds,
                "voice_connect_max_attempts": voice_connect_max_attempts,
                "voice_connect_retry_delay_seconds": voice_connect_retry_delay_seconds,
            },
            f,
            indent=2,
        )


class SoundBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._last_triggered: dict[int, float] = {}
        self._guild_queues: dict[int, asyncio.Queue] = {}
        self._guild_tasks: dict[int, asyncio.Task] = {}
        self._idle_tasks: dict[int, asyncio.Task] = {}

    async def on_ready(self):
        logger.info("Logged in as %s (id: %s)", self.user, self.user.id)
        logger.info("Connected to %d guild(s)", len(self.guilds))

    def get_member_list(self) -> list[dict]:
        """Used by the web UI to populate the user-assignment dropdown."""
        members: dict[str, str] = {}
        for guild in self.guilds:
            for member in guild.members:
                if member.bot:
                    continue
                members[str(member.id)] = f"{member.display_name} ({member.name})"
        return [
            {"id": uid, "label": label}
            for uid, label in sorted(members.items(), key=lambda x: x[1].lower())
        ]

    def get_voice_channels(self) -> list[dict]:
        """Used by the web UI to populate the manual 'play in Discord' dropdown."""
        channels = []
        for guild in self.guilds:
            for channel in guild.voice_channels:
                channels.append({"id": str(channel.id), "label": f"{guild.name} / {channel.name}"})
        return sorted(channels, key=lambda c: c["label"].lower())

    async def play_now(self, channel_id: int, sound_path: Path) -> bool:
        """Manually queue a sound in a specific voice channel, bypassing cooldown
        (this is an explicit user action from the dashboard, not a voice-state event).
        Returns False if the channel id doesn't resolve to a real voice channel.
        """
        channel = self.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            return False
        await self._enqueue(channel, sound_path)
        return True


    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if member.bot:
            return

        joined = before.channel is None and after.channel is not None
        moved = (
            before.channel is not None
            and after.channel is not None
            and before.channel.id != after.channel.id
        )

        if not (joined or moved):
            return  # leaving a channel entirely - not handled per spec

        settings = load_settings()
        now = time.monotonic()
        last = self._last_triggered.get(member.id, 0)
        if now - last < settings["cooldown_seconds"]:
            logger.debug("Cooldown active for %s, skipping duplicate event", member)
            return
        self._last_triggered[member.id] = now

        sound_map = load_sound_map()
        filename = sound_map.get(str(member.id))
        if not filename:
            return  # no sound assigned to this user

        sound_path = SOUNDS_DIR / filename
        if not sound_path.exists():
            logger.warning("Assigned sound file missing for %s: %s", member, sound_path)
            return

        await self._enqueue(after.channel, sound_path)

    async def _enqueue(self, channel: discord.VoiceChannel, sound_path: Path):
        guild_id = channel.guild.id
        if guild_id not in self._guild_queues:
            self._guild_queues[guild_id] = asyncio.Queue()
            self._guild_tasks[guild_id] = asyncio.create_task(self._guild_worker(guild_id))
        await self._guild_queues[guild_id].put((channel, sound_path))

    async def _guild_worker(self, guild_id: int):
        """One worker per guild - plays sounds for that guild strictly in order."""
        queue = self._guild_queues[guild_id]
        while True:
            channel, sound_path = await queue.get()

            idle_task = self._idle_tasks.pop(guild_id, None)
            if idle_task:
                idle_task.cancel()

            try:
                await self._play_in_channel(channel, sound_path)
            except Exception:
                logger.exception("Error playing sound in guild %s", guild_id)
            finally:
                queue.task_done()

            if queue.empty():
                self._idle_tasks[guild_id] = asyncio.create_task(self._idle_disconnect(guild_id))

    async def _connect_with_retry(
        self, channel: discord.VoiceChannel, guild: discord.Guild
    ) -> discord.VoiceClient:
        """Connects to a voice channel, retrying a couple of times on timeout
        before giving up. We've seen the exact same channel/guild time out on
        one attempt and connect cleanly seconds later, which points at a
        transient network blip rather than a real per-channel problem - so a
        short retry is worth it before treating it as a real failure.
        """
        settings = load_settings()
        max_attempts = max(1, int(settings["voice_connect_max_attempts"]))
        retry_delay = max(0.0, settings["voice_connect_retry_delay_seconds"])

        last_error: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                return await channel.connect()
            except (asyncio.TimeoutError, TimeoutError) as e:
                last_error = e
                logger.warning(
                    "Voice connect attempt %d/%d timed out for channel %s (guild %s)",
                    attempt, max_attempts, channel.id, guild.id,
                )
                # A failed handshake can still leave a half-open voice client
                # behind - clear it out before trying again, or the retry
                # will just fail immediately thinking we're already connected.
                if guild.voice_client is not None:
                    try:
                        await guild.voice_client.disconnect(force=True)
                    except Exception:
                        logger.exception("Error cleaning up half-open voice client before retry")
                if attempt < max_attempts:
                    await asyncio.sleep(retry_delay)
        assert last_error is not None
        raise last_error

    async def _play_in_channel(self, channel: discord.VoiceChannel, sound_path: Path):
        guild = channel.guild
        voice_client = guild.voice_client

        if voice_client is None:
            voice_client = await self._connect_with_retry(channel, guild)
        elif voice_client.channel.id != channel.id:
            try:
                await voice_client.move_to(channel)
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning(
                    "Voice move_to timed out for channel %s (guild %s), retrying via fresh connect",
                    channel.id, guild.id,
                )
                try:
                    await voice_client.disconnect(force=True)
                except Exception:
                    logger.exception("Error disconnecting before reconnect after failed move")
                voice_client = await self._connect_with_retry(channel, guild)

        finished = asyncio.Event()

        def _after(error):
            if error:
                logger.error("Playback error: %s", error)
            self.loop.call_soon_threadsafe(finished.set)

        source = discord.FFmpegPCMAudio(str(sound_path))
        voice_client.play(source, after=_after)
        await finished.wait()

    async def _idle_disconnect(self, guild_id: int):
        settings = load_settings()
        try:
            await asyncio.sleep(settings["idle_disconnect_seconds"])
        except asyncio.CancelledError:
            return
        guild = self.get_guild(guild_id)
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=False)
        self._idle_tasks.pop(guild_id, None)
