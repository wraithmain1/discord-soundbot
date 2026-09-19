"""
Web dashboard: a login-gated page for uploading sounds (mp3/m4a - m4a is
auto-converted to mp3 on upload), assigning them to Discord users, deleting
sounds, and adjusting bot timing settings. Runs in the same process/event
loop as the bot.
"""

import asyncio
import logging
import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .discord_bot import (
    load_sound_map,
    save_sound_map,
    load_settings,
    save_settings,
    SOUNDS_DIR,
)

logger = logging.getLogger("web")

WEB_USERNAME = os.environ["WEB_USERNAME"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_UPLOAD_EXTENSIONS = {".mp3", ".m4a"}  # what you're allowed to upload
ALLOWED_EXTENSIONS = {".mp3"}  # what actually lives in SOUNDS_DIR after conversion
CONTENT_TYPES = {".mp3": "audio/mpeg"}

BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")

# Set by main.py once the bot instance exists, so the web UI can read its
# member cache and guild list without importing main (avoids circular import).
bot_instance = None


def set_bot_instance(bot):
    global bot_instance
    bot_instance = bot


def _logged_in(request: Request) -> bool:
    return request.session.get("logged_in") is True


def _list_sound_files() -> list[str]:
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    files = [p.name for p in SOUNDS_DIR.iterdir() if p.suffix.lower() in ALLOWED_EXTENSIONS]
    return sorted(files)


async def _convert_to_mp3(src: Path) -> Path:
    """Convert a non-mp3 upload (e.g. m4a) to mp3 in place using the ffmpeg
    already installed in this image. Runs as a real async subprocess so it
    doesn't block the bot's event loop while converting. Raises RuntimeError
    with ffmpeg's own error output on failure; caller is responsible for
    cleaning up the source file either way.
    """
    dest = src.with_suffix(".mp3")
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(src),
        "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        str(dest),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(stderr.decode(errors="ignore")[-800:])
    return dest


@app.get("/login", response_class=HTMLResponse)
async def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    if username == WEB_USERNAME and password == WEB_PASSWORD:
        request.session["logged_in"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid username or password"}
    )


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    members = bot_instance.get_member_list() if bot_instance else []
    voice_channels = bot_instance.get_voice_channels() if bot_instance else []
    active_channels = []
    if bot_instance:
        active_channels = [
            f"{vc.channel.guild.name} / {vc.channel.name}"
            for vc in bot_instance.voice_clients if vc.channel is not None
        ]
    sound_map = load_sound_map()
    sound_files = _list_sound_files()
    settings = load_settings()

    assignments = [
        {"id": m["id"], "label": m["label"], "current": sound_map.get(m["id"], "")}
        for m in members
    ]

    # For each sound file, list which member labels currently use it, so the
    # sound list shows what's in use before someone deletes something by accident.
    label_by_id = {m["id"]: m["label"] for m in members}
    sounds = []
    for filename in sound_files:
        used_by = [
            label_by_id[uid] for uid, fname in sound_map.items()
            if fname == filename and uid in label_by_id
        ]
        sounds.append({"filename": filename, "used_by": used_by})

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "assignments": assignments,
            "sounds": sound_files,
            "sound_list": sounds,
            "settings": settings,
            "voice_channels": voice_channels,
            "active_channels": active_channels,
            "played": request.query_params.get("played"),
        },
    )


@app.post("/upload")
async def upload_sound(request: Request, file: UploadFile = File(...)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(file.filename).name
    suffix = Path(safe_name).suffix.lower()
    if suffix not in ALLOWED_UPLOAD_EXTENSIONS:
        return RedirectResponse("/?error=invalid_type", status_code=303)

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/?error=too_large", status_code=303)

    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SOUNDS_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(contents)

    if suffix != ".mp3":
        # Convert anything else (currently just m4a) to mp3, so every stored
        # file is a consistent format the browser can always preview and
        # ffmpeg can always play without surprises.
        try:
            await _convert_to_mp3(dest)
        except RuntimeError:
            logger.exception("ffmpeg conversion failed for %s", safe_name)
            dest.unlink(missing_ok=True)
            return RedirectResponse("/?error=convert_failed", status_code=303)
        finally:
            # Whether conversion succeeded or the mp3 already existed under
            # this name, the original non-mp3 upload is no longer needed.
            dest.unlink(missing_ok=True)

    return RedirectResponse("/", status_code=303)


@app.post("/delete-sound")
async def delete_sound(request: Request, filename: str = Form(...)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(filename).name  # guard against path traversal
    target = SOUNDS_DIR / safe_name
    if target.exists() and target.suffix.lower() in ALLOWED_EXTENSIONS:
        target.unlink()

    # Clean up any user assignments that pointed at the now-deleted file,
    # so the dashboard doesn't show a "ghost" assignment.
    sound_map = load_sound_map()
    changed = False
    for user_id in list(sound_map.keys()):
        if sound_map[user_id] == safe_name:
            del sound_map[user_id]
            changed = True
    if changed:
        save_sound_map(sound_map)

    return RedirectResponse("/", status_code=303)


@app.post("/assign")
async def assign_sound(request: Request, user_id: str = Form(...), sound_filename: str = Form("")):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    sound_map = load_sound_map()
    if sound_filename:
        sound_map[user_id] = sound_filename
    else:
        sound_map.pop(user_id, None)
    save_sound_map(sound_map)

    return RedirectResponse("/", status_code=303)


@app.get("/sounds/{filename}")
async def serve_sound(request: Request, filename: str):
    """Serves an uploaded sound file so the dashboard can preview it in-browser.
    Login-gated like everything else here - not a public file server.
    """
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(filename).name  # guard against path traversal
    suffix = Path(safe_name).suffix.lower()
    target = SOUNDS_DIR / safe_name
    if suffix not in ALLOWED_EXTENSIONS or not target.exists():
        return RedirectResponse("/", status_code=303)

    return FileResponse(target, media_type=CONTENT_TYPES.get(suffix, "application/octet-stream"))


@app.post("/play-sound")
async def play_sound_now(request: Request, filename: str = Form(...), channel_id: str = Form(...)):
    """Manually plays a sound through the bot in a chosen voice channel -
    useful for testing without waiting for a real join/move event."""
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(filename).name
    target = SOUNDS_DIR / safe_name
    if target.suffix.lower() not in ALLOWED_EXTENSIONS or not target.exists():
        return RedirectResponse("/?error=invalid_type", status_code=303)

    if not channel_id or not bot_instance:
        return RedirectResponse("/", status_code=303)

    try:
        ok = await bot_instance.play_now(int(channel_id), target)
    except ValueError:
        ok = False

    return RedirectResponse(f"/?played={'1' if ok else '0'}", status_code=303)


@app.post("/play-sound-here")
async def play_sound_here(request: Request, filename: str = Form(...)):
    """Plays a sound in whichever voice channel the bot is currently
    connected to, without requiring a manual channel pick. Only works
    when the bot is connected in exactly one place at a time - if it's
    idle everywhere, or active in more than one server at once, there's
    no single 'here' to guess, so it asks you to use the dropdown instead.
    """
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(filename).name
    target = SOUNDS_DIR / safe_name
    if target.suffix.lower() not in ALLOWED_EXTENSIONS or not target.exists():
        return RedirectResponse("/?error=invalid_type", status_code=303)

    if not bot_instance:
        return RedirectResponse("/?played=0", status_code=303)

    active_channels = [vc.channel for vc in bot_instance.voice_clients if vc.channel is not None]
    if len(active_channels) != 1:
        return RedirectResponse("/?played=ambiguous", status_code=303)

    ok = await bot_instance.play_now(active_channels[0].id, target)
    return RedirectResponse(f"/?played={'1' if ok else '0'}", status_code=303)


@app.post("/settings")
async def update_settings(
    request: Request,
    cooldown_seconds: float = Form(...),
    idle_disconnect_seconds: float = Form(...),
):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    # Keep values sane - no negative or absurd numbers from a stray typo.
    cooldown_seconds = max(0.0, min(cooldown_seconds, 60.0))
    idle_disconnect_seconds = max(0.0, min(idle_disconnect_seconds, 600.0))

    save_settings(cooldown_seconds, idle_disconnect_seconds)

    return RedirectResponse("/", status_code=303)
