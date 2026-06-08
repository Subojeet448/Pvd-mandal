#!/usr/bin/env python3
"""
Universal Media Downloader — FastAPI Edition
Developer: MANDAL !!
Version: 3.3 — YouTube SABR Fix (2026) + android_vr client
"""

import os, re, shutil, logging, asyncio, tempfile, time, ipaddress, urllib.parse, subprocess, sys
from pathlib import Path
from contextlib import asynccontextmanager

import yt_dlp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import StreamingResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.middleware.cors import CORSMiddleware

logging.basicConfig(level=logging.INFO, format="%(asctime)s — %(levelname)s — %(message)s")
logger = logging.getLogger(__name__)


def _update_ytdlp():
    try:
        logger.info("yt-dlp update check kar raha hai...")
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp", "-q"],
            capture_output=True, text=True, timeout=60
        )
        if result.returncode == 0:
            logger.info("yt-dlp update ho gaya")
        else:
            logger.warning(f"yt-dlp update fail: {result.stderr[:200]}")
    except Exception as e:
        logger.warning(f"yt-dlp update skip: {e}")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _update_ytdlp)
    yield


app = FastAPI(title="MANDAL Downloader", version="3.3", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

# ── Cookie file path ──────────────────────────────────────────────────────────
BASE_DIR    = Path(__file__).parent
COOKIE_FILE = BASE_DIR / "youtube_cookies.txt"

COMMON_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Stats tracking ────────────────────────────────────────────────────────────
_stats = {"total_downloads": 0, "total_bytes": 0, "start_time": time.time()}


# ══════════════════════════════════════════════════════════════════════════════
#  SSRF PROTECTION
# ══════════════════════════════════════════════════════════════════════════════
def _is_safe_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        if scheme not in ("http", "https"):
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            pass
        blocked = ("localhost", "metadata.google.internal", "169.254.169.254")
        if hostname.lower() in blocked:
            return False
        return True
    except Exception:
        return False


def require_safe_url(url: str):
    if not _is_safe_url(url):
        raise HTTPException(status_code=400, detail="Invalid or disallowed URL.")


# ══════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def clean_filename(name: str) -> str:
    name = re.sub(r"@\w+", "", name)
    name = re.sub(r"https?://\S+", "", name)
    name = re.sub(r"[^\w\s\-]", " ", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name[:80] if name else "video"


def _is_youtube(url: str) -> bool:
    return "youtube.com" in url or "youtu.be" in url


def _base_opts(extra: dict | None = None) -> dict:
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "http_headers": COMMON_HEADERS,
    }
    if extra:
        opts.update(extra)
    return opts


def _maybe_add_cookies(opts: dict, url: str) -> dict:
    if _is_youtube(url) and COOKIE_FILE.exists():
        opts["cookiefile"] = str(COOKIE_FILE)
        logger.info(f"Cookies loaded from: {COOKIE_FILE}")
    elif _is_youtube(url):
        logger.warning(f"Cookie file nahi mila: {COOKIE_FILE} — YouTube bina cookies ke try karega")
    return opts


# ══════════════════════════════════════════════════════════════════════════════
#  YouTube 2026 SABR Fix — extractor args
# ══════════════════════════════════════════════════════════════════════════════
def _yt_extractor_args() -> dict:
    """
    YouTube 2026 SABR bypass:
    - android_vr: SABR restriction nahi hoti, high quality formats milti hain
    - tv_embedded: reliable fallback
    - android: combined formats (360p/720p) — no merge needed
    - missing_pot: PO Token na ho toh bhi formats try karo
    - skip: dash aur hls skip karo (SABR DASH formats problem dete hain)
    """
    return {
        "youtube": {
            "player_client": ["android_vr", "tv_embedded", "android", "web"],
            "formats": "missing_pot",
            "skip": ["hls"],
        }
    }


# ══════════════════════════════════════════════════════════════════════════════
#  /info
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/info")
async def get_info(url: str = Query(...)):
    require_safe_url(url)
    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _fetch_formats, url)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Could not fetch info"))
    return JSONResponse(result)


def _fetch_formats(url: str) -> dict:
    extra = {"retries": 3}
    if _is_youtube(url):
        extra["extractor_args"] = _yt_extractor_args()

    opts = _maybe_add_cookies(_base_opts(extra), url)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        title       = info.get("title", "video")
        duration    = info.get("duration", 0)
        thumbnail   = info.get("thumbnail")
        uploader    = info.get("uploader", "")
        view_count  = info.get("view_count", 0)
        like_count  = info.get("like_count", 0)
        description = (info.get("description") or "")[:300]
        raw_fmts    = info.get("formats", [])

        seen, quality_list = set(), []
        for f in reversed(raw_fmts):
            h = f.get("height")
            if not h or f.get("vcodec", "none") == "none":
                continue
            fs = f.get("filesize") or f.get("filesize_approx") or 0
            if h not in seen:
                seen.add(h)
                quality_list.append({
                    "height": h, "ext": f.get("ext", "mp4"),
                    "filesize": fs, "has_audio": f.get("acodec", "none") != "none",
                    "label": f"{h}p", "quality": str(h),
                })

        quality_list.sort(key=lambda x: x["height"], reverse=True)

        # Agar koi format nahi mila toh default list do
        if not quality_list:
            for h in [1080, 720, 480, 360, 240]:
                quality_list.append({
                    "height": h, "ext": "mp4", "filesize": 0,
                    "has_audio": True, "label": f"{h}p", "quality": str(h),
                })

        quality_list.append({
            "height": 0, "ext": "mp3", "filesize": 0,
            "has_audio": True, "label": "MP3 🎵", "quality": "audio",
        })

        return {
            "ok": True, "title": title, "uploader": uploader,
            "duration": duration, "thumbnail": thumbnail,
            "view_count": view_count, "like_count": like_count,
            "description": description, "formats": quality_list,
        }
    except Exception as e:
        logger.error(f"_fetch_formats error: {e}")
        return {"ok": False, "error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  /download
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/download")
async def download_video(
    url:     str = Query(...),
    quality: str = Query("720"),
):
    require_safe_url(url)
    loop    = asyncio.get_event_loop()
    tmp_dir = tempfile.mkdtemp(prefix="dlr_")
    try:
        filepath, err, title, _ = await loop.run_in_executor(
            None, _blocking_download, url, tmp_dir, quality)
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

    if err or not filepath:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=f"Download failed: {err}")

    path_obj   = Path(filepath)
    file_size  = path_obj.stat().st_size
    ext        = path_obj.suffix.lstrip(".")
    safe_name  = clean_filename(title or "download")
    dl_name    = f"{safe_name}.{ext}"
    media_type = "audio/mpeg" if ext == "mp3" else "video/mp4"

    _stats["total_downloads"] += 1
    _stats["total_bytes"]     += file_size

    def iter_file():
        try:
            with open(filepath, "rb") as f:
                while chunk := f.read(1024 * 1024):
                    yield chunk
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    headers = {
        "Content-Disposition": f'attachment; filename="{dl_name}"',
        "Content-Length": str(file_size),
        "Accept-Ranges": "bytes",
    }
    return StreamingResponse(iter_file(), media_type=media_type, headers=headers)


def _blocking_download(url: str, download_dir: str, quality: str):
    extract_audio = (quality == "audio")
    is_yt = _is_youtube(url)

    base = {
        "outtmpl": f"{download_dir}/%(title)s.%(ext)s",
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        # FFmpeg reconnect — SABR streams ke liye zaroori
        "external_downloader_args": {
            "ffmpeg_i": [
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
            ]
        },
    }

    if is_yt:
        base["extractor_args"] = _yt_extractor_args()

    ydl_opts = _maybe_add_cookies(_base_opts(base), url)

    if extract_audio:
        ydl_opts.update({
            "format": "bestaudio[ext=m4a]/bestaudio/best",
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
        })
    else:
        h = int(quality)
        if is_yt:
            # YouTube 2026 SABR fix format chain:
            # 1. android_vr se combined mp4 — sabse reliable (no SABR)
            # 2. best combined mp4 — simple, no merge
            # 3. best combined any ext
            # 4. bestvideo+bestaudio merge — ffmpeg required
            # 5. last resort: best available
            ydl_opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"   # best merge
                f"/best[height<={h}][ext=mp4]"                           # combined mp4
                f"/best[height<={h}]"                                    # combined any
                f"/bestvideo[height<={h}]+bestaudio"                     # merge any
                f"/best"                                                  # last resort
            )
        else:
            ydl_opts["format"] = (
                f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={h}]+bestaudio"
                f"/best[height<={h}]"
                f"/best"
            )
        ydl_opts["merge_output_format"] = "mp4"
        ydl_opts["postprocessors"] = [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}]

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)

        title         = info.get("title", "video")
        thumbnail_url = info.get("thumbnail")

        all_files   = list(Path(download_dir).glob("*"))
        media_files = [f for f in all_files
                       if f.suffix.lower() not in (".jpg", ".jpeg", ".png", ".webp", ".part")]
        chosen = media_files[0] if media_files else (all_files[0] if all_files else None)

        if chosen:
            real_size = os.path.getsize(str(chosen))
            if real_size > 4 * 1024**3:
                return None, "file_too_large", title, None
            return str(chosen), None, title, thumbnail_url
        return None, "no_file_found", title, None

    except yt_dlp.utils.DownloadError as e:
        err_str = str(e)
        logger.error(f"yt-dlp DownloadError: {err_str}")
        if "Sign in to confirm" in err_str or "cookies" in err_str.lower():
            return None, "YouTube cookies expire ho gayi hain. Cookies refresh karo.", None, None
        if "Private video" in err_str:
            return None, "Yeh video private hai.", None, None
        if "Video unavailable" in err_str:
            return None, "Video unavailable hai.", None, None
        if "Requested format is not available" in err_str:
            return None, "Yeh quality available nahi. Doosri quality try karo.", None, None
        return None, err_str, None, None
    except Exception as e:
        logger.error(f"_blocking_download error: {e}")
        return None, str(e), None, None


# ══════════════════════════════════════════════════════════════════════════════
#  /stream
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/stream")
async def stream_url(
    url:     str = Query(...),
    quality: str = Query("720"),
):
    require_safe_url(url)
    loop = asyncio.get_event_loop()
    try:
        direct = await loop.run_in_executor(None, _get_direct_url, url, quality)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    if not direct:
        raise HTTPException(status_code=400, detail="Stream URL nahi mili")

    return RedirectResponse(url=direct, status_code=302)


def _get_direct_url(url: str, quality: str) -> str | None:
    extract_audio = (quality == "audio")
    if extract_audio:
        fmt = "bestaudio[ext=m4a]/bestaudio/best"
    else:
        h   = int(quality) if quality.isdigit() else 720
        fmt = (
            f"best[height<={h}][ext=mp4]"
            f"/best[height<={h}]"
            f"/best"
        )

    extra = {"format": fmt}
    if _is_youtube(url):
        extra["extractor_args"] = _yt_extractor_args()

    opts = _maybe_add_cookies(_base_opts(extra), url)

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if "requested_formats" in info:
                return info["requested_formats"][0].get("url")
            return info.get("url")
    except Exception as e:
        logger.error(f"_get_direct_url error: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
#  /health  /ping  /stats
# ══════════════════════════════════════════════════════════════════════════════
@app.get("/health")
async def health():
    uptime = int(time.time() - _stats["start_time"])
    cookie_ok = COOKIE_FILE.exists()
    return JSONResponse({
        "status": "ok",
        "service": "MANDAL Downloader",
        "version": "3.3",
        "uptime_seconds": uptime,
        "total_downloads": _stats["total_downloads"],
        "total_bytes_served": _stats["total_bytes"],
        "youtube_cookies": "loaded" if cookie_ok else "missing",
    })


@app.get("/ping")
async def ping():
    return JSONResponse({"ping": "pong", "ts": int(time.time())})


@app.get("/stats")
async def stats():
    uptime = int(time.time() - _stats["start_time"])
    return JSONResponse({
        "uptime_seconds": uptime,
        "total_downloads": _stats["total_downloads"],
        "total_bytes": _stats["total_bytes"],
    })


# ══════════════════════════════════════════════════════════════════════════════
#  HTML UI  — same as original, version badge updated to 3.3
# ══════════════════════════════════════════════════════════════════════════════
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="dark">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>MANDAL Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Bebas+Neue&family=Space+Mono:wght@400;700&family=Sora:wght@300;400;500;600;700&display=swap" rel="stylesheet"/>
<style>
:root{--r:14px;--ease:cubic-bezier(.4,0,.2,1);--sidebar-w:300px;}
[data-theme="dark"]{--bg:#080810;--bg2:#0e0e1a;--surface:#141420;--card:#1a1a28;--card2:#20203a;--border:#2c2c45;--border2:#3a3a58;--accent:#e040fb;--accent2:#00e5ff;--green:#00e676;--gold:#ffb300;--red:#ff5252;--text:#eeeef8;--text2:#a0a0c0;--muted:#55556a;--shadow:rgba(0,0,0,.7);--overlay:rgba(8,8,16,.85);}
[data-theme="light"]{--bg:#f0f0f8;--bg2:#e8e8f4;--surface:#ffffff;--card:#f8f8ff;--card2:#ededff;--border:#d0d0e8;--border2:#b8b8d8;--accent:#9c27b0;--accent2:#0097a7;--green:#2e7d32;--gold:#e65100;--red:#c62828;--text:#1a1a2e;--text2:#44446a;--muted:#8888a8;--shadow:rgba(0,0,0,.15);--overlay:rgba(240,240,248,.88);}
*{box-sizing:border-box;margin:0;padding:0;transition:background-color .3s var(--ease),border-color .3s var(--ease),color .2s var(--ease)}
html{scroll-behavior:smooth}
body{font-family:'Sora',sans-serif;background:var(--bg);color:var(--text);min-height:100vh;overflow-x:hidden;}
.glow-bg{position:fixed;inset:0;pointer-events:none;z-index:0;background:radial-gradient(ellipse 70% 55% at 15% 0%,rgba(224,64,251,.12) 0%,transparent 65%),radial-gradient(ellipse 55% 45% at 85% 100%,rgba(0,229,255,.10) 0%,transparent 65%),radial-gradient(ellipse 40% 35% at 55% 50%,rgba(0,230,118,.05) 0%,transparent 60%);transition:none;}
.topbar{position:fixed;top:0;left:0;right:0;z-index:100;height:62px;display:flex;align-items:center;justify-content:space-between;padding:0 20px;background:var(--bg2);border-bottom:1px solid var(--border);backdrop-filter:blur(20px);}
.hamburger{display:flex;flex-direction:column;justify-content:center;gap:5px;width:40px;height:40px;cursor:pointer;border-radius:10px;padding:8px;border:none;background:transparent;}
.hamburger span{display:block;height:2px;border-radius:2px;background:var(--text2);transition:transform .35s var(--ease),opacity .25s,width .3s var(--ease),background .2s;}
.hamburger span:nth-child(2){width:70%}
.hamburger:hover span{background:var(--accent)}
.hamburger.open span:nth-child(1){transform:translateY(7px) rotate(45deg)}
.hamburger.open span:nth-child(2){opacity:0;width:0}
.hamburger.open span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
.topbar-title{font-family:'Bebas Neue',sans-serif;font-size:26px;letter-spacing:3px;background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.topbar-right{display:flex;align-items:center;gap:8px}
.ping-dot{width:9px;height:9px;border-radius:50%;background:var(--muted);box-shadow:0 0 0 0 rgba(0,230,118,0);transition:background .4s;flex-shrink:0;}
.ping-dot.online{background:var(--green);animation:pulse-green 2s infinite;}
.ping-dot.offline{background:var(--red)}
@keyframes pulse-green{0%{box-shadow:0 0 0 0 rgba(0,230,118,.6)}70%{box-shadow:0 0 0 7px rgba(0,230,118,0)}100%{box-shadow:0 0 0 0 rgba(0,230,118,0)}}
.topbar-stats{display:flex;align-items:center;gap:6px;font-family:'Space Mono',monospace;font-size:10px;color:var(--text2);background:var(--card);border:1px solid var(--border);border-radius:99px;padding:4px 10px;cursor:default;}
.settings-btn{width:40px;height:40px;display:flex;align-items:center;justify-content:center;border:none;border-radius:10px;background:var(--card);border:1px solid var(--border);cursor:pointer;color:var(--text2);font-size:19px;transition:all .2s;}
.settings-btn:hover{background:var(--card2);color:var(--accent);border-color:var(--accent);transform:rotate(30deg)}
.settings-panel{position:fixed;top:70px;right:16px;z-index:200;width:260px;background:var(--surface);border:1px solid var(--border2);border-radius:16px;box-shadow:0 20px 60px var(--shadow);padding:8px;transform:translateY(-12px) scale(.95);opacity:0;pointer-events:none;transition:all .25s var(--ease);}
.settings-panel.open{transform:translateY(0) scale(1);opacity:1;pointer-events:all}
.settings-head{font-size:11px;letter-spacing:2px;text-transform:uppercase;color:var(--muted);padding:10px 12px 6px;font-family:'Space Mono',monospace;}
.setting-row{display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border-radius:10px;cursor:pointer;user-select:none;}
.setting-row:hover{background:var(--card)}
.setting-label{display:flex;align-items:center;gap:10px;font-size:14px;font-weight:500}
.setting-label svg{opacity:.7}
.toggle{width:44px;height:24px;background:var(--border);border-radius:99px;position:relative;cursor:pointer;flex-shrink:0;transition:background .3s;}
.toggle::after{content:'';position:absolute;top:3px;left:3px;width:18px;height:18px;border-radius:50%;background:#fff;transition:transform .3s var(--ease),background .3s;box-shadow:0 1px 4px rgba(0,0,0,.3);}
.toggle.on{background:var(--accent)}
.toggle.on::after{transform:translateX(20px)}
.settings-divider{height:1px;background:var(--border);margin:4px 8px}
.uptime-info{padding:10px 12px;font-family:'Space Mono',monospace;font-size:11px;color:var(--text2);}
.uptime-row{display:flex;justify-content:space-between;margin-bottom:4px}
.uptime-val{color:var(--accent2)}
.sidebar-overlay{position:fixed;inset:0;z-index:149;background:var(--overlay);opacity:0;pointer-events:none;transition:opacity .35s var(--ease);backdrop-filter:blur(4px);}
.sidebar-overlay.open{opacity:1;pointer-events:all}
.sidebar{position:fixed;top:0;left:0;bottom:0;z-index:150;width:var(--sidebar-w);background:var(--bg2);border-right:1px solid var(--border);display:flex;flex-direction:column;transform:translateX(-100%);transition:transform .38s var(--ease);will-change:transform;}
.sidebar.open{transform:translateX(0)}
.sidebar-header{display:flex;align-items:center;justify-content:space-between;padding:20px 18px 14px;border-bottom:1px solid var(--border);flex-shrink:0;}
.sidebar-title{font-size:13px;font-weight:600;letter-spacing:2px;text-transform:uppercase;color:var(--text2);font-family:'Space Mono',monospace;}
.sidebar-close{width:32px;height:32px;border:none;border-radius:8px;background:var(--card);color:var(--text2);cursor:pointer;font-size:16px;display:flex;align-items:center;justify-content:center;}
.sidebar-close:hover{background:var(--red);color:#fff}
.sidebar-search{padding:10px 12px;border-bottom:1px solid var(--border);flex-shrink:0;}
.sidebar-search input{width:100%;background:var(--card);border:1px solid var(--border);border-radius:8px;padding:8px 12px;font-size:12px;color:var(--text);font-family:'Space Mono',monospace;outline:none;}
.sidebar-search input:focus{border-color:var(--accent)}
.sidebar-search input::placeholder{color:var(--muted)}
.sidebar-list{flex:1;overflow-y:auto;padding:10px 10px 20px;scrollbar-width:thin;scrollbar-color:var(--border) transparent;}
.sidebar-list::-webkit-scrollbar{width:4px}
.sidebar-list::-webkit-scrollbar-thumb{background:var(--border);border-radius:4px}
.history-empty{text-align:center;padding:40px 20px;color:var(--muted);font-size:13px;font-family:'Space Mono',monospace;line-height:1.8;}
.hist-item{background:var(--card);border:1px solid var(--border);border-radius:12px;margin-bottom:10px;overflow:hidden;transition:border-color .2s,transform .2s;animation:fadeIn .3s var(--ease);}
.hist-item:hover{border-color:var(--border2);transform:translateX(3px)}
.hist-thumb-row{display:flex;gap:10px;padding:10px;align-items:flex-start}
.hist-thumb{width:68px;height:42px;border-radius:7px;object-fit:cover;flex-shrink:0;background:var(--card2);}
.hist-no-thumb{width:68px;height:42px;border-radius:7px;flex-shrink:0;background:var(--card2);display:flex;align-items:center;justify-content:center;font-size:20px;}
.hist-info{flex:1;min-width:0}
.hist-name{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;color:var(--text);margin-bottom:3px;}
.hist-meta{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace}
.hist-actions{display:flex;gap:6px;padding:0 10px 10px;}
.hist-btn{flex:1;padding:6px 4px;font-size:11px;font-weight:600;border:none;border-radius:7px;cursor:pointer;font-family:'Space Mono',monospace;display:flex;align-items:center;justify-content:center;gap:4px;transition:all .18s;letter-spacing:.5px;}
.hist-btn.dl{background:rgba(0,230,118,.12);color:var(--green);border:1px solid rgba(0,230,118,.2)}
.hist-btn.dl:hover{background:var(--green);color:#000}
.hist-btn.ren{background:rgba(0,229,255,.1);color:var(--accent2);border:1px solid rgba(0,229,255,.2)}
.hist-btn.ren:hover{background:var(--accent2);color:#000}
.hist-btn.del{background:rgba(255,82,82,.1);color:var(--red);border:1px solid rgba(255,82,82,.2)}
.hist-btn.del:hover{background:var(--red);color:#fff}
.sidebar-clear{flex-shrink:0;padding:12px;border-top:1px solid var(--border);}
.clear-all-btn{width:100%;padding:10px;border:1px solid rgba(255,82,82,.3);border-radius:10px;background:rgba(255,82,82,.06);color:var(--red);font-size:12px;font-weight:600;cursor:pointer;font-family:'Space Mono',monospace;letter-spacing:1px;transition:all .2s;}
.clear-all-btn:hover{background:var(--red);color:#fff;border-color:var(--red)}
.main{position:relative;z-index:1;padding:90px 20px 80px;min-height:100vh;display:flex;flex-direction:column;align-items:center;}
.hero{text-align:center;margin-bottom:36px}
.hero-badge{display:inline-block;font-family:'Space Mono',monospace;font-size:10px;letter-spacing:3px;text-transform:uppercase;color:var(--accent);background:rgba(224,64,251,.1);border:1px solid rgba(224,64,251,.25);padding:5px 14px;border-radius:99px;margin-bottom:14px;}
.hero-title{font-family:'Bebas Neue',sans-serif;font-size:clamp(44px,9vw,80px);letter-spacing:5px;line-height:1;color:var(--text);}
.hero-title span{background:linear-gradient(135deg,var(--accent) 20%,var(--accent2) 80%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;}
.hero-sub{font-size:13px;color:var(--text2);margin-top:10px;font-weight:300;letter-spacing:.5px;}
.stats-bar{display:flex;gap:12px;justify-content:center;flex-wrap:wrap;margin-bottom:24px;}
.stat-chip{display:flex;align-items:center;gap:6px;font-family:'Space Mono',monospace;font-size:11px;color:var(--text2);background:var(--card);border:1px solid var(--border);border-radius:99px;padding:5px 14px;}
.stat-chip .sv{color:var(--accent2);font-weight:700}
.dl-card{width:100%;max-width:640px;background:var(--surface);border:1px solid var(--border);border-radius:20px;padding:24px;box-shadow:0 8px 40px var(--shadow);}
.url-row{display:flex;gap:10px;margin-bottom:14px}
.url-wrap{flex:1;display:flex;align-items:center;gap:10px;background:var(--bg2);border:1.5px solid var(--border);border-radius:12px;padding:0 14px;transition:border-color .2s;}
.url-wrap:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px rgba(224,64,251,.1)}
.url-icon{font-size:16px;opacity:.5;flex-shrink:0}
#urlInput{flex:1;background:none;border:none;outline:none;font-family:'Space Mono',monospace;font-size:13px;color:var(--text);padding:14px 0;}
#urlInput::placeholder{color:var(--muted)}
.paste-btn{background:var(--card);border:1px solid var(--border);border-radius:8px;padding:5px 10px;font-size:11px;color:var(--text2);cursor:pointer;font-family:'Space Mono',monospace;flex-shrink:0;transition:all .18s;}
.paste-btn:hover{border-color:var(--accent2);color:var(--accent2)}
.fetch-btn{padding:0 22px;height:52px;background:linear-gradient(135deg,var(--accent),#8b00cc);border:none;border-radius:12px;color:#fff;font-family:'Sora',sans-serif;font-weight:700;font-size:14px;cursor:pointer;flex-shrink:0;display:flex;align-items:center;gap:8px;transition:opacity .2s,transform .15s,box-shadow .2s;box-shadow:0 4px 20px rgba(224,64,251,.35);white-space:nowrap;}
.fetch-btn:hover{opacity:.9;transform:translateY(-2px);box-shadow:0 8px 28px rgba(224,64,251,.45)}
.fetch-btn:active{transform:translateY(0)}
.fetch-btn:disabled{opacity:.4;cursor:not-allowed;transform:none;box-shadow:none}
.platforms{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px;}
.plat{font-size:11px;font-family:'Space Mono',monospace;color:var(--text2);background:var(--bg2);border:1px solid var(--border);padding:4px 10px;border-radius:99px;cursor:pointer;transition:all .2s;}
.plat:hover{border-color:var(--accent2);color:var(--accent2)}
#statusBar{font-family:'Space Mono',monospace;font-size:12px;color:var(--text2);text-align:center;min-height:20px;margin:10px 0;display:flex;align-items:center;justify-content:center;gap:8px;}
#statusBar.err{color:var(--red)}
#statusBar.ok{color:var(--green)}
#infoCard{display:none;border-top:1px solid var(--border);margin-top:18px;padding-top:20px;animation:fadeIn .4s var(--ease);}
@keyframes fadeIn{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:translateY(0)}}
.video-row{display:flex;gap:14px;margin-bottom:18px}
.vid-info{flex:1;min-width:0;padding-top:2px}
.vid-title{font-size:14px;font-weight:600;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;margin-bottom:6px;}
.vid-meta{font-family:'Space Mono',monospace;font-size:11px;color:var(--text2);}
.vid-extra{display:flex;gap:8px;margin-top:8px;flex-wrap:wrap;}
.vid-chip{font-size:10px;font-family:'Space Mono',monospace;color:var(--text2);background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:3px 8px;}
.prog-bar{height:3px;border-radius:3px;background:var(--border);margin-bottom:16px;overflow:hidden;}
.prog-fill{height:100%;width:0%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:3px;transition:width .4s var(--ease);}
.qlabel{font-size:10px;letter-spacing:2.5px;text-transform:uppercase;color:var(--muted);font-family:'Space Mono',monospace;margin-bottom:10px;}
.quality-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(105px,1fr));gap:8px;}
.qbtn{background:var(--card);border:1.5px solid var(--border);border-radius:11px;padding:10px 8px;cursor:pointer;text-align:center;transition:all .2s var(--ease);position:relative;overflow:hidden;color:var(--text);}
.qbtn::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--accent),var(--accent2));opacity:0;transition:opacity .2s;}
.qbtn:hover{border-color:var(--accent2);transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,229,255,.15)}
.qbtn:hover::before{opacity:.08}
.qbtn.audio{border-color:rgba(255,179,0,.3)}
.qbtn.audio:hover{border-color:var(--gold);box-shadow:0 6px 20px rgba(255,179,0,.15)}
.qbtn.loading{opacity:.5;cursor:not-allowed;transform:none !important;animation:blink .8s infinite}
@keyframes blink{0%,100%{opacity:.5}50%{opacity:.25}}
.qbtn-label{font-size:13px;font-weight:700;position:relative;display:flex;align-items:center;justify-content:center;gap:5px;}
.qbtn-size{display:block;font-size:10px;color:var(--muted);font-family:'Space Mono',monospace;margin-top:3px;position:relative;}
.modal-overlay{position:fixed;inset:0;z-index:300;background:var(--overlay);backdrop-filter:blur(8px);display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .25s;}
.modal-overlay.open{opacity:1;pointer-events:all}
.modal-box{width:90%;max-width:380px;background:var(--surface);border:1px solid var(--border2);border-radius:18px;padding:26px;box-shadow:0 30px 80px var(--shadow);transform:scale(.9);transition:transform .25s var(--ease);}
.modal-overlay.open .modal-box{transform:scale(1)}
.modal-title{font-size:16px;font-weight:700;margin-bottom:4px}
.modal-sub{font-size:12px;color:var(--muted);margin-bottom:18px;font-family:'Space Mono',monospace}
.modal-input{width:100%;background:var(--bg2);border:1.5px solid var(--border);border-radius:10px;padding:12px 14px;font-family:'Sora',sans-serif;font-size:14px;color:var(--text);outline:none;margin-bottom:16px;transition:border-color .2s;}
.modal-input:focus{border-color:var(--accent)}
.modal-btns{display:flex;gap:8px}
.modal-btn{flex:1;padding:11px;border:none;border-radius:10px;font-size:13px;font-weight:600;cursor:pointer;font-family:'Sora',sans-serif;transition:all .2s;}
.modal-btn.cancel{background:var(--card);color:var(--text2)}
.modal-btn.cancel:hover{background:var(--card2)}
.modal-btn.save{background:linear-gradient(135deg,var(--accent2),#0077ff);color:#fff}
.modal-btn.save:hover{opacity:.88;transform:translateY(-1px)}
.spin{display:inline-block;width:13px;height:13px;border:2px solid rgba(255,255,255,.25);border-top-color:currentColor;border-radius:50%;animation:spinning .65s linear infinite;}
@keyframes spinning{to{transform:rotate(360deg)}}
footer{position:relative;z-index:1;text-align:center;font-family:'Space Mono',monospace;font-size:10px;color:var(--muted);padding:20px;letter-spacing:1px;}
footer span{color:var(--accent)}
.player-overlay{position:fixed;inset:0;z-index:400;background:rgba(0,0,0,.93);backdrop-filter:blur(14px);display:flex;align-items:center;justify-content:center;opacity:0;pointer-events:none;transition:opacity .3s cubic-bezier(.4,0,.2,1);padding:16px;}
.player-overlay.open{opacity:1;pointer-events:all}
.player-box{width:100%;max-width:860px;background:var(--surface);border:1px solid var(--border2);border-radius:20px;overflow:hidden;box-shadow:0 40px 120px rgba(0,0,0,.95);transform:scale(.88) translateY(30px);transition:transform .38s cubic-bezier(.4,0,.2,1);}
.player-overlay.open .player-box{transform:scale(1) translateY(0)}
.player-header{display:flex;align-items:center;justify-content:space-between;padding:14px 18px;border-bottom:1px solid var(--border);gap:10px;}
.player-title-text{font-size:13px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;color:var(--text);}
.player-header-right{display:flex;align-items:center;gap:8px;flex-shrink:0}
.player-ext-btn{font-size:11px;font-family:'Space Mono',monospace;padding:5px 10px;border-radius:7px;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text2);transition:all .18s;text-decoration:none;display:flex;align-items:center;gap:4px;}
.player-ext-btn:hover{background:var(--card2);border-color:var(--accent2);color:var(--accent2)}
.player-close{width:32px;height:32px;border:none;border-radius:8px;background:rgba(255,82,82,.15);color:var(--red);cursor:pointer;font-size:18px;line-height:1;display:flex;align-items:center;justify-content:center;transition:all .2s;flex-shrink:0;}
.player-close:hover{background:var(--red);color:#fff}
.player-screen{position:relative;width:100%;aspect-ratio:16/9;background:#000;}
.player-screen iframe,.player-screen video{width:100%;height:100%;border:none;display:block;}
.player-loading{position:absolute;inset:0;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;color:var(--text2);font-family:'Space Mono',monospace;font-size:12px;text-align:center;padding:20px;}
.player-footer{padding:12px 18px;display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;border-top:1px solid var(--border);}
.player-qual-row{display:flex;gap:6px;flex-wrap:wrap}
.pqbtn{font-size:11px;font-family:'Space Mono',monospace;padding:5px 12px;border-radius:7px;cursor:pointer;border:1px solid var(--border);background:var(--card);color:var(--text2);transition:all .18s;}
.pqbtn:hover,.pqbtn.active{background:var(--accent);color:#fff;border-color:var(--accent)}
.player-note{font-size:10px;color:var(--muted);font-family:'Space Mono',monospace}
.vid-thumb-wrap{position:relative;cursor:pointer;flex-shrink:0}
.vid-thumb{width:120px;height:70px;border-radius:10px;object-fit:cover;display:block;background:var(--card2);transition:filter .25s,transform .25s;}
.vid-thumb-wrap:hover .vid-thumb{filter:brightness(.5);transform:scale(1.04)}
.thumb-play-icon{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;font-size:30px;opacity:0;transition:opacity .2s;pointer-events:none;}
.vid-thumb-wrap:hover .thumb-play-icon{opacity:1}
.watch-btn{display:inline-flex;align-items:center;gap:6px;padding:7px 14px;border:none;border-radius:8px;background:linear-gradient(135deg,var(--accent2),#0077ff);color:#fff;font-size:12px;font-weight:600;cursor:pointer;font-family:'Sora',sans-serif;transition:all .2s;white-space:nowrap;}
.watch-btn:hover{opacity:.85;transform:translateY(-1px)}
.toast-container{position:fixed;bottom:24px;right:24px;z-index:999;display:flex;flex-direction:column;gap:8px;pointer-events:none;}
.toast{background:var(--card);border:1px solid var(--border2);border-radius:12px;padding:12px 16px;font-size:13px;font-family:'Space Mono',monospace;box-shadow:0 8px 32px var(--shadow);display:flex;align-items:center;gap:10px;animation:toastIn .3s var(--ease);pointer-events:all;max-width:320px;}
.toast.ok{border-color:rgba(0,230,118,.4);color:var(--green)}
.toast.err{border-color:rgba(255,82,82,.4);color:var(--red)}
.toast.info{border-color:rgba(0,229,255,.3);color:var(--accent2)}
@keyframes toastIn{from{opacity:0;transform:translateX(30px)}to{opacity:1;transform:translateX(0)}}
@keyframes toastOut{from{opacity:1;transform:translateX(0)}to{opacity:0;transform:translateX(30px)}}
::-webkit-scrollbar{width:5px}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:5px}
@media(max-width:520px){.quality-grid{grid-template-columns:repeat(3,1fr)}.video-row{flex-direction:column}.vid-thumb{width:100%;height:160px}.stats-bar{gap:6px}.stat-chip{font-size:10px;padding:4px 10px}.topbar-stats{display:none}}
</style>
</head>
<body>
<div class="glow-bg"></div>
<div class="topbar">
  <button class="hamburger" id="hamburger" onclick="toggleSidebar()" title="History"><span></span><span></span><span></span></button>
  <div class="topbar-title">MANDAL DL</div>
  <div class="topbar-right">
    <div class="topbar-stats" title="Server stats"><div class="ping-dot" id="pingDot"></div><span id="topbarUptimeVal">--</span></div>
    <button class="settings-btn" id="settingsBtn" onclick="toggleSettings()" title="Settings">⚙️</button>
  </div>
</div>
<div class="settings-panel" id="settingsPanel">
  <div class="settings-head">Settings</div>
  <div class="setting-row" onclick="toggleDark()"><div class="setting-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>Dark Mode</div><div class="toggle on" id="darkToggle"></div></div>
  <div class="setting-row" onclick="toggleSidebar();closeSettings()"><div class="setting-label"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M12 20h9M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>View History</div><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M9 18l6-6-6-6"/></svg></div>
  <div class="settings-divider"></div>
  <div class="settings-head">Server Status</div>
  <div class="uptime-info">
    <div class="uptime-row"><span>Status</span><span class="uptime-val" id="spStatus">—</span></div>
    <div class="uptime-row"><span>Uptime</span><span class="uptime-val" id="spUptime">—</span></div>
    <div class="uptime-row"><span>Downloads</span><span class="uptime-val" id="spDl">—</span></div>
    <div class="uptime-row"><span>YT Cookies</span><span class="uptime-val" id="spCookies">—</span></div>
    <div class="uptime-row"><span>Last Ping</span><span class="uptime-val" id="spPing">—</span></div>
  </div>
</div>
<div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>
<aside class="sidebar" id="sidebar">
  <div class="sidebar-header"><span class="sidebar-title">📂 Download History</span><button class="sidebar-close" onclick="toggleSidebar()">✕</button></div>
  <div class="sidebar-search"><input type="text" id="histSearch" placeholder="🔍 Search history..." oninput="filterHistory(this.value)"/></div>
  <div class="sidebar-list" id="historyList"><div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div></div>
  <div class="sidebar-clear"><button class="clear-all-btn" onclick="clearAllHistory()">🗑️ Clear All History</button></div>
</aside>
<div class="modal-overlay" id="renameModal">
  <div class="modal-box">
    <div class="modal-title">✏️ Rename</div>
    <div class="modal-sub" id="renameModalSub">Edit the display name</div>
    <input class="modal-input" id="renameInput" type="text" placeholder="Enter new name..." maxlength="120"/>
    <div class="modal-btns"><button class="modal-btn cancel" onclick="closeRenameModal()">Cancel</button><button class="modal-btn save" onclick="saveRename()">Save</button></div>
  </div>
</div>
<div class="player-overlay" id="playerOverlay" onclick="closePlayer(event)">
  <div class="player-box" id="playerBox">
    <div class="player-header">
      <div class="player-title-text" id="playerTitleText">Loading...</div>
      <div class="player-header-right">
        <a class="player-ext-btn" id="playerExtLink" href="#" target="_blank" rel="noopener"><svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>Open Original</a>
        <button class="player-close" onclick="closePlayerDirect()">✕</button>
      </div>
    </div>
    <div class="player-screen" id="playerScreen"><div class="player-loading" id="playerLoading"><span class="spin" style="width:28px;height:28px;border-width:3px"></span><span>Video load ho rahi hai...</span></div></div>
    <div class="player-footer"><div class="player-qual-row" id="playerQualRow"></div><div class="player-note" id="playerNote">⚡ Direct stream — no download needed</div></div>
  </div>
</div>
<div class="toast-container" id="toastContainer"></div>
<main class="main">
  <div class="hero">
    <div class="hero-badge">✦ Universal Downloader v3.3</div>
    <h1 class="hero-title">DOWNLOAD<br/><span>ANYTHING</span></h1>
    <p class="hero-sub">YouTube • Instagram • TikTok • Twitter • Facebook & more</p>
  </div>
  <div class="stats-bar">
    <div class="stat-chip">🟢 Status <span class="sv" id="statStatus">—</span></div>
    <div class="stat-chip">⬇️ Downloads <span class="sv" id="statDl">—</span></div>
    <div class="stat-chip">⏱️ Uptime <span class="sv" id="statUptime">—</span></div>
  </div>
  <div class="dl-card">
    <div class="url-row">
      <div class="url-wrap">
        <span class="url-icon">🔗</span>
        <input id="urlInput" type="url" placeholder="Paste video URL here..." autocomplete="off" spellcheck="false"/>
        <button class="paste-btn" onclick="pasteFromClipboard()" title="Clipboard se paste karo">📋</button>
      </div>
      <button class="fetch-btn" id="fetchBtn" onclick="fetchInfo()"><svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z"/></svg>Fetch</button>
    </div>
    <div class="platforms">
      <span class="plat" onclick="demoFill('yt')">🎬 YouTube</span>
      <span class="plat" onclick="demoFill('ig')">📸 Instagram</span>
      <span class="plat" onclick="demoFill('tt')">🎵 TikTok</span>
      <span class="plat" onclick="demoFill('tw')">🐦 Twitter/X</span>
      <span class="plat" onclick="demoFill('fb')">📘 Facebook</span>
    </div>
    <div id="statusBar"></div>
    <div id="infoCard">
      <div class="video-row" id="videoRow">
        <div class="vid-thumb-wrap" id="vidThumbWrap" onclick="openPlayer()" title="Watch Video"><img class="vid-thumb" id="vidThumb" src="" alt=""/><div class="thumb-play-icon">▶️</div></div>
        <div class="vid-info">
          <div class="vid-title" id="vidTitle"></div>
          <div class="vid-meta" id="vidMeta"></div>
          <div class="vid-extra" id="vidExtra"></div>
          <button class="watch-btn" onclick="openPlayer()" style="margin-top:10px">▶ Watch Video</button>
        </div>
      </div>
      <div class="prog-bar"><div class="prog-fill" id="progFill"></div></div>
      <div class="qlabel">Select Quality</div>
      <div class="quality-grid" id="qualityGrid"></div>
    </div>
  </div>
</main>
<footer>Built by <span>@MANDAL4482</span> &nbsp;·&nbsp; yt-dlp + ffmpeg + FastAPI &nbsp;·&nbsp; v3.3</footer>
<script>
const DB_NAME='mandal_dl',DB_VER=1,STORE='history';let db=null;
function openDB(){return new Promise((res,rej)=>{const req=indexedDB.open(DB_NAME,DB_VER);req.onupgradeneeded=e=>e.target.result.createObjectStore(STORE,{keyPath:'id'});req.onsuccess=e=>{db=e.target.result;res(db)};req.onerror=e=>rej(e)})}
async function dbGet(id){await openDB();return new Promise((res,rej)=>{const tx=db.transaction(STORE,'readonly');const req=tx.objectStore(STORE).get(id);req.onsuccess=()=>res(req.result);req.onerror=()=>rej(req.error)})}
async function dbPut(item){await openDB();return new Promise((res,rej)=>{const tx=db.transaction(STORE,'readwrite');tx.objectStore(STORE).put(item);tx.oncomplete=()=>res();tx.onerror=()=>rej(tx.error)})}
async function dbDelete(id){await openDB();return new Promise((res,rej)=>{const tx=db.transaction(STORE,'readwrite');tx.objectStore(STORE).delete(id);tx.oncomplete=()=>res();tx.onerror=()=>rej(tx.error)})}
async function dbAll(){await openDB();return new Promise((res,rej)=>{const tx=db.transaction(STORE,'readonly');const req=tx.objectStore(STORE).getAll();req.onsuccess=()=>res(req.result||[]);req.onerror=()=>rej(req.error)})}
async function dbClear(){await openDB();return new Promise((res,rej)=>{const tx=db.transaction(STORE,'readwrite');tx.objectStore(STORE).clear();tx.oncomplete=()=>res();tx.onerror=()=>rej(tx.error)})}
const htmlEl=document.documentElement;let darkMode=localStorage.getItem('dark')!=='false';applyTheme();
function applyTheme(){htmlEl.setAttribute('data-theme',darkMode?'dark':'light');const tog=document.getElementById('darkToggle');if(tog)tog.className='toggle '+(darkMode?'on':'')}
function toggleDark(){darkMode=!darkMode;localStorage.setItem('dark',darkMode);applyTheme()}
function showToast(msg,type='info',duration=3000){const c=document.getElementById('toastContainer');const t=document.createElement('div');t.className='toast '+type;t.innerHTML=msg;c.appendChild(t);setTimeout(()=>{t.style.animation='toastOut .3s forwards';setTimeout(()=>t.remove(),300)},duration)}
async function pasteFromClipboard(){try{const text=await navigator.clipboard.readText();if(text&&(text.startsWith('http')||text.includes('youtu'))){document.getElementById('urlInput').value=text;showToast('📋 URL paste ho gayi!','ok',2000)}else{showToast('⚠️ Clipboard mein valid URL nahi mili','err',2000)}}catch(e){showToast('⚠️ Clipboard access nahi mila','err',2000)}}
let settingsOpen=false;
function toggleSettings(){settingsOpen=!settingsOpen;document.getElementById('settingsPanel').classList.toggle('open',settingsOpen)}
function closeSettings(){settingsOpen=false;document.getElementById('settingsPanel').classList.remove('open')}
document.addEventListener('click',e=>{const sp=document.getElementById('settingsPanel');const sb=document.getElementById('settingsBtn');if(settingsOpen&&!sp.contains(e.target)&&!sb.contains(e.target))closeSettings()});
let sidebarOpen=false;
function toggleSidebar(){sidebarOpen=!sidebarOpen;document.getElementById('sidebar').classList.toggle('open',sidebarOpen);document.getElementById('sidebarOverlay').classList.toggle('open',sidebarOpen);document.getElementById('hamburger').classList.toggle('open',sidebarOpen);if(sidebarOpen)renderHistory()}
let _allHistItems=[];
async function renderHistory(filter=''){const items=await dbAll();_allHistItems=items;displayHistoryItems(items,filter)}
function displayHistoryItems(items,filter=''){const list=document.getElementById('historyList');let filtered=items;if(filter)filtered=items.filter(it=>(it.name||'').toLowerCase().includes(filter.toLowerCase())||(it.platform||'').toLowerCase().includes(filter.toLowerCase()));if(!filtered.length){list.innerHTML=items.length?'<div class="history-empty">No results found.</div>':'<div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div>';return}filtered.sort((a,b)=>b.ts-a.ts);list.innerHTML=filtered.map(it=>`<div class="hist-item" id="hitem-${it.id}"><div class="hist-thumb-row">${it.thumbnail?`<img class="hist-thumb" src="${it.thumbnail}" alt="" onerror="this.style.display='none'"/>`:`<div class="hist-no-thumb">🎬</div>`}<div class="hist-info"><div class="hist-name" title="${esc(it.name)}">${esc(it.name)}</div><div class="hist-meta">${it.quality} · ${it.platform}<br/>${relTime(it.ts)}</div></div></div><div class="hist-actions"><button class="hist-btn dl" onclick="reDownload('${it.id}')">⬇ DL</button><button class="hist-btn ren" onclick="openRenameModal('${it.id}')">✏ Rename</button><button class="hist-btn del" onclick="deleteHistItem('${it.id}')">🗑 Del</button></div></div>`).join('')}
function filterHistory(val){displayHistoryItems(_allHistItems,val)}
function esc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function relTime(ts){const diff=Date.now()-ts;if(diff<60000)return 'Just now';if(diff<3600000)return Math.floor(diff/60000)+'m ago';if(diff<86400000)return Math.floor(diff/3600000)+'h ago';return Math.floor(diff/86400000)+'d ago'}
async function deleteHistItem(id){await dbDelete(id);const el=document.getElementById('hitem-'+id);if(el){el.style.opacity='0';el.style.transform='translateX(-20px)';el.style.transition='all .3s';setTimeout(()=>el.remove(),300)}_allHistItems=_allHistItems.filter(x=>x.id!==id);if(!(await dbAll()).length)document.getElementById('historyList').innerHTML='<div class="history-empty">No downloads yet.<br/>Paste a URL and<br/>start downloading!</div>'}
async function clearAllHistory(){if(!confirm('All history delete ho jayegi. Sure?'))return;await dbClear();_allHistItems=[];renderHistory()}
async function reDownload(id){const item=await dbGet(id);if(!item)return;document.getElementById('urlInput').value=item.url;toggleSidebar();fetchInfo()}
let renameTarget=null;
function openRenameModal(id){renameTarget=id;dbGet(id).then(it=>{if(!it)return;document.getElementById('renameInput').value=it.name;document.getElementById('renameModalSub').textContent='Rename: '+it.name.slice(0,40);document.getElementById('renameModal').classList.add('open');document.getElementById('renameInput').focus()})}
function closeRenameModal(){renameTarget=null;document.getElementById('renameModal').classList.remove('open')}
async function saveRename(){if(!renameTarget)return;const newName=document.getElementById('renameInput').value.trim();if(!newName)return;const it=await dbGet(renameTarget);if(!it)return;it.name=newName;await dbPut(it);closeRenameModal();renderHistory();showToast('✅ Rename ho gaya!','ok',2000)}
document.getElementById('renameInput').addEventListener('keydown',e=>{if(e.key==='Enter')saveRename()});
document.getElementById('renameModal').addEventListener('click',e=>{if(e.target===document.getElementById('renameModal'))closeRenameModal()});
let currentUrl='',currentData=null;
function setStatus(msg,type=''){const el=document.getElementById('statusBar');el.innerHTML=msg;el.className=type}
function fmtSize(b){if(!b)return '';if(b>1073741824)return (b/1073741824).toFixed(1)+' GB';if(b>1048576)return (b/1048576).toFixed(1)+' MB';return (b/1024).toFixed(0)+' KB'}
function fmtDur(s){if(!s)return '';const m=Math.floor(s/60),sec=s%60;return m+'m '+String(sec).padStart(2,'0')+'s'}
function fmtNum(n){if(!n)return '';if(n>=1000000)return (n/1000000).toFixed(1)+'M';if(n>=1000)return (n/1000).toFixed(1)+'K';return String(n)}
function detectPlatform(url){const u=url.toLowerCase();if(u.includes('youtube')||u.includes('youtu.be'))return 'YouTube';if(u.includes('instagram'))return 'Instagram';if(u.includes('tiktok'))return 'TikTok';if(u.includes('twitter')||u.includes('x.com'))return 'Twitter/X';if(u.includes('facebook')||u.includes('fb.'))return 'Facebook';return 'Web'}
async function fetchInfo(){
  const url=document.getElementById('urlInput').value.trim();
  if(!url){setStatus('⚠️ URL paste karo pehle!','err');return}
  currentUrl=url;
  const btn=document.getElementById('fetchBtn');
  btn.disabled=true;btn.innerHTML='<span class="spin"></span> Fetching...';
  document.getElementById('infoCard').style.display='none';
  setStatus('<span class="spin"></span> &nbsp;Video info aa rahi hai...');setProg(0);
  try{
    setProg(20);
    const res=await fetch('/info?url='+encodeURIComponent(url));
    let data;
    try{data=await res.json();}
    catch(jsonErr){const txt=await res.text().catch(()=>'');if(!res.ok){setStatus('❌ &nbsp;Server error ('+res.status+'): '+res.statusText,'err');showToast('❌ Server error '+res.status,'err',5000);}else{setStatus('❌ &nbsp;Server se invalid response mila','err');showToast('❌ Invalid JSON response','err',5000);}return;}
    setProg(60);
    if(!res.ok||!data.ok){setStatus('❌ &nbsp;'+(data.detail||data.error||'Info nahi mili'),'err');showToast('❌ '+(data.detail||data.error||'Info nahi mili'),'err');return}
    currentData=data;
    const thumb=document.getElementById('vidThumb');
    if(data.thumbnail){thumb.src=data.thumbnail;thumb.style.display='block'}else thumb.style.display='none';
    document.getElementById('vidTitle').textContent=data.title||'Untitled';
    document.getElementById('vidMeta').textContent=[data.uploader,fmtDur(data.duration)].filter(Boolean).join(' · ');
    const extraEl=document.getElementById('vidExtra');extraEl.innerHTML='';
    if(data.view_count)extraEl.innerHTML+=`<span class="vid-chip">👁 ${fmtNum(data.view_count)} views</span>`;
    if(data.like_count)extraEl.innerHTML+=`<span class="vid-chip">👍 ${fmtNum(data.like_count)}</span>`;
    extraEl.innerHTML+=`<span class="vid-chip">${detectPlatform(url)}</span>`;
    const grid=document.getElementById('qualityGrid');grid.innerHTML='';currentFormats=data.formats;
    data.formats.forEach(f=>{const btn=document.createElement('button');btn.className='qbtn'+(f.quality==='audio'?' audio':'');btn.dataset.quality=f.quality;btn.dataset.label=f.label;const sz=fmtSize(f.filesize);btn.innerHTML=`<div class="qbtn-label">${f.quality==='audio'?'🎵':''} ${f.label}</div>${sz?`<span class="qbtn-size">${sz}</span>`:''}`;btn.onclick=()=>startDownload(f.quality,btn,f.label);grid.appendChild(btn)});
    setProg(100);setTimeout(()=>setProg(0,false),600);
    document.getElementById('infoCard').style.display='block';
    setStatus('✅ &nbsp;'+data.formats.length+' options — quality choose karo!','ok');
    showToast('✅ Video info mil gayi!','ok',2500);
  }catch(e){setStatus('❌ &nbsp;Network error: '+e.message,'err');showToast('❌ Network error: '+e.message,'err');}
  finally{btn.disabled=false;btn.innerHTML='<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 21l-4.35-4.35M17 11A6 6 0 1 1 5 11a6 6 0 0 1 12 0z"/></svg> Fetch';}
}
function setProg(val,animate=true){const fill=document.getElementById('progFill');fill.style.transition=animate?'width .4s cubic-bezier(.4,0,.2,1)':'none';fill.style.width=val+'%'}
async function startDownload(quality,btnEl,label){
  document.querySelectorAll('.qbtn').forEach(b=>{b.classList.add('loading');b.disabled=true});
  btnEl.innerHTML=`<div class="qbtn-label"><span class="spin"></span></div><span class="qbtn-size">downloading</span>`;
  setProg(5);setStatus(`⬇️ &nbsp;${label} download ho rahi hai...`);showToast(`⬇️ ${label} download shuru...`,'info',2000);
  let pct=5;const timer=setInterval(()=>{pct=Math.min(pct+Math.random()*5,88);setProg(pct)},700);
  try{
    const dlUrl=`/download?url=${encodeURIComponent(currentUrl)}&quality=${encodeURIComponent(quality)}`;
    const res=await fetch(dlUrl);
    if(!res.ok){let errMsg='Download failed ('+res.status+')';try{const err=await res.json();errMsg=err.detail||errMsg;}catch(e){const txt=await res.text().catch(()=>'');if(txt)errMsg=txt.slice(0,120);}throw new Error(errMsg);}
    const blob=await res.blob();
    const cd=res.headers.get('Content-Disposition')||'';
    const fnM=cd.match(/filename="?([^"]+)"?/);
    const filename=fnM?fnM[1]:(quality==='audio'?'audio.mp3':'video.mp4');
    const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=filename;document.body.appendChild(a);a.click();a.remove();URL.revokeObjectURL(a.href);
    const histId=Date.now().toString();
    await dbPut({id:histId,url:currentUrl,name:currentData?.title||'Untitled',thumbnail:currentData?.thumbnail||null,quality:label,platform:detectPlatform(currentUrl),ts:Date.now()});
    clearInterval(timer);setProg(100);setTimeout(()=>setProg(0,false),700);
    setStatus('✅ &nbsp;Download complete! File save ho gayi.','ok');showToast('✅ Download complete! 🎉','ok',4000);
  }catch(e){clearInterval(timer);setProg(0,false);setStatus('❌ &nbsp;'+e.message,'err');showToast('❌ '+e.message,'err',4000);}
  document.querySelectorAll('.qbtn').forEach(b=>{b.classList.remove('loading');b.disabled=false});
  document.querySelectorAll('.qbtn').forEach(b=>{if(b.dataset.quality===quality){const lbl=b.dataset.label;b.innerHTML=`<div class="qbtn-label">${quality==='audio'?'🎵':''} ${lbl}</div>`}});
}
function demoFill(p){const el=document.getElementById('urlInput');const samples={yt:'https://www.youtube.com/watch?v=dQw4w9WgXcQ',ig:'https://www.instagram.com/p/example/',tt:'https://www.tiktok.com/@user/video/example',tw:'https://twitter.com/i/status/example',fb:'https://www.facebook.com/watch?v=example'};el.value=samples[p]||'';el.focus();}
let playerOpen=false,currentFormats=[];
function getYTId(url){const m=url.match(/(?:youtube\.com\/watch\?v=|youtu\.be\/)([\w-]{11})/);return m?m[1]:null}
function openPlayer(){
  if(!currentUrl||!currentData)return;
  const overlay=document.getElementById('playerOverlay'),screen=document.getElementById('playerScreen'),loading=document.getElementById('playerLoading'),ptitle=document.getElementById('playerTitleText'),pqrow=document.getElementById('playerQualRow'),extLink=document.getElementById('playerExtLink');
  ptitle.textContent=currentData?.title||'Video';extLink.href=currentUrl;overlay.classList.add('open');playerOpen=true;document.body.style.overflow='hidden';
  const oldEmbed=document.getElementById('playerEmbed');if(oldEmbed)oldEmbed.remove();loading.style.display='flex';loading.innerHTML='<span class="spin" style="width:28px;height:28px;border-width:3px"></span><span>Video load ho rahi hai...</span>';
  pqrow.innerHTML='';const videoFormats=currentFormats.filter(f=>f.quality!=='audio');const previewQualities=videoFormats.slice(0,4);
  previewQualities.forEach((f,i)=>{const btn=document.createElement('button');btn.className='pqbtn'+(i===0?' active':'');btn.textContent=f.label;btn.onclick=()=>{document.querySelectorAll('.pqbtn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');loadPlayerContent(f.quality)};pqrow.appendChild(btn)});
  const bestQ=previewQualities[0]?.quality||'720';loadPlayerContent(bestQ);
}
function loadPlayerContent(quality){
  const screen=document.getElementById('playerScreen'),loading=document.getElementById('playerLoading'),noteEl=document.getElementById('playerNote');
  const oldEmbed=document.getElementById('playerEmbed');if(oldEmbed)oldEmbed.remove();loading.style.display='flex';loading.innerHTML='<span class="spin" style="width:28px;height:28px;border-width:3px"></span><span>Loading...</span>';
  const ytId=getYTId(currentUrl);
  if(ytId){const iframe=document.createElement('iframe');iframe.id='playerEmbed';iframe.src=`https://www.youtube.com/embed/${ytId}?autoplay=1&rel=0`;iframe.allow='accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share';iframe.allowFullscreen=true;iframe.onload=()=>{loading.style.display='none'};screen.appendChild(iframe);noteEl.textContent='▶ YouTube embedded player';}
  else{const video=document.createElement('video');video.id='playerEmbed';video.controls=true;video.autoplay=true;video.playsInline=true;video.style.background='#000';video.src=`/stream?url=${encodeURIComponent(currentUrl)}&quality=${encodeURIComponent(quality)}`;video.oncanplay=()=>{loading.style.display='none'};video.onerror=()=>{loading.innerHTML=`<span style="font-size:32px">⚠️</span><span style="text-align:center">Is platform ka direct preview available nahi.<br/><strong style="color:var(--accent2)">Download karo enjoy karo!</strong></span>`;loading.style.display='flex'};screen.appendChild(video);noteEl.textContent='⚡ Direct stream — no download needed';}
}
function closePlayer(e){if(e&&e.target!==document.getElementById('playerOverlay'))return;closePlayerDirect()}
function closePlayerDirect(){document.getElementById('playerOverlay').classList.remove('open');playerOpen=false;document.body.style.overflow='';const embed=document.getElementById('playerEmbed');if(embed)embed.remove()}
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&playerOpen)closePlayerDirect()});
document.getElementById('urlInput').addEventListener('keydown',e=>{if(e.key==='Enter')fetchInfo()});
async function pingServer(){try{const res=await fetch('/ping',{cache:'no-store'});if(res.ok){document.getElementById('pingDot').className='ping-dot online';document.getElementById('spPing').textContent=new Date().toLocaleTimeString();document.getElementById('spStatus').textContent='🟢 Online';document.getElementById('statStatus').textContent='Online'}else throw new Error();}catch{document.getElementById('pingDot').className='ping-dot offline';document.getElementById('spStatus').textContent='🔴 Offline';document.getElementById('statStatus').textContent='Offline'}}
async function fetchStats(){try{const res=await fetch('/stats',{cache:'no-store'});const data=await res.json();const upSec=data.uptime_seconds||0;const days=Math.floor(upSec/86400),hrs=Math.floor((upSec%86400)/3600),mins=Math.floor((upSec%3600)/60);const uptimeStr=days>0?`${days}d ${hrs}h`:hrs>0?`${hrs}h ${mins}m`:`${mins}m`;document.getElementById('spUptime').textContent=uptimeStr;document.getElementById('spDl').textContent=data.total_downloads||0;document.getElementById('statUptime').textContent=uptimeStr;document.getElementById('statDl').textContent=data.total_downloads||0;document.getElementById('topbarUptimeVal').textContent=uptimeStr;}catch(e){}}
async function fetchHealth(){try{const res=await fetch('/health',{cache:'no-store'});const data=await res.json();const ck=document.getElementById('spCookies');if(ck)ck.textContent=data.youtube_cookies==='loaded'?'✅ Loaded':'⚠️ Missing';}catch(e){}}
pingServer();fetchStats();fetchHealth();
setInterval(pingServer,30000);setInterval(fetchStats,30000);setInterval(fetchHealth,60000);
openDB().catch(console.warn);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
async def root():
    return HTMLResponse(content=HTML_PAGE)
