import os
import re
import json
import time
import hashlib
import zipfile
import threading
import gc
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote
from typing import Optional, List
from collections import OrderedDict

import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Cookie, Response, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from PIL import Image, ImageOps, ExifTags

app = FastAPI()

PHOTOS_DIR = Path(os.getenv("PHOTOS_DIR", "/photos")).resolve()
CACHE_DIR = Path(os.getenv("CACHE_DIR", "/cache")).resolve()
ADMIN_USER = os.getenv("ADMIN_USER", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASS", "admin123")
SECRET_KEY = os.getenv("SECRET_KEY", "super-secret-pixie-key")

DEFAULT_EXCLUDE_EXTS = os.getenv(
    "DEFAULT_EXCLUDE_EXTS",
    ".cr2,.cr3,.nef,.arw,.dng,.orf,.rw2,.pef,.raf,.tif,.tiff,.psd,.xmp"
)
DEFAULT_EXCLUDE_MASKS = os.getenv(
    "DEFAULT_EXCLUDE_MASKS",
    "*_small.*, *_smal.*, *_thumb.*, ._*"
)

CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = CACHE_DIR / "config_db.json"
EXIF_CACHE_FILE = CACHE_DIR / "exif_cache.json"

templates = Jinja2Templates(directory="app/templates")
SUPPORTED_WEB_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

pregen_status = {
    "running": False,
    "cancel_requested": False,
    "total": 0,
    "processed": 0,
    "current_file": ""
}
pregen_lock = threading.Lock()
db_lock = threading.Lock()
exif_lock = threading.Lock()
thumb_semaphore = threading.Semaphore(2)

MAX_EXIF_CACHE_SIZE = 1000
_exif_mem_cache: Optional[OrderedDict] = None


def default_libraries_list() -> list:
    return [
        {
            "id": "default",
            "name": "Основной архив",
            "path": str(PHOTOS_DIR)
        }
    ]


def default_settings_dict() -> dict:
    return {
        "gallery_title": os.getenv("GALLERY_TITLE", "PHOTO ARCHIVE"),
        "show_exif": True,
        "allow_zip_download": True,
        "exclude_extensions": DEFAULT_EXCLUDE_EXTS,
        "exclude_masks": DEFAULT_EXCLUDE_MASKS,
        "hidden_folders": "",
        "show_hidden_albums": False,
        "libraries": default_libraries_list()
    }


def load_db() -> dict:
    if not DB_FILE.exists():
        default_db = {
            "settings": default_settings_dict(),
            "users": {
                ADMIN_USER: {
                    "password": hashlib.sha256(ADMIN_PASS.encode()).hexdigest(),
                    "role": "admin",
                    "mode": "allow",
                    "allowed_albums": ["*"],
                    "allowed_libraries": ["*"]
                }
            },
            "favorites": {}
        }
        save_db(default_db)
        return default_db
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            db = json.load(f)
        s = db.setdefault("settings", {})
        for k, v in default_settings_dict().items():
            s.setdefault(k, v)
        if not s.get("libraries") or not isinstance(s.get("libraries"), list):
            s["libraries"] = default_libraries_list()
        db.setdefault("users", {})
        db.setdefault("favorites", {})
        for uname, uobj in db["users"].items():
            uobj.setdefault("allowed_libraries", ["*"])
        if ADMIN_USER not in db["users"]:
            db["users"][ADMIN_USER] = {
                "password": hashlib.sha256(ADMIN_PASS.encode()).hexdigest(),
                "role": "admin",
                "mode": "allow",
                "allowed_albums": ["*"],
                "allowed_libraries": ["*"]
            }
            save_db(db)
        return db
    except Exception:
        return {
            "settings": default_settings_dict(),
            "users": {},
            "favorites": {}
        }


def save_db(data: dict):
    with db_lock:
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


def get_libraries_list(db: Optional[dict] = None) -> list:
    if db is None:
        db = load_db()
    libs = db.get("settings", {}).get("libraries", [])
    if not libs or not isinstance(libs, list):
        return default_libraries_list()
    return libs


def get_library_root(lib_id: Optional[str] = "default", db: Optional[dict] = None) -> tuple[str, Path, str]:
    libs = get_libraries_list(db)
    target_id = (lib_id or "default").strip() or "default"
    found = None
    for lib in libs:
        if lib.get("id") == target_id:
            found = lib
            break
    if not found:
        raise HTTPException(status_code=404, detail="Library not found")

    raw_path = Path(found.get("path", str(PHOTOS_DIR)))
    if found.get("id") == "default" and not raw_path.exists() and PHOTOS_DIR.exists():
        root_path = PHOTOS_DIR
    else:
        root_path = raw_path.resolve()
    return found["id"], root_path, found.get("name", found["id"])


def load_exif_cache() -> OrderedDict:
    global _exif_mem_cache
    with exif_lock:
        if _exif_mem_cache is not None:
            return _exif_mem_cache
        if EXIF_CACHE_FILE.exists():
            try:
                with open(EXIF_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    _exif_mem_cache = OrderedDict(data)
                    return _exif_mem_cache
            except Exception:
                pass
        _exif_mem_cache = OrderedDict()
        return _exif_mem_cache


def save_exif_cache():
    global _exif_mem_cache
    with exif_lock:
        if _exif_mem_cache is None:
            return
        try:
            with open(EXIF_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(_exif_mem_cache, f, ensure_ascii=False)
        except Exception:
            pass


def get_filter_rules(db: Optional[dict] = None):
    if db is None:
        db = load_db()
    settings = db.get("settings", {})
    raw_exts = settings.get("exclude_extensions", DEFAULT_EXCLUDE_EXTS)
    raw_masks = settings.get("exclude_masks", DEFAULT_EXCLUDE_MASKS)
    raw_hidden = settings.get("hidden_folders", "")

    excluded_exts = set()
    for item in raw_exts.split(","):
        clean = item.strip().lower()
        if not clean:
            continue
        if not clean.startswith("."):
            clean = "." + clean
        excluded_exts.add(clean)

    excluded_masks = [m.strip().lower() for m in raw_masks.split(",") if m.strip()]
    hidden_folders = [h.strip().strip("/").lower() for h in raw_hidden.split("|") if h.strip()]
    return excluded_exts, excluded_masks, hidden_folders


def is_folder_hidden(rel_path: str, hidden_folders: list, lib_id: str = "default") -> bool:
    if not hidden_folders:
        return False
    norm_path = rel_path.strip("/").lower()
    prefixed_path = f"{lib_id.lower()}::{norm_path}"
    for h in hidden_folders:
        norm_h = h.strip("/").lower()
        if "::" in norm_h:
            if prefixed_path == norm_h or prefixed_path.startswith(norm_h + "/"):
                return True
        elif lib_id == "default":
            if norm_path == norm_h or norm_path.startswith(norm_h + "/"):
                return True
    return False


def is_valid_image(p: Path, excluded_exts: set = None, excluded_masks: list = None) -> bool:
    if not p.is_file() or p.name.startswith("."):
        return False
    ext = p.suffix.lower()
    if ext not in SUPPORTED_WEB_EXTS:
        return False
    if excluded_exts is None or excluded_masks is None:
        ex_exts, ex_masks, _ = get_filter_rules()
    else:
        ex_exts, ex_masks = excluded_exts, excluded_masks

    if ext in ex_exts:
        return False
    fname_lower = p.name.lower()
    for mask in ex_masks:
        if fnmatch(fname_lower, mask):
            return False
    return True


def create_token(username: str, role: str) -> str:
    payload = {"sub": username, "role": role, "exp": int(time.time()) + 86400 * 30}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")


def get_current_user(pixie_token: Optional[str] = Cookie(default=None)) -> dict:
    if not pixie_token:
        raise HTTPException(status_code=401, detail="Not authenticated")
    try:
        payload = jwt.decode(pixie_token, SECRET_KEY, algorithms=["HS256"])
        username = payload.get("sub")
        db = load_db()
        user_obj = db.get("users", {}).get(username)
        if not user_obj:
            raise HTTPException(status_code=401, detail="User not found")
        return {
            "username": username,
            "role": user_obj.get("role", "user"),
            "mode": user_obj.get("mode", "allow"),
            "allowed_albums": user_obj.get("allowed_albums", []),
            "allowed_libraries": user_obj.get("allowed_libraries", ["*"]),
            "default_library": user_obj.get("default_library")
        }
    except Exception:
        raise HTTPException(status_code=401, detail="Invalid token")


def safe_resolve(rel_path: str, lib_id: Optional[str] = "default", db: Optional[dict] = None) -> Path:
    _, lib_root, _ = get_library_root(lib_id, db)
    target = (lib_root / rel_path).resolve()
    if not str(target).startswith(str(lib_root)):
        raise HTTPException(status_code=403, detail="Access denied")
    if not target.exists():
        raise HTTPException(status_code=404, detail="Not found")
    return target


def user_can_access_library(user: dict, lib_id: str) -> bool:
    if user["role"] == "admin":
        return True
    allowed_libs = user.get("allowed_libraries", ["*"])
    if "*" in allowed_libs or lib_id in allowed_libs:
        return True
    return False


def user_can_access_album(user: dict, album_rel_path: str, lib_id: str = "default") -> bool:
    if not user_can_access_library(user, lib_id):
        return False
    if user["role"] == "admin":
        return True
    mode = user.get("mode", "allow")
    albums = user.get("allowed_albums", [])
    if "*" in albums:
        return True if mode == "allow" else False

    norm_album = album_rel_path.strip("/").lower()
    norm_lib = (lib_id or "default").strip().lower()
    prefixed_album = f"{norm_lib}::{norm_album}"
    matched = False

    for allowed in albums:
        norm_allowed = allowed.strip("/").lower()
        if not norm_allowed:
            continue
        if "::" in norm_allowed:
            if prefixed_album == norm_allowed or prefixed_album.startswith(norm_allowed + "/"):
                matched = True
                break
            parts = norm_allowed.split("::", 1)
            if parts[0] == norm_lib and (norm_album == parts[1] or norm_album.startswith(parts[1] + "/")):
                matched = True
                break
        else:
            if norm_album == norm_allowed or norm_album.startswith(norm_allowed + "/"):
                matched = True
                break

    return matched if mode == "allow" else not matched


def user_can_access_image(user: dict, img_rel_path: str, lib_id: str = "default") -> bool:
    if not user_can_access_library(user, lib_id):
        return False
    if user["role"] == "admin":
        return True
    parent_album = str(Path(img_rel_path).parent.as_posix())
    return user_can_access_album(user, parent_album, lib_id)


def generate_cached_thumb(orig_path: Path, size: int = 400) -> Path:
    target_size = 1200 if size >= 1000 else 400
    stat = orig_path.stat()
    cache_key = f"{orig_path}_{stat.st_mtime}_{target_size}".encode("utf-8")
    hash_name = f"{hashlib.md5(cache_key).hexdigest()}_{target_size}.webp"
    cache_file = CACHE_DIR / hash_name[:2] / hash_name

    if cache_file.exists():
        return cache_file

    with thumb_semaphore:
        if not cache_file.exists():
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            with Image.open(orig_path) as img:
                if hasattr(img, "draft") and img.format in ("JPEG", "MPO"):
                    try:
                        img.draft("RGB", (target_size * 2, target_size * 2))
                    except Exception:
                        pass
                img = ImageOps.exif_transpose(img)
                if img.mode != "RGB":
                    img = img.convert("RGB")
                img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                quality = 85 if target_size == 1200 else 80
                img.save(cache_file, "WEBP", quality=quality)
            gc.collect()
    return cache_file


def extract_exif_data_cached(orig_path: Path) -> tuple[dict, bool]:
    cache = load_exif_cache()
    try:
        stat = orig_path.stat()
        mtime = int(stat.st_mtime)
    except Exception:
        mtime = 0

    key = str(orig_path)
    cached_entry = cache.get(key)
    
    with exif_lock:
        if cached_entry and cached_entry.get("mtime") == mtime:
            cache.move_to_end(key) # LRU: mark as recently used
            return cached_entry.get("data", {}), False

    exif_info = {}
    try:
        with Image.open(orig_path) as img:
            exif_info["dimensions"] = f"{img.width}×{img.height}"
            raw_exif = img.getexif()
            if raw_exif:
                tag_map = {ExifTags.TAGS.get(k, k): v for k, v in raw_exif.items()}
                if 0x8769 in raw_exif:
                    try:
                        sub_ifd = raw_exif.get_ifd(0x8769)
                        for k, v in sub_ifd.items():
                            tag_map[ExifTags.TAGS.get(k, k)] = v
                    except Exception:
                        pass

                make = str(tag_map.get("Make", "")).strip().replace("\x00", "")
                model = str(tag_map.get("Model", "")).strip().replace("\x00", "")
                if make or model:
                    camera = f"{make} {model}".strip()
                    if make and model.lower().startswith(make.lower()):
                        camera = model
                    exif_info["camera"] = camera

                lens = str(tag_map.get("LensModel", "")).strip().replace("\x00", "")
                if lens:
                    exif_info["lens"] = lens

                f_num = tag_map.get("FNumber")
                if f_num:
                    try:
                        exif_info["aperture"] = f"f/{float(f_num):.1f}".rstrip("0").rstrip(".")
                    except Exception:
                        pass

                exp = tag_map.get("ExposureTime")
                if exp:
                    try:
                        val = float(exp)
                        if 0 < val < 1:
                            exif_info["shutter"] = f"1/{int(round(1/val))}s"
                        else:
                            exif_info["shutter"] = f"{val}s"
                    except Exception:
                        pass

                iso = tag_map.get("ISOSpeedRatings") or tag_map.get("PhotographicSensitivity")
                if iso:
                    exif_info["iso"] = f"ISO {iso}"

                fl = tag_map.get("FocalLength")
                if fl:
                    try:
                        exif_info["focal"] = f"{int(round(float(fl)))}mm"
                    except Exception:
                        pass

                dt = tag_map.get("DateTimeOriginal") or tag_map.get("DateTime")
                if dt:
                    clean_dt = str(dt).strip().replace("\x00", "")[:19]
                    if len(clean_dt) >= 16:
                        exif_info["date"] = clean_dt[:16].replace(":", "-", 2)
                        exif_info["sort_date"] = clean_dt.replace(":", "-", 2)
    except Exception:
        pass

    if "sort_date" not in exif_info:
        exif_info["sort_date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))

    with exif_lock:
        cache[key] = {"mtime": mtime, "data": exif_info}
        if len(cache) > MAX_EXIF_CACHE_SIZE:
            cache.popitem(last=False) # LRU: remove oldest

    return exif_info, True


class LoginRequest(BaseModel):
    username: str
    password: str


class SettingsUpdateRequest(BaseModel):
    gallery_title: str = "PHOTO ARCHIVE"
    show_exif: bool
    allow_zip_download: bool
    exclude_extensions: str = ""
    exclude_masks: str = ""
    hidden_folders: str = ""
    show_hidden_albums: bool = False


class LibraryUpsertRequest(BaseModel):
    id: Optional[str] = None
    name: str
    path: str


class UserUpsertRequest(BaseModel):
    username: str
    password: Optional[str] = None
    role: str = "user"
    mode: str = "allow"
    allowed_albums: List[str] = []
    allowed_libraries: List[str] = ["*"]
    default_library: Optional[str] = None
    original_username: Optional[str] = None


class FavoriteToggleRequest(BaseModel):
    photo_path: str
    lib: Optional[str] = "default"


@app.get("/pixie_gallery.svg", include_in_schema=False)
@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    svg_path = Path(__file__).parent / "static" / "pixie_gallery.svg"
    if not svg_path.exists():
        svg_path = Path(__file__).parent.parent / "pixie_gallery.svg"
    if svg_path.exists():
        return FileResponse(svg_path, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="Favicon not found")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    db = load_db()
    title = db.get("settings", {}).get("gallery_title", "PHOTO ARCHIVE")
    return templates.TemplateResponse("index.html", {"request": request, "title": title})


@app.post("/api/login")
def login(data: LoginRequest, response: Response):
    db = load_db()
    user_obj = db.get("users", {}).get(data.username)
    pw_hash = hashlib.sha256(data.password.encode()).hexdigest()
    if not user_obj or user_obj.get("password") != pw_hash:
        raise HTTPException(status_code=401, detail="Invalid credentials")
    token = create_token(data.username, user_obj.get("role", "user"))
    response.set_cookie(key="pixie_token", value=token, httponly=True, max_age=86400 * 30, samesite="lax")
    return {"status": "ok", "username": data.username, "role": user_obj.get("role", "user")}


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("pixie_token")
    return {"status": "ok"}


@app.get("/api/me")
def get_me(user: dict = Depends(get_current_user)):
    db = load_db()
    return {"user": user, "settings": db.get("settings", default_settings_dict())}


@app.post("/api/settings")
def update_settings(req: SettingsUpdateRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = load_db()
    existing_libs = get_libraries_list(db)
    db["settings"] = {
        "gallery_title": req.gallery_title.strip() or "PHOTO ARCHIVE",
        "show_exif": req.show_exif,
        "allow_zip_download": req.allow_zip_download,
        "exclude_extensions": req.exclude_extensions.strip(),
        "exclude_masks": req.exclude_masks.strip(),
        "hidden_folders": req.hidden_folders.strip(),
        "show_hidden_albums": req.show_hidden_albums,
        "libraries": existing_libs
    }
    save_db(db)
    return {"status": "ok", "settings": db["settings"]}


def _format_libraries_response(db: dict, user: dict) -> list:
    libs = get_libraries_list(db)
    out = []
    for lib in libs:
        lid = lib.get("id", "default")
        if not user_can_access_library(user, lid):
            continue
        raw_p = Path(lib.get("path", str(PHOTOS_DIR)))
        if lid == "default" and not raw_p.exists() and PHOTOS_DIR.exists():
            resolved_p = PHOTOS_DIR
        else:
            resolved_p = raw_p
        item = {
            "id": lid,
            "name": lib.get("name", lid)
        }
        if user["role"] == "admin":
            item["path"] = lib.get("path", str(PHOTOS_DIR))
            item["exists"] = resolved_p.is_dir()
        out.append(item)
    return out


@app.get("/api/libraries")
def list_libraries(user: dict = Depends(get_current_user)):
    db = load_db()
    return {"libraries": _format_libraries_response(db, user)}


@app.post("/api/libraries")
def save_library(req: LibraryUpsertRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    name = req.name.strip()
    path_str = req.path.strip()
    if not name or not path_str:
        raise HTTPException(status_code=400, detail="Name and path are required")

    db = load_db()
    libs = get_libraries_list(db)

    if req.id:
        target_id = req.id.strip()
        updated = False
        for lib in libs:
            if lib.get("id") == target_id:
                lib["name"] = name
                lib["path"] = path_str
                updated = True
                break
        if not updated:
            libs.append({"id": target_id, "name": name, "path": path_str})
    else:
        slug = re.sub(r"[^a-zA-Z0-9_-]", "", name.lower().replace(" ", "_"))
        if not slug:
            slug = "lib"
        existing_ids = {l.get("id") for l in libs}
        candidate = slug
        counter = 2
        while candidate in existing_ids:
            candidate = f"{slug}_{counter}"
            counter += 1
        libs.append({"id": candidate, "name": name, "path": path_str})

    db.setdefault("settings", {})["libraries"] = libs
    save_db(db)
    return {"status": "ok", "libraries": _format_libraries_response(db, user)}


@app.delete("/api/libraries/{lib_id}")
def delete_library(lib_id: str, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = load_db()
    libs = get_libraries_list(db)
    if len(libs) <= 1:
        raise HTTPException(status_code=400, detail="Cannot delete the only library")
    new_libs = [l for l in libs if l.get("id") != lib_id]
    if len(new_libs) == len(libs):
        raise HTTPException(status_code=404, detail="Library not found")
    db.setdefault("settings", {})["libraries"] = new_libs
    save_db(db)
    return {"status": "ok", "libraries": _format_libraries_response(db, user)}


@app.post("/api/cache/clear")
def clear_cache(user: dict = Depends(get_current_user)):
    global _exif_mem_cache
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")

    count = 0
    if CACHE_DIR.exists():
        for dirpath, dirnames, filenames in os.walk(CACHE_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp) and f != "config_db.json":
                    try:
                        os.remove(fp)
                        count += 1
                    except Exception:
                        pass
    with exif_lock:
        _exif_mem_cache = OrderedDict()
    gc.collect()
    return {"status": "ok", "deleted_files": count}


@app.get("/api/users")
def list_users(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    db = load_db()
    result = []
    for uname, udata in db.get("users", {}).items():
        fav_count = len(db.get("favorites", {}).get(uname, []))
        result.append({
            "username": uname,
            "role": udata.get("role", "user"),
            "mode": udata.get("mode", "allow"),
            "allowed_albums": udata.get("allowed_albums", []),
            "allowed_libraries": udata.get("allowed_libraries", ["*"]),
            "default_library": udata.get("default_library"),
            "favorites_count": fav_count
        })
    return {"users": result}


@app.post("/api/users")
def save_user(req: UserUpsertRequest, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    uname = req.username.strip()
    if not uname:
        raise HTTPException(status_code=400, detail="Username required")
    db = load_db()
    existing = db.setdefault("users", {}).get(uname)
    orig_name = (req.original_username or "").strip()
    if orig_name and orig_name != uname and orig_name in db.get("users", {}):
        old_data = db["users"].pop(orig_name)
        if not existing:
            existing = old_data
            if orig_name in db.get("favorites", {}):
                db["favorites"][uname] = db["favorites"].pop(orig_name)
    if not existing and not req.password:
        raise HTTPException(status_code=400, detail="Password required for new user")

    pw_hash = existing["password"] if (existing and not req.password) else hashlib.sha256(req.password.encode()).hexdigest()
    def_lib = (req.default_library or "").strip()
    db["users"][uname] = {
        "password": pw_hash,
        "role": req.role,
        "mode": req.mode,
        "allowed_albums": req.allowed_albums,
        "allowed_libraries": req.allowed_libraries if req.allowed_libraries else ["*"],
        "default_library": def_lib if def_lib and def_lib != "auto" else None
    }
    save_db(db)
    return {"status": "ok"}


@app.delete("/api/users/{uname}")
def delete_user(uname: str, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    if uname == user["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete yourself")
    db = load_db()
    if uname in db.get("users", {}):
        del db["users"][uname]
        save_db(db)
    return {"status": "ok"}


@app.post("/api/favorites/toggle")
def toggle_favorite(req: FavoriteToggleRequest, user: dict = Depends(get_current_user)):
    rel_clean = unquote(req.photo_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(req.lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    ex_exts, ex_masks, _ = get_filter_rules(db)
    if not is_valid_image(orig_path, ex_exts, ex_masks):
        raise HTTPException(status_code=404, detail="Image not found")
    uname = user["username"]
    user_favs = set(db.setdefault("favorites", {}).get(uname, []))
    fav_key = rel_clean if lib_id == "default" else f"{lib_id}::{rel_clean}"
    if fav_key in user_favs:
        user_favs.remove(fav_key)
        is_favorite = False
    else:
        user_favs.add(fav_key)
        is_favorite = True
    db["favorites"][uname] = sorted(list(user_favs))
    save_db(db)
    return {"status": "ok", "photo_path": rel_clean, "lib": lib_id, "is_favorite": is_favorite}


def run_all_thumbnails_task():
    global pregen_status
    db = load_db()
    ex_exts, ex_masks, hidden_folders = get_filter_rules(db)
    libs = get_libraries_list(db)
    all_images = []

    for lib in libs:
        if pregen_status["cancel_requested"]:
            break
        lid = lib.get("id", "default")
        try:
            _, lib_root, _ = get_library_root(lid, db)
        except Exception:
            continue
        if not lib_root.exists():
            continue
        for root, dirs, files in os.walk(lib_root):
            if pregen_status["cancel_requested"]:
                break
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            current_dir = Path(root)
            if current_dir != lib_root:
                rel = current_dir.relative_to(lib_root).as_posix()
                if is_folder_hidden(rel, hidden_folders, lid):
                    continue
            for f in files:
                p = Path(root) / f
                if is_valid_image(p, ex_exts, ex_masks):
                    all_images.append(p)

    with pregen_lock:
        pregen_status["total"] = len(all_images)
        pregen_status["processed"] = 0

    exif_dirty = False
    for idx, img_path in enumerate(all_images, start=1):
        if pregen_status["cancel_requested"]:
            break
        with pregen_lock:
            pregen_status["current_file"] = img_path.name
        try:
            generate_cached_thumb(img_path, 400)
            generate_cached_thumb(img_path, 1200)
            _, updated = extract_exif_data_cached(img_path)
            if updated:
                exif_dirty = True
        except Exception:
            pass
        
        with pregen_lock:
            pregen_status["processed"] = idx
            
        if idx % 50 == 0:
            gc.collect()

    if exif_dirty:
        save_exif_cache()

    with pregen_lock:
        pregen_status["running"] = False
        pregen_status["cancel_requested"] = False
        pregen_status["current_file"] = "DONE"
    
    gc.collect()


@app.post("/api/pregenerate")
def start_pregenerate(background_tasks: BackgroundTasks, user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    with pregen_lock:
        if pregen_status["running"]:
            return {"status": "already_running", "progress": pregen_status}
        pregen_status["running"] = True
        pregen_status["cancel_requested"] = False
        pregen_status["processed"] = 0
        pregen_status["total"] = 0
        pregen_status["current_file"] = "SCANNING"
    background_tasks.add_task(run_all_thumbnails_task)
    return {"status": "started", "progress": pregen_status}


@app.post("/api/pregenerate/cancel")
def cancel_pregenerate(user: dict = Depends(get_current_user)):
    if user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    with pregen_lock:
        pregen_status["cancel_requested"] = True
        pregen_status["running"] = False
    return {"status": "canceled"}


@app.get("/api/pregenerate/status")
def get_pregenerate_status(user: dict = Depends(get_current_user)):
    cache_size_bytes = 0
    if CACHE_DIR.exists():
        for dirpath, dirnames, filenames in os.walk(CACHE_DIR):
            for f in filenames:
                fp = os.path.join(dirpath, f)
                if not os.path.islink(fp) and f != "config_db.json":
                    try:
                        cache_size_bytes += os.path.getsize(fp)
                    except Exception:
                        pass

    res = pregen_status.copy()
    res["cache_size_bytes"] = cache_size_bytes
    return res


def scan_all_albums_recursive(
    ex_exts: set,
    ex_masks: list,
    hidden_folders: list,
    show_hidden: bool,
    lib_id: str = "default",
    lib_root: Optional[Path] = None
) -> list:
    if lib_root is None:
        _, lib_root, _ = get_library_root(lib_id)
    if not lib_root.exists():
        return []

    albums_flat = []
    q_lib = quote(lib_id)
    for root, dirs, files in os.walk(lib_root):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        current_dir = Path(root)
        if current_dir == lib_root:
            continue

        rel_path = current_dir.relative_to(lib_root).as_posix()
        is_hidden = is_folder_hidden(rel_path, hidden_folders, lib_id)
        
        if is_hidden and not show_hidden:
            continue

        valid_imgs = sorted(
            [f for f in current_dir.iterdir() if is_valid_image(f, ex_exts, ex_masks)],
            key=lambda x: x.name
        )
        if valid_imgs:
            cover_rel = valid_imgs[0].relative_to(lib_root).as_posix()
            parts = rel_path.split("/")
            year_tag = parts[0] if len(parts) > 0 else "Archive"

            if len(parts) > 1:
                display_name = "/".join(parts[1:])
            else:
                display_name = parts[0]

            albums_flat.append({
                "name": display_name,
                "path": rel_path,
                "year": year_tag,
                "count": len(valid_imgs),
                "cover": f"/api/thumb/400/{quote(cover_rel)}?lib={q_lib}",
                "is_hidden": is_hidden
            })

    years_dict = {}
    for alb in sorted(albums_flat, key=lambda x: x["path"], reverse=True):
        y = alb["year"]
        years_dict.setdefault(y, []).append(alb)

    result_years = []
    for y in sorted(years_dict.keys(), reverse=True):
        result_years.append({
            "year": y,
            "albums": sorted(years_dict[y], key=lambda x: x["path"], reverse=True)
        })

    return result_years


@app.get("/api/structure")
def get_structure(lib: Optional[str] = None, user: dict = Depends(get_current_user)):
    db = load_db()
    ex_exts, ex_masks, hidden_folders = get_filter_rules(db)
    show_hidden = db.get("settings", {}).get("show_hidden_albums", False)

    accessible_libs = _format_libraries_response(db, user)
    if not accessible_libs:
        return {
            "libraries": [],
            "active_library": None,
            "years": [],
            "all_albums_catalog": []
        }

    requested_id = (lib or "").strip()
    active_lib = next((l for l in accessible_libs if l["id"] == requested_id), None)
    if not active_lib:
        user_def_lib = user.get("default_library")
        if user_def_lib:
            active_lib = next((l for l in accessible_libs if l["id"] == user_def_lib), None)
    if not active_lib:
        active_lib = accessible_libs[0]
    active_id = active_lib["id"]
    _, active_root, _ = get_library_root(active_id, db)

    def _filter_years_for_lib(target_lib_id, target_root):
        raw = scan_all_albums_recursive(
            ex_exts, ex_masks, hidden_folders, show_hidden,
            lib_id=target_lib_id, lib_root=target_root
        )
        f_years = []
        f_flat = []
        for y in raw:
            allowed_list = []
            for alb in y["albums"]:
                if user_can_access_album(user, alb["path"], target_lib_id):
                    allowed_list.append(alb)
                    f_flat.append({
                        "path": alb["path"],
                        "label": alb["path"],
                        "name": alb["name"]
                    })
            if allowed_list:
                f_years.append({"year": y["year"], "albums": allowed_list})
        return f_years, f_flat

    filtered_years, active_albums_flat = _filter_years_for_lib(active_id, active_root)

    # If active_lib has no accessible albums for this non-admin user, but another library does, auto-switch:
    if not filtered_years and user["role"] != "admin" and len(accessible_libs) > 1:
        for other_lib in accessible_libs:
            if other_lib["id"] == active_id:
                continue
            try:
                _, other_root, _ = get_library_root(other_lib["id"], db)
                o_years, o_flat = _filter_years_for_lib(other_lib["id"], other_root)
                if o_years:
                    active_lib = other_lib
                    active_id = other_lib["id"]
                    filtered_years = o_years
                    active_albums_flat = o_flat
                    break
            except Exception:
                continue

    admin_catalog = []
    if user["role"] == "admin":
        all_libs = get_libraries_list(db)
        multi = len(all_libs) > 1
        for lobj in all_libs:
            lid = lobj.get("id", "default")
            lname = lobj.get("name", lid)
            try:
                _, lroot, _ = get_library_root(lid, db)
            except Exception:
                continue
            lyears = scan_all_albums_recursive(
                ex_exts, ex_masks, hidden_folders, show_hidden,
                lib_id=lid, lib_root=lroot
            )
            for y in lyears:
                for alb in y["albums"]:
                    perm_key = alb["path"] if lid == "default" else f"{lid}::{alb['path']}"
                    disp_label = f"[{lname}] {alb['path']}" if multi else alb["path"]
                    disp_name = f"[{lname}] {alb['name']}" if multi else alb["name"]
                    admin_catalog.append({
                        "path": perm_key,
                        "label": disp_label,
                        "name": disp_name,
                        "lib_id": lid,
                        "rel_path": alb["path"]
                    })

    return {
        "libraries": accessible_libs,
        "active_library": active_lib,
        "years": filtered_years,
        "sibling_catalog": active_albums_flat,
        "all_albums_catalog": admin_catalog if user["role"] == "admin" else active_albums_flat
    }


@app.get("/api/album/{album_path:path}")
def get_album(album_path: str, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    rel_clean = unquote(album_path).strip("/")
    db = load_db()
    lib_id, lib_root, _ = get_library_root(lib, db)
    if not user_can_access_album(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    target_dir = safe_resolve(rel_clean, lib_id, db)
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Album not found")
    ex_exts, ex_masks, _ = get_filter_rules(db)
    show_exif = db.get("settings", {}).get("show_exif", True)
    user_favs = set(db.get("favorites", {}).get(user["username"], []))

    images = sorted([f for f in target_dir.iterdir() if is_valid_image(f, ex_exts, ex_masks)], key=lambda x: x.name)
    photos = []
    exif_dirty = False
    q_lib = quote(lib_id)

    for img in images:
        rel = img.relative_to(lib_root).as_posix()
        q_rel = quote(rel)
        exif_data, updated = extract_exif_data_cached(img)
        if updated:
            exif_dirty = True

        fav_key = rel if lib_id == "default" else f"{lib_id}::{rel}"
        photos.append({
            "name": img.name,
            "path": rel,
            "thumb": f"/api/thumb/400/{q_rel}?lib={q_lib}",
            "preview": f"/api/thumb/1200/{q_rel}?lib={q_lib}",
            "original": f"/api/original/{q_rel}?lib={q_lib}",
            "is_favorite": fav_key in user_favs,
            "sort_date": exif_data.get("sort_date", ""),
            "exif": exif_data if show_exif else {}
        })

    if exif_dirty:
        save_exif_cache()

    parts = rel_clean.split("/")
    if len(parts) > 1:
        display_name = "/".join(parts[1:])
    else:
        display_name = parts[0]

    return {
        "name": display_name,
        "path": rel_clean,
        "lib": lib_id,
        "year": parts[0] if parts else "",
        "count": len(photos),
        "cover": photos[0]["preview"] if photos else None,
        "photos": photos
    }


@app.get("/api/download-zip/{album_path:path}")
def download_album_zip(
    album_path: str,
    favorites_only: bool = False,
    lib: Optional[str] = "default",
    user: dict = Depends(get_current_user)
):
    rel_clean = unquote(album_path).strip("/")
    db = load_db()
    lib_id, lib_root, _ = get_library_root(lib, db)
    if not user_can_access_album(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    if not db.get("settings", {}).get("allow_zip_download", True) and user["role"] != "admin":
        raise HTTPException(status_code=403, detail="ZIP download disabled")
    target_dir = safe_resolve(rel_clean, lib_id, db)
    if not target_dir.is_dir():
        raise HTTPException(status_code=404, detail="Album not found")
    ex_exts, ex_masks, _ = get_filter_rules(db)
    user_favs = set(db.get("favorites", {}).get(user["username"], []))
    images = sorted([f for f in target_dir.iterdir() if is_valid_image(f, ex_exts, ex_masks)], key=lambda x: x.name)
    if favorites_only:
        def _is_fav(img_p: Path) -> bool:
            r = img_p.relative_to(lib_root).as_posix()
            fk = r if lib_id == "default" else f"{lib_id}::{r}"
            return fk in user_favs
        images = [img for img in images if _is_fav(img)]
    if not images:
        raise HTTPException(status_code=404, detail="No photos to download")
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for img in images:
            zf.write(img, arcname=img.name)
    zip_buffer.seek(0)
    suffix = "_favorites" if favorites_only else ""
    safe_filename = quote(f"{target_dir.name}{suffix}.zip")
    return StreamingResponse(
        zip_buffer,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}"}
    )


def _serve_thumbnail(img_path: str, target_size: int, user: dict, lib: Optional[str] = "default") -> FileResponse:
    rel_clean = unquote(img_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    if not is_valid_image(orig_path):
        raise HTTPException(status_code=404, detail="Image not found")
    try:
        cache_file = generate_cached_thumb(orig_path, target_size)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Image error: {e}")

    safe_stem = quote(f"{orig_path.stem}_{target_size}px.webp")
    return FileResponse(
        cache_file,
        media_type="image/webp",
        headers={
            "Cache-Control": "public, max-age=31536000",
            "Content-Disposition": f"inline; filename*=UTF-8''{safe_stem}"
        }
    )


@app.get("/api/thumb/400/{img_path:path}")
def get_thumbnail_400(img_path: str, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    return _serve_thumbnail(img_path, 400, user, lib)


@app.get("/api/thumb/1200/{img_path:path}")
def get_thumbnail_1200(img_path: str, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    return _serve_thumbnail(img_path, 1200, user, lib)


@app.get("/api/thumb/{img_path:path}")
def get_thumbnail_legacy(img_path: str, size: int = 400, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    return _serve_thumbnail(img_path, 1200 if size >= 1000 else 400, user, lib)


@app.get("/api/original/{img_path:path}")
def get_original(img_path: str, download: bool = False, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    rel_clean = unquote(img_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    if not is_valid_image(orig_path):
        raise HTTPException(status_code=404, detail="Image not found")
    if download:
        return FileResponse(orig_path, filename=orig_path.name)
    safe_name = quote(orig_path.name)
    return FileResponse(
        orig_path,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{safe_name}"}
    )
