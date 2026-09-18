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
    """Return {"cooldown_seconds": float, "idle_disconnect_seconds": float}.

    Falls back to the env-var defaults if settings.json doesn't exist yet
    or can't be parsed.
    """
    defaults = {
        "cooldown_seconds": DEFAULT_COOLDOWN_SECONDS,
        "idle_disconnect_seconds": DEFAULT_IDLE_DISCONNECT_SECONDS,
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
        }
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        logger.exception("Failed to read settings, using defaults")
        return defaults


def save_settings(cooldown_seconds: float, idle_disconnect_seconds: float) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(SETTINGS_PATH, "w") as f:
        json.dump(
            {
                "cooldown_seconds": cooldown_seconds,
                "idle_disconnect_seconds": idle_disconnect_seconds,
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

    async def _play_in_channel(self, channel: discord.VoiceChannel, sound_path: Path):
        guild = channel.guild
        voice_client = guild.voice_client

        if voice_client is None:
            voice_client = await channel.connect()
        elif voice_client.channel.id != channel.id:
            await voice_client.move_to(channel)

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
