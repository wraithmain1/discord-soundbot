"""
Web dashboard: a login-gated page for uploading mp3s and assigning them
to Discord users. Runs in the same process/event loop as the bot.
"""

import os
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .discord_bot import load_sound_map, save_sound_map, SOUNDS_DIR

WEB_USERNAME = os.environ["WEB_USERNAME"]
WEB_PASSWORD = os.environ["WEB_PASSWORD"]
SESSION_SECRET = os.environ.get("SESSION_SECRET") or secrets.token_hex(32)

MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

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
    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    sounds = sorted(p.name for p in SOUNDS_DIR.glob("*.mp3"))

    assignments = [
        {"id": m["id"], "label": m["label"], "current": sound_map.get(m["id"], "")}
        for m in members
    ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {"assignments": assignments, "sounds": sounds},
    )


@app.post("/upload")
async def upload_sound(request: Request, file: UploadFile = File(...)):
    if not _logged_in(request):
        return RedirectResponse("/login", status_code=303)

    if not file.filename.lower().endswith(".mp3"):
        return RedirectResponse("/?error=invalid_type", status_code=303)

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        return RedirectResponse("/?error=too_large", status_code=303)

    SOUNDS_DIR.mkdir(parents=True, exist_ok=True)
    # Keep filenames simple/safe - strip any path components.
    safe_name = Path(file.filename).name
    dest = SOUNDS_DIR / safe_name
    with open(dest, "wb") as f:
        f.write(contents)

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
