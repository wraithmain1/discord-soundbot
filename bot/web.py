"""
Web dashboard: a login-gated page for uploading sounds (mp3/m4a), assigning
them to Discord users, deleting sounds, and adjusting bot timing settings.
Runs in the same process/event loop as the bot.
"""

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .discord_bot import (
    load_sound_map,
    save_sound_map,
    load_settings,
    save_settings,
    SOUNDS_DIR,
)

WEB_USERNAME = os.environ["WEB_USERNAME"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_EXTENSIONS = {".mp3", ".m4a"}

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
        },
    )


@app.post("/upload")
async def upload_sound(request: Request, file: UploadFile = File(...)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    safe_name = Path(file.filename).name
    if Path(safe_name).suffix.lower() not in ALLOWED_EXTENSIONS:
        return RedirectResponse("/?error=invalid_type", status_code=303)

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/?error=too_large", status_code=303)

    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    dest = SOUNDS_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(contents)

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
