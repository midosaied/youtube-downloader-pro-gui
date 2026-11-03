#!/usr/bin/env python3
"""
YouTube Downloader Pro — modern desktop GUI
Download audio/video from YouTube with playlist support, multi-download,
resumable downloads, and automatic detection/installation of every missing
dependency (Python packages + ffmpeg).
"""
import os
import sys
import shutil
import subprocess
import importlib
import re
import json
import time
import tempfile
import argparse
import logging
import threading
import queue
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Callable

# ========== AUTO DEPENDENCY BOOTSTRAP ==========
# Must run before importing any third-party library, so missing ones get
# installed automatically.
REQUIRED_PACKAGES = {
    "yt_dlp": "yt-dlp",
    "customtkinter": "customtkinter",
}

# Defined at module scope (not inside Config) because the yt-dlp update check
# runs at import time, before the Config class below even exists.
_UPDATE_CHECK_FILE = os.path.expanduser("~/.yt_downloader_update_check.json")
_UPDATE_CHECK_INTERVAL_HOURS = 24


def _atomic_write_json(path: str, data) -> bool:
    """
    Writes JSON to `path` safely: write to a temp file in the same directory,
    then os.replace() it over the target. This avoids a half-written/corrupt
    file if the process is killed or the machine loses power mid-write.
    """
    try:
        directory = os.path.dirname(path) or "."
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, path)
            return True
        except Exception:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
    except OSError as e:
        logging.warning(f"Could not save {path}: {e}")
        return False

def _pip_install(pip_name: str) -> bool:
    """
    Install a package via pip (current interpreter), returns True on success.
    Tries several fallback strategies since environments vary a lot:
    plain install -> --user -> --break-system-packages (PEP 668 systems,
    common on modern Debian/Ubuntu) -> both combined.
    """
    if getattr(sys, "frozen", False):
        # Packaged .exe: sys.executable is the app itself, not Python, so
        # "python -m pip" would relaunch the app. Everything is bundled anyway.
        return False
    attempts = [
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", pip_name],
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--user", pip_name],
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--break-system-packages", pip_name],
        [sys.executable, "-m", "pip", "install", "--upgrade", "--quiet", "--user",
         "--break-system-packages", pip_name],
    ]
    last_error = None
    for cmd in attempts:
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
            return True
        except subprocess.CalledProcessError as e:
            last_error = e
            continue
    print(f"[!] Failed to install {pip_name}: {last_error}")
    return False

def ensure_python_packages(status_cb: Optional[Callable[[str], None]] = None):
    """Check every required package is importable; auto-install anything missing."""
    def emit(msg):
        if status_cb:
            status_cb(msg)
        else:
            print(msg)

    missing = []
    for module_name, pip_name in REQUIRED_PACKAGES.items():
        try:
            importlib.import_module(module_name)
        except ImportError:
            missing.append((module_name, pip_name))

    if not missing:
        return True

    emit("Some required packages are missing, installing them automatically...")
    for module_name, pip_name in missing:
        emit(f"Installing {pip_name} ...")
        if _pip_install(pip_name):
            importlib.invalidate_caches()
            try:
                importlib.import_module(module_name)
                emit(f"Installed {pip_name}")
            except ImportError:
                emit(f"Installed but failed to import: {module_name} — please restart the app.")
                return False
        else:
            emit(f"Could not auto-install {pip_name}. Install it manually: {sys.executable} -m pip install {pip_name}")
            return False
    return True

def _should_check_ytdlp_update() -> bool:
    """
    Only check for an update once every _UPDATE_CHECK_INTERVAL_HOURS. Doing
    this on every single startup means a pip/network round-trip every time
    the app opens, which adds a few real seconds of delay for no benefit —
    YouTube doesn't change its protection every hour.
    """
    try:
        with open(_UPDATE_CHECK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        last_check = datetime.fromisoformat(data.get("last_check", ""))
        return datetime.now() - last_check >= timedelta(hours=_UPDATE_CHECK_INTERVAL_HOURS)
    except (FileNotFoundError, json.JSONDecodeError, ValueError, TypeError):
        return True


def _record_ytdlp_check(version: str):
    _atomic_write_json(_UPDATE_CHECK_FILE, {
        "last_check": datetime.now().isoformat(),
        "last_version": version,
    })


def ensure_ytdlp_up_to_date(status_cb: Optional[Callable[[str], None]] = None, force: bool = False):
    """
    yt-dlp needs to be updated regularly because YouTube changes its
    protection/signature mechanism every few weeks. An outdated version
    causes a "Requested format is not available" error even when the video
    itself is perfectly fine. This tries to update in the background (never
    blocks the app if the update fails), but only actually hits the network
    once a day — see _should_check_ytdlp_update().
    """
    def emit(msg):
        if status_cb:
            status_cb(msg)
        else:
            print(msg)

    if getattr(sys, "frozen", False):
        return  # packaged build: yt-dlp is bundled, update by downloading a newer release

    try:
        old_version = getattr(importlib.import_module("yt_dlp.version"), "__version__", "?")
    except Exception:
        old_version = "?"

    if not force and not _should_check_ytdlp_update():
        emit(f"yt-dlp update check skipped (already checked within the last {_UPDATE_CHECK_INTERVAL_HOURS}h, version {old_version}).")
        return

    emit("Making sure yt-dlp is up to date (needed because YouTube keeps changing its protection)...")
    if _pip_install("yt-dlp"):
        importlib.invalidate_caches()
        # Clear cached yt_dlp modules so we actually pick up the new version
        # within the same run.
        for mod_name in list(sys.modules):
            if mod_name == "yt_dlp" or mod_name.startswith("yt_dlp."):
                del sys.modules[mod_name]
        new_version = old_version
        try:
            new_version = getattr(importlib.import_module("yt_dlp.version"), "__version__", "?")
            if new_version != old_version:
                emit(f"yt-dlp updated: {old_version} → {new_version}")
            else:
                emit(f"yt-dlp is already up to date (version {new_version})")
        except Exception:
            pass
        _record_ytdlp_check(new_version)
    else:
        emit("Could not check for a yt-dlp update (maybe no internet) — continuing with the current version.")

# Make sure yt_dlp and customtkinter are available before importing them at
# module level.
if not ensure_python_packages():
    sys.exit(1)

# Keep yt-dlp fresh — this is the most important fix for "Requested format is
# not available", since that usually happens because of an outdated version.
# Actual network check only happens once a day (see _should_check_ytdlp_update).
ensure_ytdlp_up_to_date()

import yt_dlp  # noqa: E402
from yt_dlp.utils import DownloadError  # noqa: E402
import concurrent.futures  # noqa: E402
from functools import partial  # noqa: E402

import customtkinter as ctk  # noqa: E402
from tkinter import filedialog, messagebox  # noqa: E402

# ========== CONFIGURATION ==========
class Config:
    DEFAULT_AUDIO_FOLDER = os.path.expanduser("~/YouTube/Audio")
    DEFAULT_VIDEO_FOLDER = os.path.expanduser("~/YouTube/Video")
    FFMPEG_PATH: Optional[str] = None
    MAX_RETRIES = 3
    EMBED_METADATA = True
    EMBED_THUMBNAIL = True
    LOG_FILE = "youtube_downloader.log"
    LOG_LEVEL = logging.INFO
    MAX_CONCURRENT_DOWNLOADS = 4
    SOCKET_TIMEOUT = 30
    HISTORY_FILE = os.path.expanduser("~/.yt_downloader_history.json")
    SETTINGS_FILE = os.path.expanduser("~/.yt_downloader_settings.json")
    COOKIES_FROM_BROWSER: Optional[str] = None  # "chrome" / "firefox" / "edge" / "brave" / None
    SPONSORBLOCK_REMOVE = False  # cut out sponsor segments using api.sponsor.ajay.app
    DOWNLOAD_SUBTITLES = False   # save subtitles (manual, falling back to auto-generated) alongside the file
    SUBTITLE_LANGS = "en"        # comma-separated language codes, e.g. "en,ar"

# ========== LOGGING SETUP ==========
def setup_logging():
    logging.basicConfig(
        level=Config.LOG_LEVEL,
        format="%(asctime)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.FileHandler(Config.LOG_FILE, encoding="utf-8")],
    )
    logging.getLogger("yt_dlp").setLevel(logging.ERROR)

# ========== FFMPEG DETECTION / AUTO-INSTALL ==========
def find_ffmpeg() -> Optional[str]:
    found = shutil.which("ffmpeg")
    if found:
        return found
    candidates = [
        r"C:\ffmpeg\bin\ffmpeg.exe",
        "/usr/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return None

def try_auto_install_ffmpeg(status_cb: Optional[Callable[[str], None]] = None) -> Optional[str]:
    def emit(msg):
        if status_cb:
            status_cb(msg)
        else:
            print(msg)

    emit("ffmpeg not found, attempting automatic installation...")

    system_installers = []
    if sys.platform.startswith("linux") and shutil.which("apt-get"):
        # -n makes sudo non-interactive: if a password is required it fails
        # immediately (in well under a second) instead of hanging forever
        # waiting for input on a terminal that doesn't exist (we're a GUI
        # app launched from a thread, so there's nowhere for sudo to prompt).
        system_installers.append((
            ["sudo", "-n", "apt-get", "install", "-y", "ffmpeg"],
            "Run this in a terminal to install ffmpeg manually: sudo apt-get install -y ffmpeg",
        ))
    elif sys.platform == "darwin" and shutil.which("brew"):
        system_installers.append((["brew", "install", "ffmpeg"], "Run: brew install ffmpeg"))
    elif sys.platform.startswith("win") and shutil.which("winget"):
        system_installers.append((
            ["winget", "install", "-e", "--id", "Gyan.FFmpeg", "--silent"],
            "Run: winget install -e --id Gyan.FFmpeg",
        ))

    for cmd, manual_hint in system_installers:
        try:
            if cmd[0] == "sudo":
                emit("Attempting an unattended (passwordless) sudo install — if your system requires a sudo password this step will just fail fast, and you'll get the manual command to run yourself.")
            emit(f"Trying: {' '.join(cmd)}")
            subprocess.run(cmd, check=True, timeout=300, stdin=subprocess.DEVNULL,
                            capture_output=True, text=True)
            found = find_ffmpeg()
            if found:
                emit("ffmpeg installed via the system package manager")
                return found
        except subprocess.CalledProcessError as e:
            stderr = (e.stderr or "").strip()
            if "sudo" in cmd and ("password" in stderr.lower() or "a password is required" in stderr.lower()):
                emit(f"Needs a password to install with sudo, can't prompt from here. {manual_hint}")
            else:
                emit(f"This method failed: {stderr or e}")
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            emit(f"This method failed: {e}")

    try:
        emit("Trying to fetch a portable copy via pip (static-ffmpeg)...")
        if getattr(sys, "frozen", False) or _pip_install("static-ffmpeg"):
            importlib.invalidate_caches()
            from static_ffmpeg import run as _static_ffmpeg_run
            ffmpeg_path, _ffprobe_path = _static_ffmpeg_run.get_or_fetch_platform_executables_else_raise()
            if ffmpeg_path and os.path.isfile(ffmpeg_path):
                emit(f"Portable ffmpeg ready at: {ffmpeg_path}")
                return ffmpeg_path
    except Exception as e:
        emit(f"Failed to prepare the portable copy: {e}")

    return None

def validate_ffmpeg(status_cb: Optional[Callable[[str], None]] = None) -> bool:
    Config.FFMPEG_PATH = find_ffmpeg()
    if not Config.FFMPEG_PATH:
        Config.FFMPEG_PATH = try_auto_install_ffmpeg(status_cb)
    if not Config.FFMPEG_PATH:
        return False
    try:
        subprocess.run(
            [Config.FFMPEG_PATH, "-version"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
        )
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False

# ========== UTILITY FUNCTIONS ==========
_WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
}

def sanitize_filename(filename: str) -> str:
    filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)
    filename = re.sub(r'\s+', ' ', filename).strip()
    filename = filename.rstrip('. ')
    filename = filename[:200] or "Unknown"
    # Windows reserves these names (with or without an extension) even inside
    # a folder path — a channel/playlist literally named "AUX" or "CON" would
    # otherwise make os.makedirs()/file creation fail with a confusing error.
    if filename.upper() in _WINDOWS_RESERVED_NAMES:
        filename = f"_{filename}"
    return filename

def get_channel_folder_name(info: Dict) -> str:
    uploader = info.get('uploader') or info.get('channel') or 'Unknown Channel'
    uploader_id = info.get('uploader_id')
    if uploader_id and uploader_id != uploader:
        return sanitize_filename(f"{uploader} [{uploader_id}]")
    return sanitize_filename(uploader)

def is_valid_youtube_url(url: str) -> bool:
    youtube_patterns = [
        r'(https?://)?(www\.|m\.)?youtube\.com/watch\?v=',
        r'(https?://)?(www\.|m\.)?youtube\.com/playlist\?list=',
        r'(https?://)?(www\.)?youtu\.be/',
        r'(https?://)?(www\.|m\.)?youtube\.com/shorts/',
        r'(https?://)?(www\.|m\.)?youtube\.com/live/',
        r'(https?://)?(www\.|m\.)?youtube\.com/embed/',
        r'(https?://)?music\.youtube\.com/watch\?v=',
    ]
    url = url.strip()
    return any(re.search(pattern, url) for pattern in youtube_patterns)

def normalize_youtube_url(url: str) -> str:
    url = url.strip()
    if 'youtu.be' in url:
        video_id = url.split('/')[-1].split('?')[0]
        return f"https://www.youtube.com/watch?v={video_id}"
    # Mobile-shared links (m.youtube.com) and Music links work fine in
    # yt-dlp as-is, but normalizing to www. keeps output folder naming and
    # de-duplication consistent regardless of which one the user pasted.
    if 'm.youtube.com' in url:
        url = url.replace('m.youtube.com', 'www.youtube.com')
    if 'music.youtube.com' in url:
        url = url.replace('music.youtube.com', 'www.youtube.com')
    return url.split('&')[0] if 'list=' not in url else url

# ========== QUALITY PRESETS ==========
QUALITY_PRESETS = {
    "audio": [("128 kbps", "128"), ("192 kbps", "192"), ("256 kbps", "256"), ("320 kbps", "320")],
    "video": [
        ("144p", "144"), ("240p", "240"), ("360p", "360"), ("480p", "480"),
        ("720p", "720"), ("1080p", "1080"), ("1440p (2K)", "1440"),
        ("2160p (4K)", "2160"), ("Best quality", "best"),
    ],
}

def build_format_string(format_choice: str, quality_value: str) -> str:
    """
    Important note: audio-only formats have no "height" field at all, so a
    filter like bestaudio[height<=720] always fails and returns zero
    results, producing "Requested format is not available" — the height
    filter must only apply to the video side, while audio just takes
    bestaudio unfiltered.
    We also add multiple fallback levels so that if YouTube doesn't offer
    the exact requested quality, we drop to the closest available option
    instead of failing outright.
    """
    if format_choice == "audio":
        if quality_value == "best":
            return "bestaudio/best"
        return f"bestaudio[abr<={quality_value}]/bestaudio/best"
    else:
        if quality_value == "best":
            return "bestvideo*+bestaudio/best"
        h = quality_value
        return (
            f"bestvideo[height<={h}]+bestaudio/"   # preferred: video at the requested quality + best audio
            f"best[height<={h}]/"                   # a ready-made combined format within the same cap, if any
            f"bestvideo+bestaudio/"                 # any available video quality + audio (better than failing)
            f"best"                                  # last resort: any combined format that exists at all
        )

# ========== DOWNLOAD HISTORY ==========
def load_history() -> set:
    try:
        with open(Config.HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except (FileNotFoundError, json.JSONDecodeError):
        return set()

def save_history(history: set):
    _atomic_write_json(Config.HISTORY_FILE, list(history))

# ========== USER SETTINGS PERSISTENCE ==========
def load_settings() -> Dict:
    try:
        with open(Config.SETTINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def save_settings(settings: Dict):
    _atomic_write_json(Config.SETTINGS_FILE, settings)

# ========== DOWNLOAD CORE ==========
def prepare_ydl_options(format_choice: str, quality_value: str, output_path: str) -> Dict:
    ydl_opts = {
        "ffmpeg_location": Config.FFMPEG_PATH,
        "outtmpl": os.path.join(output_path, "%(title)s.%(ext)s"),
        "quiet": True,
        "logger": logging.getLogger(),
        "socket_timeout": Config.SOCKET_TIMEOUT,
        "retries": Config.MAX_RETRIES,
        "fragment_retries": Config.MAX_RETRIES,
        "ignoreerrors": False,
        "no_warnings": False,
        "noplaylist": False,
        "continuedl": True,
        "noprogress": True,  # progress via hook only, not console spam
        "windowsfilenames": True,  # prevents file-save errors on Windows for chars like : * ? "
        "restrictfilenames": False,
        # Light throttling between requests to reduce the chance of YouTube
        # returning 429 / "confirm you're not a bot" when several downloads
        # are running at the same time.
        "sleep_interval_requests": 1,
        "sleep_interval": 0.5,
        "max_sleep_interval": 2,
    }
    if Config.COOKIES_FROM_BROWSER:
        ydl_opts["cookiesfrombrowser"] = (Config.COOKIES_FROM_BROWSER,)

    if Config.DOWNLOAD_SUBTITLES:
        langs = [l.strip() for l in Config.SUBTITLE_LANGS.split(",") if l.strip()] or ["en"]
        ydl_opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,  # fall back to YouTube's auto-generated captions if no manual ones exist
            "subtitleslangs": langs,
            "subtitlesformat": "srt/best",
        })

    # SponsorBlock: fetches community-submitted sponsor-segment timestamps for
    # the video and cuts them out of the final file. Works for both audio and
    # video since it's keyed on the video ID, not the format.
    sponsorblock_pps = []
    if Config.SPONSORBLOCK_REMOVE:
        sponsorblock_pps = [
            {"key": "SponsorBlock", "categories": ["sponsor"], "api": "https://sponsor.ajay.app"},
            {"key": "ModifyChapters", "remove_sponsor_segments": ["sponsor"], "force_keyframes": False},
        ]

    fmt = build_format_string(format_choice, quality_value)
    if format_choice == "audio":
        ydl_opts.update({
            "format": fmt,
            "postprocessors": [
                *sponsorblock_pps,
                {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
                 "preferredquality": "0" if quality_value == "best" else quality_value},
                *([{"key": "FFmpegMetadata"}] if Config.EMBED_METADATA else []),
                *([{"key": "EmbedThumbnail"}] if Config.EMBED_THUMBNAIL else []),
            ],
            "writethumbnail": Config.EMBED_THUMBNAIL,
        })
    else:
        ydl_opts.update({
            "format": fmt,
            "merge_output_format": "mp4",
            "postprocessors": [
                *sponsorblock_pps,
                {"key": "FFmpegVideoConvertor", "preferedformat": "mp4"},
                *([{"key": "FFmpegMetadata"}] if Config.EMBED_METADATA else []),
                *([{"key": "FFmpegEmbedSubtitle"}] if Config.DOWNLOAD_SUBTITLES else []),
            ],
        })
    return ydl_opts

# yt-dlp/YouTube error messages are usually technical English that a regular
# user won't understand, so we translate the most common ones into a clear
# message instead of showing the raw text.
_ERROR_PATTERNS = [
    (r"private video", "This video is private and not publicly available."),
    (r"video (has been |is )?removed|violat", "This video was removed from YouTube (policy violation)."),
    (r"video unavailable", "This video is currently unavailable (it may have been deleted, or the link is wrong)."),
    (r"sign in to confirm (your age|you.?re not a bot)",
     "YouTube is asking for sign-in / bot confirmation — try enabling the Cookies option in Advanced Options."),
    (r"age.?restrict", "This video is age-restricted — enable the Cookies option in Advanced Options to download it."),
    (r"this live event will begin|premieres in", "This is a live event that hasn't started yet, or a premiere that hasn't aired."),
    (r"copyright", "This video was removed due to a copyright claim."),
    (r"not available in your country|geo.?restrict", "This video is geo-blocked in your country."),
    (r"requested format is not available", "The requested quality isn't available for this specific video — try a different quality."),
    (r"members?-only|join this channel", "This video is for channel members only."),
    (r"HTTP Error 429|too many requests", "YouTube temporarily throttled requests due to too many concurrent downloads — lower the concurrent downloads count and try again shortly."),
]

def friendly_error(raw_error: str) -> str:
    """Tries to translate a yt-dlp error into a clear message; if nothing matches, returns the original text."""
    low = raw_error.lower()
    for pattern, message in _ERROR_PATTERNS:
        if re.search(pattern, low):
            return message
    return raw_error

# "Permanent" errors — retrying won't help because the cause isn't temporary
# (the video itself is deleted/private/violates a policy/etc.), so we fail
# immediately instead of wasting time on retries that will fail again anyway.
_PERMANENT_ERROR_PATTERNS = [
    r"private video", r"video (has been |is )?removed|violat", r"video unavailable",
    r"copyright", r"members?-only|join this channel", r"not available in your country|geo.?restrict",
]

def _is_permanent_error(raw_error: str) -> bool:
    low = raw_error.lower()
    return any(re.search(p, low) for p in _PERMANENT_ERROR_PATTERNS)

def _strip_ansi(text: str) -> str:
    """Removes terminal color/escape codes. yt-dlp's progress-hook strings
    (_percent_str, _speed_str, _eta_str) are formatted for a color terminal
    and can carry raw ANSI escape sequences (e.g. '\\x1b[0;94m') even though
    we never print them to a terminal — showing that raw in the GUI looks
    like garbage, and parsing a percent out of it can silently fail."""
    return re.sub(r'\x1b\[[0-9;]*m', '', text or '').strip()


def _calc_progress_percent(d: dict) -> float:
    """
    Computes download percent straight from byte counts instead of parsing
    yt-dlp's pre-formatted _percent_str, which is meant for terminal display
    and isn't a reliable machine-readable value (can carry ANSI codes, or
    just not be present yet on the first callback).
    """
    downloaded = d.get('downloaded_bytes') or 0
    total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
    if total > 0:
        return max(0.0, min(100.0, 100 * downloaded / total))
    try:
        return float(_strip_ansi(d.get('_percent_str', '0%')).replace('%', ''))
    except ValueError:
        return 0.0

def _resolve_entry_url(entry: Dict) -> Optional[str]:
    """Get a downloadable URL from a (possibly flat) playlist entry dict."""
    if entry.get('webpage_url'):
        return entry['webpage_url']
    if entry.get('url'):
        u = entry['url']
        # flat playlist entries sometimes give just the id or a bare url
        if u.startswith('http'):
            return u
        return f"https://www.youtube.com/watch?v={u}"
    if entry.get('id'):
        return f"https://www.youtube.com/watch?v={entry['id']}"
    return None

def download_single_item(
    url: str,
    format_choice: str,
    quality_value: str,
    output_folder: str,
    on_event: Callable[..., None],
    history: Optional[set] = None,
    cancel_event: Optional[threading.Event] = None,
    history_lock: Optional[threading.Lock] = None,
) -> bool:
    """Download a single video/audio item, or every entry of a playlist."""
    attempt = 0
    normalized_url = normalize_youtube_url(url)

    while attempt < Config.MAX_RETRIES:
        if cancel_event and cancel_event.is_set():
            on_event("log", text=f"Cancelled before starting: {url}")
            return False
        try:
            # extract_flat='in_playlist' makes playlist probing fast (only listing,
            # not fully resolving every video) while leaving single-video URLs untouched.
            probe_opts = {'quiet': True, 'ignoreerrors': True, 'extract_flat': 'in_playlist'}
            if Config.COOKIES_FROM_BROWSER:
                probe_opts['cookiesfrombrowser'] = (Config.COOKIES_FROM_BROWSER,)
            with yt_dlp.YoutubeDL(probe_opts) as ydl:
                info = ydl.extract_info(normalized_url, download=False)
                if not info:
                    on_event("log", text=f"Failed to extract info for: {normalized_url}")
                    return False

                is_playlist = info.get('_type') == 'playlist'
                entries = info.get('entries') or [info] if is_playlist else [info]
                entries = [e for e in entries if e]
                total_entries = len(entries)

                if total_entries == 0:
                    on_event("log", text=f"Playlist is empty or unavailable: {normalized_url}")
                    return False

                display_title = info.get('title') or url
                if is_playlist:
                    display_title = f"📃 {display_title} ({total_entries} videos)"
                on_event("item_meta", url=url, title=display_title, total=total_entries)

                # One shared folder for the whole playlist (named after the playlist),
                # or a per-channel folder for a standalone video — computed once,
                # not per entry (flat playlist entries don't carry channel info anyway).
                if is_playlist:
                    folder_name = sanitize_filename(info.get('title') or 'Playlist')
                    output_path = os.path.join(output_folder, folder_name)
                else:
                    output_path = os.path.join(output_folder, get_channel_folder_name(info))
                os.makedirs(output_path, exist_ok=True)

                all_ok = True
                done_count = 0
                for entry in entries:
                    if cancel_event and cancel_event.is_set():
                        on_event("log", text=f"Cancelled during download: {url}")
                        return False

                    entry_url = _resolve_entry_url(entry)
                    entry_title = entry.get('title') or entry.get('id') or entry_url
                    video_id = entry.get('id')

                    if not entry_url:
                        on_event("log", text=f"Could not resolve a URL for item: {entry_title}")
                        all_ok = False
                        done_count += 1
                        continue

                    already_done = False
                    if history is not None:
                        if history_lock:
                            with history_lock:
                                already_done = video_id in history
                        else:
                            already_done = video_id in history
                    if already_done:
                        done_count += 1
                        on_event("log", text=f"Skipping (already downloaded before): {entry_title}")
                        on_event("item_progress", url=url, percent=100 * done_count / total_entries,
                                  speed="-", eta="-", status=f"Skipped {done_count}/{total_entries}")
                        continue

                    ydl_opts = prepare_ydl_options(format_choice, quality_value, output_path)

                    def _hook(d, _url=url, _done=done_count, _total=total_entries):
                        if d["status"] == "downloading":
                            pct = _calc_progress_percent(d)
                            overall = (100 * _done + pct) / max(_total, 1)
                            on_event("item_progress", url=_url, percent=overall,
                                      speed=_strip_ansi(d.get('_speed_str', '?')),
                                      eta=_strip_ansi(d.get('_eta_str', '?')),
                                      status=f"{_done + 1}/{_total}")
                        elif d["status"] == "finished":
                            on_event("log", text=f"Ready: {os.path.basename(d.get('filename', ''))}")

                    def _pp_hook(d, _url=url, _done=done_count, _total=total_entries):
                        # Without this, the progress bar just sits still at ~100% while
                        # ffmpeg converts/embeds thumbnail/removes sponsor segments —
                        # which for a long video can take a noticeable amount of time
                        # and looks like the app has frozen. Surface that it's still working.
                        if d.get("status") == "started":
                            pp_name = d.get("postprocessor", "Processing")
                            on_event("item_progress", url=_url, percent=(100 * (_done + 1)) / max(_total, 1),
                                      speed="-", eta="-", status=f"{pp_name}...")

                    ydl_opts["progress_hooks"] = [_hook]
                    ydl_opts["postprocessor_hooks"] = [_pp_hook]

                    entry_ok = False
                    last_entry_error = None
                    for entry_attempt in range(1, Config.MAX_RETRIES + 1):
                        if cancel_event and cancel_event.is_set():
                            on_event("log", text=f"Cancelled during download: {url}")
                            return False
                        try:
                            with yt_dlp.YoutubeDL(ydl_opts) as item_ydl:
                                item_ydl.download([entry_url])
                            entry_ok = True
                            break
                        except DownloadError as e:
                            last_entry_error = str(e)
                            logging.warning(f"Failed entry {entry_url} (attempt {entry_attempt}): {e}")
                            if _is_permanent_error(last_entry_error):
                                # No point retrying (video is deleted/private/etc.)
                                break
                            if entry_attempt < Config.MAX_RETRIES:
                                # Exponential backoff (1s, 2s, 4s, capped at 8s) instead of
                                # retrying instantly — an immediate retry is exactly what
                                # triggers YouTube's 429 "too many requests" throttling.
                                delay = min(2 ** (entry_attempt - 1), 8)
                                on_event("log", text=(
                                    f"Attempt {entry_attempt}/{Config.MAX_RETRIES} failed for {entry_title} "
                                    f"— retrying in {delay}s..."
                                ))
                                if cancel_event:
                                    cancel_event.wait(delay)
                                else:
                                    time.sleep(delay)

                    if entry_ok:
                        if history is not None and video_id:
                            if history_lock:
                                with history_lock:
                                    history.add(video_id)
                            else:
                                history.add(video_id)
                        done_count += 1
                        on_event("item_progress", url=url, percent=100 * done_count / total_entries,
                                  speed="-", eta="-", status=f"{done_count}/{total_entries}")
                    else:
                        on_event("log", text=f"Item failed: {entry_title} — {friendly_error(last_entry_error or 'Unknown error')}")
                        all_ok = False
                        done_count += 1

                return all_ok

        except DownloadError as e:
            attempt += 1
            if attempt < Config.MAX_RETRIES:
                delay = min(2 ** (attempt - 1), 8)
                on_event("log", text=f"Attempt {attempt}/{Config.MAX_RETRIES} failed for {normalized_url}: {friendly_error(str(e))} — retrying in {delay}s...")
                if cancel_event:
                    cancel_event.wait(delay)
                else:
                    time.sleep(delay)
            else:
                on_event("log", text=f"Attempt {attempt}/{Config.MAX_RETRIES} failed for {normalized_url}: {friendly_error(str(e))}")
        except Exception as e:
            logging.error(f"Unexpected error downloading {normalized_url}: {e}")
            on_event("log", text=f"Unexpected error: {e}")
            return False

    return False

def download_multiple_items(
    urls: List[str],
    format_choice: str,
    quality_value: str,
    output_folder: str,
    on_event: Callable[..., None],
    skip_history: bool = True,
    max_workers: int = 4,
    cancel_event: Optional[threading.Event] = None,
) -> int:
    success_count = 0
    history = load_history() if skip_history else None
    # One shared lock across all threads so reading/writing the history set
    # stays safe when multiple downloads run at the same time.
    history_lock = threading.Lock() if history is not None else None

    download_func = partial(
        download_single_item,
        format_choice=format_choice,
        quality_value=quality_value,
        output_folder=output_folder,
        on_event=on_event,
        history=history,
        cancel_event=cancel_event,
        history_lock=history_lock,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_url = {executor.submit(download_func, url): url for url in urls}
        for future in concurrent.futures.as_completed(future_to_url):
            url = future_to_url[future]
            try:
                if future.result():
                    success_count += 1
                    on_event("item_done", url=url, success=True)
                else:
                    on_event("item_done", url=url, success=False)
            except Exception as e:
                logging.error(f"Exception occurred for {url}: {e}")
                on_event("item_done", url=url, success=False)
                on_event("log", text=f"Error for {url}: {e}")

    if history is not None:
        save_history(history)

    on_event("all_done", success_count=success_count, total=len(urls))
    return success_count

# ========== GUI THEME ==========
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("dark-blue")

THEME = {
    "bg": "#0f0f17",
    "surface": "#1a1a26",
    "surface2": "#22222f",
    "border": "#2c2c3d",
    "accent": "#7c5cff",
    "accent_hover": "#6a4ae8",
    "accent2": "#ff3d68",
    "text": "#eaeaf0",
    "muted": "#8b8b9e",
    "success": "#2ecc71",
    "error": "#ff5c6c",
    "warning": "#f5a623",
}

FONT_FAMILY = "Segoe UI" if sys.platform.startswith("win") else "Helvetica"

def _pct_color(p: float) -> str:
    if p >= 100:
        return THEME["success"]
    if p <= 0:
        return THEME["muted"]
    return THEME["accent"]

# ========== PROGRESS ROW WIDGET ==========
class DownloadRow(ctk.CTkFrame):
    def __init__(self, master, url: str, **kwargs):
        super().__init__(master, fg_color=THEME["surface2"], corner_radius=12, **kwargs)
        self.url = url
        self.grid_columnconfigure(0, weight=1)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=14, pady=(10, 2))
        top.grid_columnconfigure(0, weight=1)

        self.title_label = ctk.CTkLabel(
            top, text=url, anchor="w", font=(FONT_FAMILY, 13, "bold"),
            text_color=THEME["text"], wraplength=520, justify="left",
        )
        self.title_label.grid(row=0, column=0, sticky="ew")

        self.status_label = ctk.CTkLabel(
            top, text="Waiting...", anchor="e", font=(FONT_FAMILY, 11),
            text_color=THEME["muted"],
        )
        self.status_label.grid(row=0, column=1, sticky="e", padx=(8, 0))

        self.progress = ctk.CTkProgressBar(
            self, height=10, corner_radius=6, progress_color=THEME["accent"],
            fg_color=THEME["border"],
        )
        self.progress.grid(row=1, column=0, sticky="ew", padx=14, pady=(4, 4))
        self.progress.set(0)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.grid(row=2, column=0, sticky="ew", padx=14, pady=(0, 10))
        bottom.grid_columnconfigure(0, weight=1)

        self.meta_label = ctk.CTkLabel(
            bottom, text="", anchor="w", font=(FONT_FAMILY, 10),
            text_color=THEME["muted"],
        )
        self.meta_label.grid(row=0, column=0, sticky="w")

    def set_title(self, title: str):
        self.title_label.configure(text=title)

    def set_progress(self, percent: float, speed: str = "", eta: str = "", status: str = ""):
        percent = max(0.0, min(100.0, percent))
        self.progress.configure(progress_color=_pct_color(percent))
        self.progress.set(percent / 100.0)
        if status:
            self.status_label.configure(text=status)
        meta_parts = []
        if speed and speed != "-":
            meta_parts.append(f"Speed: {speed}")
        if eta and eta != "-":
            meta_parts.append(f"ETA: {eta}")
        self.meta_label.configure(text="   ".join(meta_parts))

    def set_done(self, success: bool):
        if success:
            self.progress.configure(progress_color=THEME["success"])
            self.progress.set(1.0)
            self.status_label.configure(text="✓ Done", text_color=THEME["success"])
        else:
            self.progress.configure(progress_color=THEME["error"])
            self.status_label.configure(text="✗ Failed", text_color=THEME["error"])

# ========== MAIN APPLICATION ==========
class YTDownloaderApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("YouTube Downloader Pro")
        self.geometry("980x720")
        self.minsize(860, 620)
        self.configure(fg_color=THEME["bg"])

        self.event_queue: "queue.Queue" = queue.Queue()
        self.cancel_event = threading.Event()
        self.is_downloading = False
        self.rows: Dict[str, DownloadRow] = {}
        self.deps_ready = False
        self._clipboard_watch_enabled = False
        self._last_clipboard = ""

        self._build_ui()
        self._load_saved_settings()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_queue)
        self.after(1500, self._poll_clipboard)
        threading.Thread(target=self._startup_checks, daemon=True).start()

    # ---------- SETTINGS PERSISTENCE ----------
    def _load_saved_settings(self):
        """Restores the last settings the user used (quality, folder, concurrent downloads...)."""
        settings = load_settings()
        if not settings:
            return
        try:
            fmt_label = "🎬 Video" if settings.get("format") == "video" else "🎵 Audio"
            self.format_switch.set(fmt_label)
            self._on_format_change(fmt_label)

            quality_label = settings.get("quality_label")
            if quality_label and quality_label in self.quality_menu.cget("values"):
                self.quality_menu.set(quality_label)

            output_folder = settings.get("output_folder")
            if output_folder:
                self.output_entry.delete(0, "end")
                self.output_entry.insert(0, output_folder)

            self.skip_history_var.set(settings.get("skip_history", True))

            workers = settings.get("workers")
            if workers:
                self.workers_slider.set(workers)
                self.workers_value_label.configure(text=str(int(workers)))

            cookies_label = settings.get("cookies_label")
            if cookies_label and cookies_label in self.cookies_menu.cget("values"):
                self.cookies_menu.set(cookies_label)
                self._on_cookies_change(cookies_label)

            self.sponsorblock_var.set(settings.get("sponsorblock", False))
            self._on_sponsorblock_change()
            self.subtitles_var.set(settings.get("subtitles", False))
            self._on_subtitles_change()
            self.clipboard_watch_var.set(settings.get("clipboard_watch", False))
            self._on_clipboard_watch_change()
        except Exception as e:
            logging.warning(f"Could not apply saved settings: {e}")

    def _save_current_settings(self):
        try:
            save_settings({
                "format": "video" if "Video" in self.format_switch.get() else "audio",
                "quality_label": self.quality_menu.get(),
                "output_folder": self.output_entry.get().strip(),
                "skip_history": self.skip_history_var.get(),
                "workers": int(self.workers_slider.get()),
                "cookies_label": self.cookies_menu.get(),
                "sponsorblock": self.sponsorblock_var.get(),
                "subtitles": self.subtitles_var.get(),
                "clipboard_watch": self.clipboard_watch_var.get(),
            })
        except Exception as e:
            logging.warning(f"Could not save current settings: {e}")

    def _on_close(self):
        self._save_current_settings()
        self.destroy()

    # ---------- UI BUILD ----------
    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self._build_header()

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=20, pady=(0, 8))
        body.grid_columnconfigure(0, weight=1, uniform="col")
        body.grid_columnconfigure(1, weight=1, uniform="col")
        body.grid_rowconfigure(0, weight=1)

        self._build_settings_panel(body)
        self._build_progress_panel(body)

        self._build_statusbar()

    def _build_header(self):
        header = ctk.CTkFrame(self, fg_color=THEME["surface"], corner_radius=0, height=84)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        header.grid_propagate(False)

        badge = ctk.CTkLabel(
            header, text="▶", width=48, height=48, corner_radius=14,
            fg_color=THEME["accent2"], text_color="white", font=(FONT_FAMILY, 20, "bold"),
        )
        badge.grid(row=0, column=0, rowspan=2, padx=(20, 14), pady=18)

        title = ctk.CTkLabel(
            header, text="YouTube Downloader Pro", anchor="w",
            font=(FONT_FAMILY, 20, "bold"), text_color=THEME["text"],
        )
        title.grid(row=0, column=1, sticky="sw", pady=(16, 0))

        subtitle = ctk.CTkLabel(
            header, text="Download audio and video from YouTube in high quality — fast and easy",
            anchor="w", font=(FONT_FAMILY, 12), text_color=THEME["muted"],
        )
        subtitle.grid(row=1, column=1, sticky="nw", pady=(0, 16))

        self.deps_dot = ctk.CTkLabel(
            header, text="●", font=(FONT_FAMILY, 16), text_color=THEME["warning"],
        )
        self.deps_dot.grid(row=0, column=2, rowspan=2, padx=(0, 8))
        self.deps_label = ctk.CTkLabel(
            header, text="Checking requirements...", font=(FONT_FAMILY, 11), text_color=THEME["muted"],
        )
        self.deps_label.grid(row=0, column=3, rowspan=2, padx=(0, 24))

    def _build_settings_panel(self, parent):
        panel = ctk.CTkScrollableFrame(parent, fg_color=THEME["surface"], corner_radius=16, label_text="")
        panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        panel.grid_columnconfigure(0, weight=1)

        self._section_title(panel, "Links", 0)
        self.url_box = ctk.CTkTextbox(
            panel, height=120, corner_radius=10, fg_color=THEME["surface2"],
            border_width=1, border_color=THEME["border"], font=(FONT_FAMILY, 12),
        )
        self.url_box.grid(row=1, column=0, sticky="ew", pady=(4, 2))
        self.url_box.insert("1.0", "Paste YouTube links here — one link per line\nExample: https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        self.url_box.bind("<FocusIn>", self._clear_placeholder)

        hint = ctk.CTkLabel(
            panel, text="Supports single videos, Shorts, and full playlists",
            font=(FONT_FAMILY, 10), text_color=THEME["muted"], anchor="w",
        )
        hint.grid(row=2, column=0, sticky="ew", pady=(0, 14))

        self._section_title(panel, "Download type", 3)
        self.format_var = ctk.StringVar(value="audio")
        self.format_switch = ctk.CTkSegmentedButton(
            panel, values=["🎵 Audio", "🎬 Video"],
            command=self._on_format_change,
            fg_color=THEME["surface2"], selected_color=THEME["accent"],
            selected_hover_color=THEME["accent_hover"], unselected_color=THEME["surface2"],
            font=(FONT_FAMILY, 12, "bold"), height=38,
        )
        self.format_switch.set("🎵 Audio")
        self.format_switch.grid(row=4, column=0, sticky="ew", pady=(4, 14))

        self._section_title(panel, "Quality", 5)
        self.quality_menu = ctk.CTkOptionMenu(
            panel, values=[q[0] for q in QUALITY_PRESETS["audio"]],
            fg_color=THEME["surface2"], button_color=THEME["accent"],
            button_hover_color=THEME["accent_hover"], dropdown_fg_color=THEME["surface2"],
            font=(FONT_FAMILY, 12), height=38,
        )
        self.quality_menu.set("320 kbps")
        self.quality_menu.grid(row=6, column=0, sticky="ew", pady=(4, 14))

        self._section_title(panel, "Save folder", 7)
        out_row = ctk.CTkFrame(panel, fg_color="transparent")
        out_row.grid(row=8, column=0, sticky="ew", pady=(4, 14))
        out_row.grid_columnconfigure(0, weight=1)
        self.output_entry = ctk.CTkEntry(
            out_row, fg_color=THEME["surface2"], border_width=1, border_color=THEME["border"],
            font=(FONT_FAMILY, 11), height=38,
        )
        self.output_entry.insert(0, Config.DEFAULT_AUDIO_FOLDER)
        self.output_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))
        browse_btn = ctk.CTkButton(
            out_row, text="Browse", width=90, height=38, fg_color=THEME["surface2"],
            hover_color=THEME["border"], border_width=1, border_color=THEME["border"],
            command=self._browse_folder,
        )
        browse_btn.grid(row=0, column=1)

        self._section_title(panel, "Advanced options", 9)
        opts = ctk.CTkFrame(panel, fg_color="transparent")
        opts.grid(row=10, column=0, sticky="ew", pady=(4, 4))
        opts.grid_columnconfigure(0, weight=1)

        self.skip_history_var = ctk.BooleanVar(value=True)
        skip_switch = ctk.CTkSwitch(
            opts, text="Skip files already downloaded before", variable=self.skip_history_var,
            font=(FONT_FAMILY, 12), progress_color=THEME["accent"],
        )
        skip_switch.grid(row=0, column=0, sticky="w", pady=6)

        workers_row = ctk.CTkFrame(opts, fg_color="transparent")
        workers_row.grid(row=1, column=0, sticky="ew", pady=(10, 4))
        workers_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            workers_row, text="Concurrent downloads", font=(FONT_FAMILY, 12), text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w")
        self.workers_value_label = ctk.CTkLabel(
            workers_row, text="4", font=(FONT_FAMILY, 12, "bold"), text_color=THEME["accent"],
        )
        self.workers_value_label.grid(row=0, column=1, sticky="e")
        self.workers_slider = ctk.CTkSlider(
            opts, from_=1, to=8, number_of_steps=7, progress_color=THEME["accent"],
            button_color=THEME["accent"], button_hover_color=THEME["accent_hover"],
            command=self._on_workers_change,
        )
        self.workers_slider.set(4)
        self.workers_slider.grid(row=2, column=0, sticky="ew", pady=(2, 4))

        cookies_row = ctk.CTkFrame(opts, fg_color="transparent")
        cookies_row.grid(row=3, column=0, sticky="ew", pady=(14, 4))
        cookies_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            cookies_row, text="Browser cookies (for private/age-restricted videos)",
            font=(FONT_FAMILY, 12), text_color=THEME["text"], anchor="w",
        ).grid(row=0, column=0, sticky="w", pady=(0, 4))
        self.cookies_menu = ctk.CTkOptionMenu(
            opts, values=["No cookies", "Chrome", "Firefox", "Edge", "Brave", "Opera"],
            fg_color=THEME["surface2"], button_color=THEME["accent"],
            button_hover_color=THEME["accent_hover"], dropdown_fg_color=THEME["surface2"],
            font=(FONT_FAMILY, 12), height=36, command=self._on_cookies_change,
        )
        self.cookies_menu.set("No cookies")
        self.cookies_menu.grid(row=4, column=0, sticky="ew", pady=(0, 4))
        cookies_hint = ctk.CTkLabel(
            opts, text="Only enable this if you see a \"sign-in required\" error — and close the selected browser first",
            font=(FONT_FAMILY, 9), text_color=THEME["muted"], anchor="w", justify="left", wraplength=380,
        )
        cookies_hint.grid(row=5, column=0, sticky="ew", pady=(0, 4))

        self.sponsorblock_var = ctk.BooleanVar(value=False)
        sponsorblock_switch = ctk.CTkSwitch(
            opts, text="Skip sponsor segments (SponsorBlock)", variable=self.sponsorblock_var,
            font=(FONT_FAMILY, 12), progress_color=THEME["accent"], command=self._on_sponsorblock_change,
        )
        sponsorblock_switch.grid(row=6, column=0, sticky="w", pady=(10, 2))

        self.subtitles_var = ctk.BooleanVar(value=False)
        subtitles_switch = ctk.CTkSwitch(
            opts, text="Download subtitles (.srt)", variable=self.subtitles_var,
            font=(FONT_FAMILY, 12), progress_color=THEME["accent"], command=self._on_subtitles_change,
        )
        subtitles_switch.grid(row=7, column=0, sticky="w", pady=(4, 2))

        self.clipboard_watch_var = ctk.BooleanVar(value=False)
        clipboard_switch = ctk.CTkSwitch(
            opts, text="Auto-add YouTube links copied to clipboard", variable=self.clipboard_watch_var,
            font=(FONT_FAMILY, 12), progress_color=THEME["accent"], command=self._on_clipboard_watch_change,
        )
        clipboard_switch.grid(row=8, column=0, sticky="w", pady=(4, 4))

        # action buttons
        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=11, column=0, sticky="ew", pady=(20, 4))
        actions.grid_columnconfigure((0, 1), weight=1)

        self.start_btn = ctk.CTkButton(
            actions, text="⬇  Start Download", height=46, corner_radius=12,
            fg_color=THEME["accent"], hover_color=THEME["accent_hover"],
            font=(FONT_FAMILY, 14, "bold"), command=self._start_download,
        )
        self.start_btn.grid(row=0, column=0, sticky="ew", padx=(0, 6))

        self.cancel_btn = ctk.CTkButton(
            actions, text="Cancel", height=46, corner_radius=12,
            fg_color=THEME["surface2"], hover_color=THEME["border"],
            border_width=1, border_color=THEME["error"], text_color=THEME["error"],
            font=(FONT_FAMILY, 14, "bold"), command=self._cancel_download, state="disabled",
        )
        self.cancel_btn.grid(row=0, column=1, sticky="ew", padx=(6, 0))

    def _build_progress_panel(self, parent):
        panel = ctk.CTkFrame(parent, fg_color=THEME["surface"], corner_radius=16)
        panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        header_row = ctk.CTkFrame(panel, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        header_row.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header_row, text="Progress", font=(FONT_FAMILY, 15, "bold"), text_color=THEME["text"],
        ).grid(row=0, column=0, sticky="w")
        self.summary_label = ctk.CTkLabel(
            header_row, text="Ready to download", font=(FONT_FAMILY, 11), text_color=THEME["muted"],
        )
        self.summary_label.grid(row=0, column=1, sticky="e")

        self.progress_list = ctk.CTkScrollableFrame(panel, fg_color="transparent")
        self.progress_list.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        self.progress_list.grid_columnconfigure(0, weight=1)

        self.empty_state = ctk.CTkLabel(
            self.progress_list, text="No downloads yet 👋\nAdd a link and hit start",
            font=(FONT_FAMILY, 13), text_color=THEME["muted"], justify="center",
        )
        self.empty_state.grid(row=0, column=0, pady=60)

        log_header = ctk.CTkFrame(panel, fg_color="transparent")
        log_header.grid(row=2, column=0, sticky="ew", padx=16, pady=(4, 2))
        ctk.CTkLabel(
            log_header, text="Event log", font=(FONT_FAMILY, 12, "bold"), text_color=THEME["muted"],
        ).grid(row=0, column=0, sticky="w")

        self.log_box = ctk.CTkTextbox(
            panel, height=110, fg_color=THEME["surface2"], corner_radius=10,
            font=("Consolas" if sys.platform.startswith("win") else "Menlo", 10),
            text_color=THEME["muted"], border_width=1, border_color=THEME["border"],
        )
        self.log_box.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 14))
        self.log_box.configure(state="disabled")

    def _build_statusbar(self):
        bar = ctk.CTkFrame(self, fg_color=THEME["surface"], corner_radius=0, height=30)
        bar.grid(row=2, column=0, sticky="ew")
        bar.grid_propagate(False)
        self.status_text = ctk.CTkLabel(
            bar, text=f"Error log: {Config.LOG_FILE}", font=(FONT_FAMILY, 10), text_color=THEME["muted"],
        )
        self.status_text.pack(side="left", padx=14)

        open_folder_btn = ctk.CTkButton(
            bar, text="Open downloads folder", width=150, height=22, fg_color="transparent",
            hover_color=THEME["surface2"], text_color=THEME["accent"], font=(FONT_FAMILY, 10),
            command=self._open_output_folder,
        )
        open_folder_btn.pack(side="right", padx=10, pady=2)

        check_update_btn = ctk.CTkButton(
            bar, text="Check for yt-dlp update", width=150, height=22, fg_color="transparent",
            hover_color=THEME["surface2"], text_color=THEME["accent"], font=(FONT_FAMILY, 10),
            command=self._check_ytdlp_update_now,
        )
        check_update_btn.pack(side="right", padx=(0, 4), pady=2)

    def _check_ytdlp_update_now(self):
        def worker():
            def status(msg):
                self.event_queue.put(("log", {"text": msg}))
            ensure_ytdlp_up_to_date(status_cb=status, force=True)
        threading.Thread(target=worker, daemon=True).start()

    def _section_title(self, parent, text, row):
        lbl = ctk.CTkLabel(
            parent, text=text, font=(FONT_FAMILY, 12, "bold"), text_color=THEME["muted"], anchor="w",
        )
        lbl.grid(row=row, column=0, sticky="ew", pady=(6, 0))

    # ---------- EVENT HANDLERS ----------
    def _clear_placeholder(self, _event=None):
        current = self.url_box.get("1.0", "end-1c")
        if current.startswith("Paste YouTube links here"):
            self.url_box.delete("1.0", "end")

    def _on_format_change(self, value: str):
        fmt = "audio" if "Audio" in value else "video"
        labels = [q[0] for q in QUALITY_PRESETS[fmt]]
        self.quality_menu.configure(values=labels)
        # Sensible default per type: 320 kbps for audio, 720p for video
        # (not always "Best quality").
        default_label = "320 kbps" if fmt == "audio" else "720p"
        self.quality_menu.set(default_label if default_label in labels else labels[-1])
        default_folder = Config.DEFAULT_AUDIO_FOLDER if fmt == "audio" else Config.DEFAULT_VIDEO_FOLDER
        current = self.output_entry.get().strip()
        if current in (Config.DEFAULT_AUDIO_FOLDER, Config.DEFAULT_VIDEO_FOLDER, ""):
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, default_folder)

    def _on_workers_change(self, value):
        self.workers_value_label.configure(text=str(int(value)))

    def _on_cookies_change(self, value: str):
        mapping = {
            "No cookies": None, "Chrome": "chrome", "Firefox": "firefox",
            "Edge": "edge", "Brave": "brave", "Opera": "opera",
        }
        Config.COOKIES_FROM_BROWSER = mapping.get(value)

    def _on_sponsorblock_change(self):
        Config.SPONSORBLOCK_REMOVE = self.sponsorblock_var.get()

    def _on_subtitles_change(self):
        Config.DOWNLOAD_SUBTITLES = self.subtitles_var.get()

    def _on_clipboard_watch_change(self):
        self._clipboard_watch_enabled = self.clipboard_watch_var.get()
        if self._clipboard_watch_enabled:
            try:
                self._last_clipboard = self.clipboard_get()
            except Exception:
                self._last_clipboard = ""
            self._append_log("Clipboard watching on — copy a YouTube link and it'll be added automatically.")

    def _poll_clipboard(self):
        if getattr(self, "_clipboard_watch_enabled", False):
            try:
                current = self.clipboard_get()
            except Exception:
                current = None
            if current and current != self._last_clipboard:
                self._last_clipboard = current
                candidate = current.strip()
                if is_valid_youtube_url(candidate):
                    existing = self.url_box.get("1.0", "end-1c")
                    if candidate not in existing:
                        self._clear_placeholder()
                        text = self.url_box.get("1.0", "end-1c")
                        prefix = "\n" if text.strip() else ""
                        self.url_box.insert("end", f"{prefix}{candidate}")
                        self._append_log(f"Link detected on clipboard, added: {candidate}")
        self.after(1500, self._poll_clipboard)

    def _browse_folder(self):
        folder = filedialog.askdirectory(initialdir=self.output_entry.get() or os.path.expanduser("~"))
        if folder:
            self.output_entry.delete(0, "end")
            self.output_entry.insert(0, folder)

    def _open_output_folder(self):
        folder = self.output_entry.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showinfo("Notice", "This folder doesn't exist yet.")
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(folder)  # noqa
            elif sys.platform == "darwin":
                subprocess.run(["open", folder])
            else:
                subprocess.run(["xdg-open", folder])
        except Exception as e:
            messagebox.showerror("Error", f"Could not open the folder: {e}")

    _MAX_LOG_LINES = 500

    def _append_log(self, text: str):
        ts = datetime.now().strftime("%H:%M:%S")
        self.log_box.configure(state="normal")
        self.log_box.insert("end", f"[{ts}] {text}\n")
        # Cap the log so a very long session (big playlist, many retries)
        # doesn't let the textbox grow without bound.
        line_count = int(self.log_box.index("end-1c").split(".")[0])
        if line_count > self._MAX_LOG_LINES:
            excess = line_count - self._MAX_LOG_LINES
            self.log_box.delete("1.0", f"{excess + 1}.0")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _set_deps_status(self, ok: bool, text: str):
        self.deps_dot.configure(text_color=THEME["success"] if ok else THEME["error"])
        self.deps_label.configure(text=text)

    # ---------- STARTUP CHECKS (background thread) ----------
    def _startup_checks(self):
        def status(msg):
            self.event_queue.put(("log", {"text": msg}))

        setup_logging()
        ok = validate_ffmpeg(status_cb=status)
        self.deps_ready = ok
        if ok:
            self.event_queue.put(("deps_status", {"ok": True, "text": f"Ready — ffmpeg: {Config.FFMPEG_PATH}"}))
        else:
            self.event_queue.put((
                "deps_status",
                {"ok": False, "text": "ffmpeg not found and couldn't be auto-installed — check the event log"},
            ))

    # ---------- DOWNLOAD FLOW ----------
    def _collect_urls(self) -> List[str]:
        raw = self.url_box.get("1.0", "end-1c")
        lines = [l.strip() for l in raw.splitlines() if l.strip()]
        lines = [l for l in lines if not l.startswith("Paste YouTube links") and not l.startswith("Example:")]
        valid, invalid = [], []
        seen = set()
        for l in lines:
            if is_valid_youtube_url(l):
                normalized = normalize_youtube_url(l)
                # De-duplicate: two rows keyed by the same URL would otherwise
                # collide in self.rows and throw off the "X / Y complete" count.
                if normalized not in seen:
                    seen.add(normalized)
                    valid.append(normalized)
            else:
                invalid.append(l)
        if invalid:
            messagebox.showwarning(
                "Invalid links",
                "These links will be ignored because they're not valid YouTube links:\n" + "\n".join(invalid[:10]),
            )
        return valid

    def _start_download(self):
        if self.is_downloading:
            return
        if not self.deps_ready:
            messagebox.showwarning("Not ready yet", "Requirements (ffmpeg) aren't ready yet — wait a moment or check the event log.")
            return

        urls = self._collect_urls()
        if not urls:
            messagebox.showwarning("No links", "Add at least one YouTube link.")
            return

        output_folder = self.output_entry.get().strip() or Config.DEFAULT_AUDIO_FOLDER
        try:
            os.makedirs(output_folder, exist_ok=True)
        except OSError as e:
            messagebox.showerror("Error", f"Could not create the save folder: {e}")
            return

        format_choice = "audio" if "Audio" in self.format_switch.get() else "video"
        quality_label = self.quality_menu.get()
        quality_value = next(
            (v for lbl, v in QUALITY_PRESETS[format_choice] if lbl == quality_label), "best"
        )
        skip_history = self.skip_history_var.get()
        max_workers = int(self.workers_slider.get())
        self._save_current_settings()

        # reset UI state
        for child in list(self.progress_list.winfo_children()):
            child.destroy()
        self.rows.clear()
        for i, url in enumerate(urls):
            row = DownloadRow(self.progress_list, url=url)
            row.grid(row=i, column=0, sticky="ew", pady=6)
            self.rows[url] = row

        self.cancel_event.clear()
        self.is_downloading = True
        self.start_btn.configure(state="disabled", text="Downloading...")
        self.cancel_btn.configure(state="normal")
        self.summary_label.configure(text=f"0 / {len(urls)} complete")

        def on_event(event_type, **kwargs):
            self.event_queue.put((event_type, kwargs))

        def worker():
            download_multiple_items(
                urls, format_choice, quality_value, output_folder,
                on_event=on_event, skip_history=skip_history,
                max_workers=max_workers, cancel_event=self.cancel_event,
            )

        threading.Thread(target=worker, daemon=True).start()

    def _cancel_download(self):
        if not self.is_downloading:
            return
        self.cancel_event.set()
        self.cancel_btn.configure(state="disabled", text="Cancelling...")
        self._append_log(
            "Cancel requested — the file currently downloading will finish first "
            "(yt-dlp can't be interrupted mid-file), then no new items will start."
        )

    # ---------- QUEUE POLLING (main thread only touches widgets) ----------
    def _poll_queue(self):
        try:
            while True:
                event_type, data = self.event_queue.get_nowait()
                self._handle_event(event_type, data)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _handle_event(self, event_type: str, data: dict):
        if event_type == "log":
            self._append_log(data["text"])

        elif event_type == "deps_status":
            self._set_deps_status(data["ok"], data["text"])

        elif event_type == "item_meta":
            row = self.rows.get(data["url"])
            if row:
                row.set_title(data["title"])

        elif event_type == "item_progress":
            row = self.rows.get(data["url"])
            if row:
                row.set_progress(
                    percent=data["percent"], speed=data.get("speed", ""),
                    eta=data.get("eta", ""), status=data.get("status", ""),
                )

        elif event_type == "item_done":
            row = self.rows.get(data["url"])
            if row:
                row.set_done(data["success"])
            done = sum(1 for r in self.rows.values() if "✓" in r.status_label.cget("text") or "✗" in r.status_label.cget("text"))
            self.summary_label.configure(text=f"{done} / {len(self.rows)} complete")

        elif event_type == "all_done":
            self.is_downloading = False
            self.start_btn.configure(state="normal", text="⬇  Start Download")
            self.cancel_btn.configure(state="disabled", text="Cancel")
            success, total = data["success_count"], data["total"]
            self.summary_label.configure(text=f"Done: {success} / {total}")
            self._append_log(f"Download finished: {success}/{total} succeeded.")
            if success < total and not self.cancel_event.is_set():
                messagebox.showwarning("Partially complete", f"{success} out of {total} succeeded. Check the event log for details.")
            elif success == total and total > 0:
                messagebox.showinfo("Done", f"All files downloaded successfully ({total}).")

# ========== ENTRY POINT ==========
def run_gui():
    app = YTDownloaderApp()
    app.mainloop()

def run_headless(args):
    """CLI mode for automation/scripting without opening the GUI."""
    setup_logging()
    if not validate_ffmpeg():
        print("FFmpeg is unavailable and couldn't be auto-installed. Install it manually and try again.")
        sys.exit(1)

    def on_event(event_type, **kwargs):
        if event_type == "log":
            print(kwargs["text"])
        elif event_type == "item_done":
            status = "✓" if kwargs["success"] else "✗"
            print(f"{status} {kwargs['url']}")
        elif event_type == "all_done":
            print(f"Done: {kwargs['success_count']}/{kwargs['total']}")

    quality_map = {v: v for _, v in QUALITY_PRESETS[args.format]}
    quality_value = args.quality.lower().replace("k", "").replace("p", "")
    if quality_value not in quality_map and quality_value != "best":
        print(f"Invalid quality: {args.quality}")
        sys.exit(1)

    output_folder = args.output or (
        Config.DEFAULT_AUDIO_FOLDER if args.format == "audio" else Config.DEFAULT_VIDEO_FOLDER
    )
    os.makedirs(output_folder, exist_ok=True)

    download_multiple_items(
        args.urls, args.format, quality_value, output_folder,
        on_event=on_event, skip_history=not args.no_skip,
        max_workers=args.workers or Config.MAX_CONCURRENT_DOWNLOADS,
    )

def main():
    parser = argparse.ArgumentParser(
        description="YouTube Downloader Pro — modern desktop GUI (or headless mode via -u)"
    )
    parser.add_argument("-f", "--format", choices=["audio", "video"], help="Download type (headless mode)")
    parser.add_argument("-q", "--quality", help="Quality (e.g. 320k, 1080p, or best)")
    parser.add_argument("-u", "--urls", nargs="+", help="YouTube links (enables headless mode)")
    parser.add_argument("-o", "--output", help="Save folder")
    parser.add_argument("--no-skip", action="store_true", help="Re-download files even if already in history")
    parser.add_argument("--workers", type=int, help="Number of concurrent downloads")
    args = parser.parse_args()

    if args.urls:
        missing = []
        if not args.format:
            missing.append("-f/--format")
        if not args.quality:
            missing.append("-q/--quality")
        if missing:
            print(f"Error: -u/--urls was given but {', '.join(missing)} is also required for headless mode.")
            print("Example: python youtube_downloader_pro.py -u https://youtu.be/xxxx -f audio -q 320")
            sys.exit(1)
        run_headless(args)
    else:
        run_gui()

if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled by user.")
