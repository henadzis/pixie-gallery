import os
import re
import json
import time
import hashlib
import zipfile
import threading
import subprocess
import shutil
import gc
from fnmatch import fnmatch
from io import BytesIO
from pathlib import Path
from urllib.parse import quote, unquote
from typing import Optional, List
from collections import OrderedDict

import jwt
from fastapi import FastAPI, HTTPException, Request, Depends, Cookie, Response, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse, JSONResponse
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
VIDEO_CACHE_DIR = CACHE_DIR / "transcoded"
VIDEO_CACHE_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = CACHE_DIR / "config_db.json"
EXIF_CACHE_FILE = CACHE_DIR / "exif_cache.json"
VIDEO_META_CACHE_FILE = CACHE_DIR / "video_meta_cache.json"

templates = Jinja2Templates(directory="app/templates")

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
SUPPORTED_WEB_EXTS = SUPPORTED_IMAGE_EXTS  # backward compatibility alias

SUPPORTED_NATIVE_VIDEO_EXTS = {".mp4", ".m4v", ".webm", ".mov"}
SUPPORTED_TRANSCODE_VIDEO_EXTS = {".mkv", ".avi", ".wmv", ".flv", ".mts", ".m2ts", ".ts", ".3gp"}
SUPPORTED_VIDEO_EXTS = SUPPORTED_NATIVE_VIDEO_EXTS | SUPPORTED_TRANSCODE_VIDEO_EXTS
SUPPORTED_MEDIA_EXTS = SUPPORTED_IMAGE_EXTS | SUPPORTED_VIDEO_EXTS

video_transcode_status = {}  # {hash_str: {"status": "processing"|"ready"|"error", "progress": int, "error": str}}
video_transcode_lock = threading.Lock()
video_meta_lock = threading.Lock()
_video_meta_cache: Optional[dict] = None

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


def is_valid_media(p: Path, excluded_exts: set = None, excluded_masks: list = None) -> bool:
    if not p.is_file() or p.name.startswith("."):
        return False
    ext = p.suffix.lower()
    if ext not in SUPPORTED_MEDIA_EXTS:
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


def is_valid_image(p: Path, excluded_exts: set = None, excluded_masks: list = None) -> bool:
    if not is_valid_media(p, excluded_exts, excluded_masks):
        return False
    return p.suffix.lower() in SUPPORTED_IMAGE_EXTS


def is_valid_video(p: Path, excluded_exts: set = None, excluded_masks: list = None) -> bool:
    if not is_valid_media(p, excluded_exts, excluded_masks):
        return False
    return p.suffix.lower() in SUPPORTED_VIDEO_EXTS


def find_companion_poster(video_path: Path) -> Optional[Path]:
    parent = video_path.parent
    stem_lower = video_path.stem.lower()
    try:
        for child in parent.iterdir():
            if child.is_file() and child.stem.lower() == stem_lower and child.suffix.lower() in SUPPORTED_IMAGE_EXTS:
                return child
    except Exception:
        pass
    return None


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
            ext = orig_path.suffix.lower()
            if ext in SUPPORTED_VIDEO_EXTS:
                poster = find_companion_poster(orig_path)
                if poster and poster.exists():
                    with Image.open(poster) as img:
                        img = ImageOps.exif_transpose(img)
                        if img.mode != "RGB":
                            img = img.convert("RGB")
                        img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                        quality = 85 if target_size == 1200 else 80
                        img.save(cache_file, "WEBP", quality=quality)
                else:
                    meta, _ = extract_video_metadata(orig_path)
                    dur = meta.get("duration", 0.0)
                    if dur > 0 and dur < 4.0:
                        target_ss = max(0.0, dur / 2.0)
                    else:
                        target_ss = 4.0

                    frame_img = None
                    for attempt_ss in [target_ss, 0.0]:
                        try:
                            cmd = [
                                "ffmpeg",
                                "-ss", f"{attempt_ss:.2f}",
                                "-i", str(orig_path),
                                "-vframes", "1",
                                "-f", "image2",
                                "-vcodec", "png",
                                "pipe:1"
                            ]
                            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20)
                            if proc.returncode == 0 and len(proc.stdout) > 0:
                                frame_img = Image.open(BytesIO(proc.stdout))
                                break
                        except Exception:
                            continue

                    if frame_img is not None:
                        frame_img = ImageOps.exif_transpose(frame_img)
                        if frame_img.mode != "RGB":
                            frame_img = frame_img.convert("RGB")
                        frame_img.thumbnail((target_size, target_size), Image.Resampling.LANCZOS)
                        quality = 85 if target_size == 1200 else 80
                        frame_img.save(cache_file, "WEBP", quality=quality)
                    else:
                        raise RuntimeError(f"Could not extract video thumbnail for {orig_path.name}")
            else:
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


def load_video_meta_cache() -> dict:
    global _video_meta_cache
    if _video_meta_cache is not None:
        return _video_meta_cache
    with video_meta_lock:
        if _video_meta_cache is not None:
            return _video_meta_cache
        data = {}
        if VIDEO_META_CACHE_FILE.exists():
            try:
                with open(VIDEO_META_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}
        _video_meta_cache = data
        return _video_meta_cache


def save_video_meta_cache():
    global _video_meta_cache
    with video_meta_lock:
        if _video_meta_cache is None:
            return
        tmp_file = CACHE_DIR / "video_meta_cache.tmp"
        try:
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(_video_meta_cache, f, ensure_ascii=False)
            tmp_file.replace(VIDEO_META_CACHE_FILE)
        except Exception:
            pass


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def extract_video_metadata(video_path: Path) -> tuple[dict, bool]:
    cache = load_video_meta_cache()
    try:
        stat = video_path.stat()
        mtime = int(stat.st_mtime)
        size = stat.st_size
    except Exception:
        mtime = 0
        size = 0

    key = str(video_path)
    cached = cache.get(key)
    if cached and cached.get("mtime") == mtime:
        return cached.get("data", {}), False

    meta = {
        "duration": 0.0,
        "duration_str": "00:00",
        "width": 0,
        "height": 0,
        "resolution": "",
        "video_codec": "",
        "audio_codec": "",
        "container": video_path.suffix.lstrip(".").upper(),
        "size_str": format_file_size(size),
        "sort_date": ""
    }

    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration,size,tags:stream=width,height,codec_name,codec_type",
            "-of", "json",
            str(video_path)
        ]
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=12)
        if proc.returncode == 0:
            parsed = json.loads(proc.stdout)
            fmt = parsed.get("format", {})
            try:
                dur = float(fmt.get("duration", 0) or 0)
            except Exception:
                dur = 0.0
            meta["duration"] = dur
            total_sec = int(round(dur))
            mins = total_sec // 60
            secs = total_sec % 60
            if mins >= 60:
                hours = mins // 60
                mins = mins % 60
                meta["duration_str"] = f"{hours:02d}:{mins:02d}:{secs:02d}"
            else:
                meta["duration_str"] = f"{mins:02d}:{secs:02d}"

            tags = fmt.get("tags", {})
            creation_time = tags.get("creation_time") or tags.get("date")
            if creation_time:
                clean_date = re.sub(r"[T\s]", " ", creation_time[:19]).replace(":", "-", 2)
                meta["sort_date"] = clean_date

            for st in parsed.get("streams", []):
                ctype = st.get("codec_type")
                if ctype == "video" and not meta["video_codec"]:
                    meta["video_codec"] = (st.get("codec_name") or "").upper()
                    w = st.get("width")
                    h = st.get("height")
                    if w and h:
                        meta["width"] = int(w)
                        meta["height"] = int(h)
                        meta["resolution"] = f"{w}×{h}"
                elif ctype == "audio" and not meta["audio_codec"]:
                    meta["audio_codec"] = (st.get("codec_name") or "").upper()
    except Exception:
        pass

    if not meta["sort_date"]:
        try:
            mtime = video_path.stat().st_mtime
            meta["sort_date"] = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(mtime))
        except Exception:
            meta["sort_date"] = ""

    with video_meta_lock:
        cache[key] = {"mtime": mtime, "data": meta}

    return meta, True


def range_file_response(request: Request, file_path: Path, media_type: str = "video/mp4") -> Response:
    try:
        stat = file_path.stat()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="File not found")

    file_size = stat.st_size
    range_header = request.headers.get("range")

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Type": media_type,
        "Cache-Control": "public, max-age=3600",
    }

    def iter_file(path: Path, offset: int, length: int, chunk_size: int = 256 * 1024):
        with open(path, "rb") as f:
            f.seek(offset)
            remaining = length
            while remaining > 0:
                chunk = min(chunk_size, remaining)
                data = f.read(chunk)
                if not data:
                    break
                remaining -= len(data)
                yield data

    if not range_header:
        headers["Content-Length"] = str(file_size)
        return StreamingResponse(
            iter_file(file_path, 0, file_size),
            status_code=200,
            headers=headers
        )

    try:
        range_val = range_header.strip().lower()
        if not range_val.startswith("bytes="):
            raise ValueError()
        parts = range_val[6:].split("-")
        start_str = parts[0].strip()
        end_str = parts[1].strip() if len(parts) > 1 else ""

        if start_str and end_str:
            start = int(start_str)
            end = int(end_str)
        elif start_str:
            start = int(start_str)
            end = file_size - 1
        elif end_str:
            start = max(0, file_size - int(end_str))
            end = file_size - 1
        else:
            raise ValueError()

        if start < 0 or end >= file_size or start > end:
            return Response(
                status_code=416,
                headers={"Content-Range": f"bytes */{file_size}"}
            )
    except Exception:
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{file_size}"}
        )

    content_length = end - start + 1
    headers["Content-Range"] = f"bytes {start}-{end}/{file_size}"
    headers["Content-Length"] = str(content_length)

    return StreamingResponse(
        iter_file(file_path, start, content_length),
        status_code=206,
        headers=headers
    )


def transcode_video_worker(orig_path: Path, output_file: Path, transcode_id: str):
    try:
        temp_output = output_file.with_suffix(".tmp.mp4")
        if temp_output.exists():
            try:
                temp_output.unlink()
            except Exception:
                pass

        cmd = [
            "ffmpeg",
            "-y",
            "-i", str(orig_path),
            "-vf", "scale='min(1920,iw)':-2",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "23",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(temp_output)
        ]
        with video_transcode_lock:
            video_transcode_status[transcode_id] = {"status": "processing", "progress": 10}

        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if proc.returncode == 0 and temp_output.exists() and temp_output.stat().st_size > 0:
            temp_output.replace(output_file)
            with video_transcode_lock:
                video_transcode_status[transcode_id] = {"status": "ready", "progress": 100}
        else:
            with video_transcode_lock:
                err_msg = proc.stderr[-300:] if proc.stderr else "FFmpeg transcode failed"
                video_transcode_status[transcode_id] = {"status": "error", "error": err_msg}
    except Exception as e:
        with video_transcode_lock:
            video_transcode_status[transcode_id] = {"status": "error", "error": str(e)}


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


@app.get("/manifest.json", include_in_schema=False)
async def web_manifest():
    db = load_db()
    title = db.get("settings", {}).get("gallery_title", "PHOTO ARCHIVE")
    manifest = {
        "name": title,
        "short_name": title[:16] if title else "Gallery",
        "description": "Minimalist Photo Archive & Gallery",
        "start_url": "/",
        "display": "standalone",
        "orientation": "any",
        "background_color": "#faf9f6",
        "theme_color": "#121212",
        "icons": [
            {
                "src": "/pixie_gallery.svg",
                "sizes": "any",
                "type": "image/svg+xml",
                "purpose": "any maskable"
            }
        ]
    }
    return JSONResponse(manifest, media_type="application/manifest+json")


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
    ex_exts, ex_masks, hidden_folders = get_filter_rules(db)
    show_hidden = db.get("settings", {}).get("show_hidden_albums", False)
    
    out = []
    for lib in libs:
        lid = lib.get("id", "default")
        if not user_can_access_library(user, lid):
            continue
        
        # Для пользователей проверяем, есть ли доступные альбомы
        if user["role"] != "admin":
            _, lib_root, _ = get_library_root(lid, db)
            albums = scan_all_albums_recursive(
                ex_exts, ex_masks, hidden_folders, show_hidden,
                lib_id=lid, lib_root=lib_root
            )
            has_access = False
            for y in albums:
                for alb in y["albums"]:
                    if user_can_access_album(user, alb["path"], lid):
                        has_access = True
                        break
                if has_access:
                    break
            if not has_access:
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
    global _exif_mem_cache, _video_meta_cache
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
    if VIDEO_CACHE_DIR.exists():
        for f in os.listdir(VIDEO_CACHE_DIR):
            try:
                (VIDEO_CACHE_DIR / f).unlink()
            except Exception:
                pass

    with exif_lock:
        _exif_mem_cache = OrderedDict()
    with video_meta_lock:
        _video_meta_cache = {}
    with video_transcode_lock:
        video_transcode_status.clear()

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
    if not is_valid_media(orig_path, ex_exts, ex_masks):
        raise HTTPException(status_code=404, detail="File not found")
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
                if is_valid_media(p, ex_exts, ex_masks):
                    all_images.append(p)

    with pregen_lock:
        pregen_status["total"] = len(all_images)
        pregen_status["processed"] = 0

    exif_dirty = False
    video_dirty = False
    for idx, img_path in enumerate(all_images, start=1):
        if pregen_status["cancel_requested"]:
            break
        with pregen_lock:
            pregen_status["current_file"] = img_path.name
        try:
            generate_cached_thumb(img_path, 400)
            generate_cached_thumb(img_path, 1200)
            if img_path.suffix.lower() in SUPPORTED_VIDEO_EXTS:
                _, v_up = extract_video_metadata(img_path)
                if v_up:
                    video_dirty = True
            else:
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
    if video_dirty:
        save_video_meta_cache()

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

        all_media = [f for f in current_dir.iterdir() if is_valid_media(f, ex_exts, ex_masks)]
        if not all_media:
            continue

        video_stems = {f.stem.lower() for f in all_media if f.suffix.lower() in SUPPORTED_VIDEO_EXTS}
        valid_items = sorted(
            [f for f in all_media if not (f.suffix.lower() in SUPPORTED_IMAGE_EXTS and f.stem.lower() in video_stems)],
            key=lambda x: x.name
        )
        if valid_items:
            cover_rel = valid_items[0].relative_to(lib_root).as_posix()
            parts = rel_path.split("/")
            year_tag = parts[0] if len(parts) > 0 else "Archive"

            if len(parts) > 1:
                display_name = "/".join(parts[1:])
            else:
                display_name = parts[0]

            photo_count = sum(1 for f in valid_items if f.suffix.lower() in SUPPORTED_IMAGE_EXTS)
            video_count = sum(1 for f in valid_items if f.suffix.lower() in SUPPORTED_VIDEO_EXTS)

            albums_flat.append({
                "name": display_name,
                "path": rel_path,
                "year": year_tag,
                "count": len(valid_items),
                "photo_count": photo_count,
                "video_count": video_count,
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

    all_media = [f for f in target_dir.iterdir() if is_valid_media(f, ex_exts, ex_masks)]
    video_stems = {f.stem.lower() for f in all_media if f.suffix.lower() in SUPPORTED_VIDEO_EXTS}
    valid_items = sorted(
        [f for f in all_media if not (f.suffix.lower() in SUPPORTED_IMAGE_EXTS and f.stem.lower() in video_stems)],
        key=lambda x: x.name
    )

    photos = []
    exif_dirty = False
    video_dirty = False
    q_lib = quote(lib_id)

    for item in valid_items:
        rel = item.relative_to(lib_root).as_posix()
        q_rel = quote(rel)
        ext = item.suffix.lower()
        is_video = ext in SUPPORTED_VIDEO_EXTS
        fav_key = rel if lib_id == "default" else f"{lib_id}::{rel}"

        if is_video:
            meta, updated = extract_video_metadata(item)
            if updated:
                video_dirty = True
            photos.append({
                "name": item.name,
                "path": rel,
                "media_type": "video",
                "is_native_video": ext in SUPPORTED_NATIVE_VIDEO_EXTS,
                "duration": meta.get("duration_str", "00:00"),
                "duration_seconds": meta.get("duration", 0),
                "stream_url": f"/api/video/stream/{q_rel}?lib={q_lib}",
                "thumb": f"/api/thumb/400/{q_rel}?lib={q_lib}",
                "preview": f"/api/thumb/1200/{q_rel}?lib={q_lib}",
                "original": f"/api/original/{q_rel}?lib={q_lib}",
                "is_favorite": fav_key in user_favs,
                "sort_date": meta.get("sort_date", ""),
                "exif": {},
                "video_info": meta if show_exif else {}
            })
        else:
            exif_data, updated = extract_exif_data_cached(item)
            if updated:
                exif_dirty = True
            photos.append({
                "name": item.name,
                "path": rel,
                "media_type": "photo",
                "is_native_video": False,
                "duration": None,
                "duration_seconds": None,
                "stream_url": None,
                "thumb": f"/api/thumb/400/{q_rel}?lib={q_lib}",
                "preview": f"/api/thumb/1200/{q_rel}?lib={q_lib}",
                "original": f"/api/original/{q_rel}?lib={q_lib}",
                "is_favorite": fav_key in user_favs,
                "sort_date": exif_data.get("sort_date", ""),
                "exif": exif_data if show_exif else {},
                "video_info": {}
            })

    if exif_dirty:
        save_exif_cache()
    if video_dirty:
        save_video_meta_cache()

    parts = rel_clean.split("/")
    if len(parts) > 1:
        display_name = "/".join(parts[1:])
    else:
        display_name = parts[0]

    photo_count = sum(1 for p in photos if p["media_type"] == "photo")
    video_count = sum(1 for p in photos if p["media_type"] == "video")

    return {
        "name": display_name,
        "path": rel_clean,
        "lib": lib_id,
        "year": parts[0] if parts else "",
        "count": len(photos),
        "photo_count": photo_count,
        "video_count": video_count,
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

    all_media = [f for f in target_dir.iterdir() if is_valid_media(f, ex_exts, ex_masks)]
    v_stems = {f.stem.lower() for f in all_media if f.suffix.lower() in SUPPORTED_VIDEO_EXTS}
    items = sorted(
        [f for f in all_media if not (f.suffix.lower() in SUPPORTED_IMAGE_EXTS and f.stem.lower() in v_stems)],
        key=lambda x: x.name
    )
    if favorites_only:
        def _is_fav(img_p: Path) -> bool:
            r = img_p.relative_to(lib_root).as_posix()
            fk = r if lib_id == "default" else f"{lib_id}::{r}"
            return fk in user_favs
        items = [img for img in items if _is_fav(img)]
    if not items:
        raise HTTPException(status_code=404, detail="No media files to download")
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        for it in items:
            zf.write(it, arcname=it.name)
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
    if not is_valid_media(orig_path):
        raise HTTPException(status_code=404, detail="File not found")
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
    if not is_valid_media(orig_path):
        raise HTTPException(status_code=404, detail="File not found")
    if download:
        return FileResponse(orig_path, filename=orig_path.name)
    safe_name = quote(orig_path.name)
    return FileResponse(
        orig_path,
        headers={"Content-Disposition": f"inline; filename*=UTF-8''{safe_name}"}
    )


@app.get("/api/video/stream/{video_path:path}")
def stream_video(video_path: str, request: Request, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    rel_clean = unquote(video_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    if not is_valid_media(orig_path) or orig_path.suffix.lower() not in SUPPORTED_VIDEO_EXTS:
        raise HTTPException(status_code=404, detail="Video not found")

    ext = orig_path.suffix.lower()
    target_stream_file = orig_path
    media_type = "video/mp4"

    if ext in SUPPORTED_NATIVE_VIDEO_EXTS:
        if ext == ".webm":
            media_type = "video/webm"
        elif ext == ".mov":
            media_type = "video/quicktime"
        else:
            media_type = "video/mp4"
    else:
        stat = orig_path.stat()
        cache_key = f"{orig_path}_{stat.st_mtime}_1080p_v1"
        hash_str = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
        transcoded_file = VIDEO_CACHE_DIR / f"{hash_str}.mp4"
        if transcoded_file.exists() and transcoded_file.stat().st_size > 0:
            target_stream_file = transcoded_file
            media_type = "video/mp4"
        else:
            raise HTTPException(status_code=422, detail="Video needs transcoding")

    return range_file_response(request, target_stream_file, media_type)


@app.get("/api/video/status/{video_path:path}")
def get_video_transcode_status(video_path: str, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    rel_clean = unquote(video_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    if not is_valid_media(orig_path):
        raise HTTPException(status_code=404, detail="File not found")

    ext = orig_path.suffix.lower()
    if ext in SUPPORTED_NATIVE_VIDEO_EXTS:
        return {"status": "ready", "is_native": True}

    stat = orig_path.stat()
    cache_key = f"{orig_path}_{stat.st_mtime}_1080p_v1"
    hash_str = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
    transcoded_file = VIDEO_CACHE_DIR / f"{hash_str}.mp4"

    if transcoded_file.exists() and transcoded_file.stat().st_size > 0:
        return {"status": "ready", "is_native": False}

    with video_transcode_lock:
        state = video_transcode_status.get(hash_str)
        if state:
            return {"status": state.get("status", "idle"), "is_native": False, "error": state.get("error")}

    return {"status": "needs_transcoding", "is_native": False}


@app.post("/api/video/transcode/{video_path:path}")
def trigger_video_transcode(video_path: str, background_tasks: BackgroundTasks, lib: Optional[str] = "default", user: dict = Depends(get_current_user)):
    rel_clean = unquote(video_path).strip("/")
    db = load_db()
    lib_id, _, _ = get_library_root(lib, db)
    if not user_can_access_image(user, rel_clean, lib_id):
        raise HTTPException(status_code=403, detail="Access denied")
    orig_path = safe_resolve(rel_clean, lib_id, db)
    if not is_valid_media(orig_path):
        raise HTTPException(status_code=404, detail="File not found")

    stat = orig_path.stat()
    cache_key = f"{orig_path}_{stat.st_mtime}_1080p_v1"
    hash_str = hashlib.md5(cache_key.encode("utf-8")).hexdigest()
    transcoded_file = VIDEO_CACHE_DIR / f"{hash_str}.mp4"

    if transcoded_file.exists() and transcoded_file.stat().st_size > 0:
        return {"status": "ready", "is_native": False}

    with video_transcode_lock:
        state = video_transcode_status.get(hash_str)
        if state and state.get("status") == "processing":
            return {"status": "processing", "is_native": False}
        video_transcode_status[hash_str] = {"status": "processing", "progress": 5}

    background_tasks.add_task(transcode_video_worker, orig_path, transcoded_file, hash_str)
    return {"status": "started", "is_native": False}
