#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Yank for Resolve  —  v2.0 (Studio-aware)
========================================
Paste a link -> the clip lands in your Media Pool.

Runs inside DaVinci Resolve via Workspace > Scripts, headless from a terminal
with --cli, or as a background watcher with --watch.

Studio features are detected at runtime via GetProductName() and light up
automatically. On free Resolve they stay dark and nothing breaks.

  Studio only:
    - external scripting (so --cli and --watch can import into a live project)
    - proxy generation + LinkProxyMedia (edit light, render from the original)
    - CreateSubtitlesFromAudio (Neural Engine transcription)

Built on yt-dlp + ffmpeg. Written from scratch.

Author: built for JMV Productions
License: do whatever you want with it.
"""

import os
import re
import sys
import json
import time
import queue
import shutil
import platform
import argparse
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

APP_NAME = "Yank for Resolve"
VERSION = "2.0.0"

IS_WIN = platform.system() == "Windows"
IS_MAC = platform.system() == "Darwin"

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------

SETTINGS_DIR = os.path.join(os.path.expanduser("~"), ".yank_for_resolve")
SETTINGS_PATH = os.path.join(SETTINGS_DIR, "settings.json")
WATCH_DEFAULT = os.path.join(SETTINGS_DIR, "queue.txt")


def default_download_dir():
    home = os.path.expanduser("~")
    return os.path.join(home, "Movies" if IS_MAC else "Videos", "Yank Downloads")


QUALITY_CHOICES = ["Best available", "2160p", "1440p", "1080p", "720p", "480p"]
QUALITY_HEIGHTS = {
    "Best available": None, "2160p": 2160, "1440p": 1440,
    "1080p": 1080, "720p": 720, "480p": 480,
}

MODE_CHOICES = ["Video + Audio", "Video only (no audio)", "Audio only (WAV)", "Audio only (MP3)"]

TRANSCODE_CHOICES = [
    "Auto (only if Resolve can't read it)",
    "Never (keep as downloaded)",
    "Always - DNxHR HQ (.mov)",
    "Always - ProRes 422 (.mov)",
]

# Studio's H.264/H.265 hardware decode is solid, so the sane default there is
# "keep the original, generate a proxy" rather than "transcode everything".
PROXY_CHOICES = [
    "No proxies",
    "Half res - DNxHR LB",
    "Half res - ProRes Proxy",
    "720p - DNxHR LB",
    "720p - ProRes Proxy",
]

TIMELINE_CHOICES = ["Bin only", "Append to current timeline", "New timeline from clips"]

COOKIE_CHOICES = ["No cookies", "chrome", "safari", "firefox", "edge", "brave",
                  "chromium", "vivaldi", "opera"]

JOB_CHOICES = ["1", "2", "3", "4", "6"]

DEFAULTS = {
    "download_dir": default_download_dir(),
    "bin_name": "Yank Downloads",
    "quality": "1080p",
    "mode": MODE_CHOICES[0],
    "transcode": TRANSCODE_CHOICES[0],
    "proxy": PROXY_CHOICES[0],
    "timeline_mode": TIMELINE_CHOICES[0],
    "cookies": COOKIE_CHOICES[0],
    "jobs": "3",
    "subfolder_per_day": True,
    "delete_source_after_transcode": False,
    "skip_duplicates": True,
    "color_by_platform": True,
    "allow_playlists": False,
    "playlist_limit": 25,
    "auto_subtitles": False,
    "write_srt": False,
    "frame_accurate_sections": True,
    "watch_file": WATCH_DEFAULT,
    "ytdlp_path": "",
    "ffmpeg_path": "",
}


def load_settings():
    s = dict(DEFAULTS)
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as fh:
            saved = json.load(fh)
        if isinstance(saved, dict):
            for k in DEFAULTS:
                if k in saved:
                    s[k] = saved[k]
    except Exception:
        pass
    return s


def save_settings(s):
    try:
        os.makedirs(SETTINGS_DIR, exist_ok=True)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=2)
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Binary discovery
#
# Resolve launches from the GUI, so it inherits a stripped PATH that usually
# excludes /opt/homebrew/bin. That is the #1 reason a script like this reports
# "yt-dlp not found" while it works fine in Terminal.
# ----------------------------------------------------------------------------

EXTRA_BIN_DIRS = [
    "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/opt/local/bin",
    os.path.expanduser("~/.local/bin"), os.path.expanduser("~/bin"),
    os.path.expanduser("~/Library/Python/3.11/bin"),
    os.path.expanduser("~/Library/Python/3.12/bin"),
    "C:\\ProgramData\\chocolatey\\bin", "C:\\ffmpeg\\bin",
    os.path.expanduser("~\\AppData\\Local\\Microsoft\\WinGet\\Links"),
    os.path.expanduser("~\\scoop\\shims"),
]


def find_binary(name, override=""):
    if override:
        override = os.path.expanduser(override.strip())
        if os.path.isfile(override):
            return override
    hit = shutil.which(name)
    if hit:
        return hit
    exe = name + (".exe" if IS_WIN else "")
    for d in EXTRA_BIN_DIRS:
        cand = os.path.join(d, exe)
        if os.path.isfile(cand):
            return cand
    return None


def sibling_tool(binary_path, wanted):
    if not binary_path:
        return None
    cand = os.path.join(os.path.dirname(binary_path), wanted + (".exe" if IS_WIN else ""))
    return cand if os.path.isfile(cand) else find_binary(wanted)


def no_window_kwargs():
    if not IS_WIN:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    return {"startupinfo": si, "creationflags": 0x08000000}


# ----------------------------------------------------------------------------
# Resolve handles + tier detection
# ----------------------------------------------------------------------------

def _add_resolve_module_path():
    if IS_MAC:
        p = "/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting/Modules"
    elif IS_WIN:
        p = os.path.join(os.environ.get("PROGRAMDATA", "C:\\ProgramData"),
                         "Blackmagic Design", "DaVinci Resolve", "Support",
                         "Developer", "Scripting", "Modules")
    else:
        p = "/opt/resolve/Developer/Scripting/Modules"
    if os.path.isdir(p) and p not in sys.path:
        sys.path.append(p)


def get_bmd():
    g = globals()
    if "bmd" in g and g["bmd"] is not None:
        return g["bmd"]
    for mod in ("fusionscript", "BlackmagicFusion", "DaVinciResolveScript"):
        try:
            return __import__(mod)
        except ImportError:
            continue
    _add_resolve_module_path()
    try:
        return __import__("DaVinciResolveScript")
    except ImportError:
        return None


def get_resolve():
    g = globals()
    if "resolve" in g and g["resolve"] is not None:
        return g["resolve"]
    b = get_bmd()
    if b is None:
        return None
    try:
        return b.scriptapp("Resolve")
    except Exception:
        return None


_TIER_CACHE = {}


def studio_info():
    """Returns (is_studio, description). Cached — the answer can't change mid-run."""
    if "v" in _TIER_CACHE:
        return _TIER_CACHE["v"]
    r = get_resolve()
    out = (False, "Resolve not reachable")
    if r is not None:
        try:
            name = r.GetProductName() or "DaVinci Resolve"
            ver = r.GetVersionString() or ""
            out = ("studio" in name.lower(), "{} {}".format(name, ver).strip())
        except Exception:
            out = (False, "DaVinci Resolve (version unknown)")
    _TIER_CACHE["v"] = out
    return out


# ----------------------------------------------------------------------------
# URL specs — "https://... @ 1:30-2:45" trims to a section
# ----------------------------------------------------------------------------

TIME_RE = re.compile(r"^(?:(\d+):)?(?:(\d{1,2}):)?(\d{1,2}(?:\.\d+)?)$")
SPEC_SPLIT_RE = re.compile(r"\s+@\s+")

PLATFORM_COLORS = {
    "youtube": "Orange", "youtu.be": "Orange",
    "instagram": "Pink",
    "tiktok": "Teal",
    "twitter": "Blue", "x.com": "Blue",
    "vimeo": "Navy",
    "twitch": "Purple",
    "reddit": "Chocolate",
    "facebook": "Violet",
    "soundcloud": "Yellow",
}


def platform_of(url):
    u = url.lower()
    for key, color in PLATFORM_COLORS.items():
        if key in u:
            return key, color
    return "other", "Beige"


def parse_timecode(text):
    """'90' | '1:30' | '1:02:03' | '90.5' -> seconds, or None."""
    text = text.strip()
    if not text:
        return None
    m = TIME_RE.match(text)
    if not m:
        return None
    a, b, c = m.groups()
    parts = [p for p in (a, b, c) if p is not None]
    total = 0.0
    for p in parts:
        total = total * 60 + float(p)
    return total


def seconds_to_hms(sec):
    sec = max(0.0, float(sec))
    h = int(sec // 3600)
    m = int((sec % 3600) // 60)
    s = sec - h * 3600 - m * 60
    return "{:d}:{:02d}:{:06.3f}".format(h, m, s)


def parse_url_spec(line):
    """
    Accepts:
      https://...
      https://... @ 1:30-2:45      -> trimmed section
      https://... @ 1:30-          -> from 1:30 to the end
      https://...&t=90s            -> start hint from the URL itself
    Returns (url, start_seconds_or_None, end_seconds_or_None).
    """
    line = line.strip()
    start = end = None

    parts = SPEC_SPLIT_RE.split(line, maxsplit=1)
    url = parts[0].strip()

    if len(parts) == 2:
        rng = parts[1].strip()
        if "-" in rng:
            a, _, b = rng.partition("-")
            start = parse_timecode(a)
            end = parse_timecode(b)
        else:
            start = parse_timecode(rng)

    if start is None:
        m = re.search(r"[?&#]t=(\d+h)?(\d+m)?(\d+)s?\b", url)
        if m:
            h = int(m.group(1)[:-1]) if m.group(1) else 0
            mi = int(m.group(2)[:-1]) if m.group(2) else 0
            s = int(m.group(3))
            secs = h * 3600 + mi * 60 + s
            if secs > 0:
                start = float(secs)

    return url, start, end


# ----------------------------------------------------------------------------
# Download + transcode
# ----------------------------------------------------------------------------

PROGRESS_RE = re.compile(r"\[download\]\s+(\d{1,3}(?:\.\d+)?)%")
DEST_RE = re.compile(
    r"^\[(?:download|Merger|ExtractAudio)\]\s+(?:Destination:|Merging formats into)\s+\"?(.+?)\"?$")

UNFRIENDLY_VCODECS = {"vp8", "vp9", "vp09", "av1", "av01", "theora"}
UNFRIENDLY_ACODECS = {"opus", "vorbis"}
UNFRIENDLY_EXTS = {".webm", ".ogv", ".ogg", ".opus", ".flv"}


def build_format_selector(mode, quality):
    if mode.startswith("Audio"):
        return "ba/b"
    h = QUALITY_HEIGHTS.get(quality)
    hc = "[height<={}]".format(h) if h else ""
    if mode.startswith("Video only"):
        return "bv*[vcodec^=avc1]{hc}/bv*{hc}/bv*".format(hc=hc)
    return (
        "bv*[vcodec^=avc1]{hc}+ba[acodec^=mp4a]/"
        "bv*[vcodec^=avc1]{hc}+ba/"
        "bv*{hc}+ba/"
        "b{hc}/bv*+ba/b"
    ).format(hc=hc)


def build_ytdlp_command(url, cfg, ytdlp, out_dir, start=None, end=None):
    cmd = [
        ytdlp, "--newline", "--no-simulate", "--no-warnings", "--windows-filenames",
        "--print", "after_move:filepath",
        "--paths", out_dir,
        "-o", "%(title).80s [%(id)s].%(ext)s",
    ]

    if cfg.get("allow_playlists"):
        cmd += ["--yes-playlist", "--playlist-items",
                "1:{}".format(int(cfg.get("playlist_limit", 25)))]
    else:
        cmd.append("--no-playlist")

    mode = cfg["mode"]
    cmd += ["-f", build_format_selector(mode, cfg["quality"])]

    if mode == "Audio only (WAV)":
        cmd += ["-x", "--audio-format", "wav"]
    elif mode == "Audio only (MP3)":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        cmd += ["--merge-output-format", "mp4"]

    if start is not None or end is not None:
        a = seconds_to_hms(start) if start is not None else "0:00:00.000"
        b = seconds_to_hms(end) if end is not None else "inf"
        cmd += ["--download-sections", "*{}-{}".format(a, b)]
        if cfg.get("frame_accurate_sections", True):
            # Re-encodes the section so the cut lands on the exact frame.
            # Turn this off in settings.json if you'd rather have a fast,
            # keyframe-snapped cut with zero generation loss.
            cmd.append("--force-keyframes-at-cuts")

    if cfg.get("write_srt") and not mode.startswith("Audio"):
        cmd += ["--write-subs", "--write-auto-subs", "--sub-langs", "en.*",
                "--convert-subs", "srt"]

    if cfg["cookies"] and cfg["cookies"] != "No cookies":
        cmd += ["--cookies-from-browser", cfg["cookies"]]

    ffmpeg = find_binary("ffmpeg", cfg.get("ffmpeg_path", ""))
    if ffmpeg:
        cmd += ["--ffmpeg-location", os.path.dirname(ffmpeg)]

    cmd.append(url)
    return cmd


def probe_streams(ffprobe, path):
    if not ffprobe:
        return None
    try:
        r = subprocess.run([ffprobe, "-v", "quiet", "-print_format", "json",
                            "-show_streams", path],
                           capture_output=True, text=True, timeout=30, **no_window_kwargs())
        return json.loads(r.stdout).get("streams", [])
    except Exception:
        return None


def needs_transcode(path, ffprobe):
    if os.path.splitext(path)[1].lower() in UNFRIENDLY_EXTS:
        return True
    streams = probe_streams(ffprobe, path)
    if streams is None:
        return False
    for s in streams:
        name = (s.get("codec_name") or "").lower()
        if s.get("codec_type") == "video" and name in UNFRIENDLY_VCODECS:
            return True
        if s.get("codec_type") == "audio" and name in UNFRIENDLY_ACODECS:
            return True
    return False


TRANSCODE_PROFILES = {
    "dnxhr_hq":     (["-c:v", "dnxhd", "-profile:v", "dnxhr_hq", "-pix_fmt", "yuv422p"], "_DNxHR"),
    "dnxhr_lb":     (["-c:v", "dnxhd", "-profile:v", "dnxhr_lb", "-pix_fmt", "yuv422p"], "_DNxLB"),
    "prores_422":   (["-c:v", "prores_ks", "-profile:v", "3", "-pix_fmt", "yuv422p10le"], "_ProRes422"),
    "prores_proxy": (["-c:v", "prores_ks", "-profile:v", "0", "-pix_fmt", "yuv422p10le"], "_ProResProxy"),
}


def resolve_transcode_choice(choice):
    if choice.startswith("Never"):
        return None, False
    if choice.startswith("Auto"):
        return "dnxhr_hq", True
    if "DNxHR" in choice:
        return "dnxhr_hq", False
    if "ProRes 422" in choice:
        return "prores_422", False
    return None, False


def resolve_proxy_choice(choice):
    """Returns (profile_key, scale_filter) or (None, None)."""
    if not choice or choice.startswith("No"):
        return None, None
    key = "dnxhr_lb" if "DNxHR" in choice else "prores_proxy"
    scale = "scale=trunc(iw/4)*2:trunc(ih/4)*2" if "Half res" in choice else "scale=-2:720"
    return key, scale


def run_ffmpeg(cmd, out, log, cancel, label):
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            **no_window_kwargs())
    while proc.poll() is None:
        if cancel.is_set():
            proc.terminate()
            log("  {} cancelled.".format(label))
            return None
        time.sleep(0.2)
    if proc.returncode != 0 or not os.path.exists(out):
        log("  ! {} failed.".format(label))
        return None
    return out


def transcode(path, profile_key, ffmpeg, log, cancel):
    args, suffix = TRANSCODE_PROFILES[profile_key]
    base, _ = os.path.splitext(path)
    out = base + suffix + ".mov"
    if os.path.exists(out):
        out = "{}{}_{}.mov".format(base, suffix, int(time.time()))
    log("  Transcoding -> {}".format(os.path.basename(out)))
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path] + args
    cmd += ["-c:a", "pcm_s16le", out]
    return run_ffmpeg(cmd, out, log, cancel, "Transcode")


def make_proxy(path, profile_key, scale, ffmpeg, log, cancel):
    """
    Proxy must match the original's duration and frame rate exactly or Resolve
    refuses the link — so no -r, no -t, no -ss here. Only scale and codec.
    """
    args, _ = TRANSCODE_PROFILES[profile_key]
    pdir = os.path.join(os.path.dirname(path), "Proxies")
    try:
        os.makedirs(pdir, exist_ok=True)
    except Exception as e:
        log("  ! Can't create Proxies folder: {}".format(e))
        return None
    base = os.path.splitext(os.path.basename(path))[0]
    out = os.path.join(pdir, base + ".mov")
    if os.path.exists(out):
        out = os.path.join(pdir, "{}_{}.mov".format(base, int(time.time())))
    log("  Building proxy -> Proxies/{}".format(os.path.basename(out)))
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
           "-vf", scale] + args
    cmd += ["-c:a", "pcm_s16le", out]
    return run_ffmpeg(cmd, out, log, cancel, "Proxy")


def download_one(url, cfg, tools, out_dir, log, cancel, start=None, end=None):
    cmd = build_ytdlp_command(url, cfg, tools["ytdlp"], out_dir, start, end)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1, encoding="utf-8", errors="replace",
                            **no_window_kwargs())

    finals, fallback, last_pct, tail = [], None, -20.0, []

    for raw in proc.stdout:
        if cancel.is_set():
            proc.terminate()
            log("  Cancelled.")
            return []
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        tail.append(line)
        if len(tail) > 12:
            tail.pop(0)

        m = PROGRESS_RE.search(line)
        if m:
            pct = float(m.group(1))
            if pct - last_pct >= 20 or pct >= 100:
                last_pct = pct
                log("  {:.0f}%".format(pct))
            continue

        m = DEST_RE.match(line)
        if m:
            fallback = m.group(1)
            continue

        if os.path.isabs(line) and os.path.isfile(line):
            if not line.lower().endswith((".srt", ".vtt")):
                finals.append(line)
            continue

        if line.startswith("ERROR"):
            log("  ! " + line)

    proc.wait()

    if not finals and fallback and os.path.isfile(fallback):
        finals = [fallback]

    if not finals:
        log("  ! yt-dlp exited {} with no usable output.".format(proc.returncode))
        for t in tail[-4:]:
            log("    " + t)
        return []

    for f in finals:
        log("  Got: {}".format(os.path.basename(f)))
    return finals


def process_urls(specs, cfg, log, cancel, is_studio=False):
    """
    specs: list of raw lines (may carry ' @ start-end').
    Returns (ordered_paths, meta) where meta[path] = {"url", "proxy"}.
    No Resolve API calls happen here, so this is safe on a worker thread and
    is exactly what --cli and --watch reuse.
    """
    tools = {
        "ytdlp": find_binary("yt-dlp", cfg.get("ytdlp_path", "")),
        "ffmpeg": find_binary("ffmpeg", cfg.get("ffmpeg_path", "")),
    }
    tools["ffprobe"] = sibling_tool(tools["ffmpeg"], "ffprobe")

    if not tools["ytdlp"]:
        log("! yt-dlp not found. See the README.")
        return [], {}

    out_dir = os.path.expanduser(cfg["download_dir"])
    if cfg.get("subfolder_per_day"):
        out_dir = os.path.join(out_dir, time.strftime("%Y-%m-%d"))
    try:
        os.makedirs(out_dir, exist_ok=True)
    except Exception as e:
        log("! Can't create download folder: {}".format(e))
        return [], {}

    t_profile, only_if_needed = resolve_transcode_choice(cfg["transcode"])
    if t_profile and not tools["ffmpeg"]:
        log("! ffmpeg missing - transcoding off for this run.")
        t_profile = None

    p_profile, p_scale = resolve_proxy_choice(cfg.get("proxy"))
    if p_profile and not tools["ffmpeg"]:
        log("! ffmpeg missing - proxies off for this run.")
        p_profile = None
    if p_profile and not is_studio:
        log("  (proxies will be generated; linking needs Studio)")

    parsed = [parse_url_spec(s) for s in specs]
    total = len(parsed)
    results = [None] * total

    def job(idx):
        url, start, end = parsed[idx]
        tag = "[{}/{}]".format(idx + 1, total)

        def jlog(msg):
            log("{} {}".format(tag, msg))

        if cancel.is_set():
            return
        span = ""
        if start is not None or end is not None:
            span = "  ({}{})".format(
                seconds_to_hms(start) if start is not None else "start",
                " -> " + (seconds_to_hms(end) if end is not None else "end"))
        jlog(url + span)

        paths = download_one(url, cfg, tools, out_dir, jlog, cancel, start, end)
        out = []
        for path in paths:
            if cancel.is_set():
                break
            if t_profile and not cfg["mode"].startswith("Audio"):
                go = True
                if only_if_needed:
                    go = needs_transcode(path, tools["ffprobe"])
                    if not go:
                        jlog("  Codec is already Resolve-native - no transcode needed.")
                if go:
                    new = transcode(path, t_profile, tools["ffmpeg"], jlog, cancel)
                    if new:
                        if cfg.get("delete_source_after_transcode"):
                            try:
                                os.remove(path)
                            except Exception:
                                pass
                        path = new

            proxy = None
            if p_profile and not cfg["mode"].startswith("Audio"):
                proxy = make_proxy(path, p_profile, p_scale, tools["ffmpeg"], jlog, cancel)

            out.append((path, {"url": url, "proxy": proxy}))
        results[idx] = out

    workers = max(1, min(int(cfg.get("jobs", 3) or 3), 8))
    if workers > 1 and total > 1:
        log("Running {} downloads in parallel.".format(workers))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(job, range(total)))
    else:
        for i in range(total):
            job(i)

    ordered, meta = [], {}
    for chunk in results:
        for path, info in (chunk or []):
            ordered.append(path)
            meta[path] = info
    return ordered, meta


# ----------------------------------------------------------------------------
# Resolve-side work  (MAIN THREAD ONLY)
# ----------------------------------------------------------------------------

def find_or_create_bin(media_pool, name, create=True):
    root = media_pool.GetRootFolder()
    if not name or not name.strip():
        return root
    name = name.strip()
    try:
        for folder in root.GetSubFolderList():
            if folder.GetName() == name:
                return folder
    except Exception:
        pass
    if not create:
        return None
    made = media_pool.AddSubFolder(root, name)
    return made if made else root


def existing_source_urls(bin_name):
    """URLs already sitting in the target bin, read from the Comments field."""
    urls = set()
    r = get_resolve()
    if r is None:
        return urls
    try:
        project = r.GetProjectManager().GetCurrentProject()
        if project is None:
            return urls
        mp = project.GetMediaPool()
        folder = find_or_create_bin(mp, bin_name, create=False)
        if folder is None:
            return urls
        for clip in folder.GetClipList() or []:
            try:
                c = clip.GetMetadata("Comments")
                if c:
                    urls.add(str(c).strip())
            except Exception:
                pass
    except Exception:
        pass
    return urls


def import_to_media_pool(paths, meta, cfg, log=print):
    resolve = get_resolve()
    if resolve is None:
        log("! Resolve API not reachable - files are on disk but not imported.")
        return 0

    project = resolve.GetProjectManager().GetCurrentProject()
    if project is None:
        log("! No project open. Files downloaded but not imported.")
        return 0

    is_studio, _ = studio_info()
    media_pool = project.GetMediaPool()
    media_storage = resolve.GetMediaStorage()

    prev = None
    try:
        prev = media_pool.GetCurrentFolder()
    except Exception:
        pass

    target = find_or_create_bin(media_pool, cfg["bin_name"])
    media_pool.SetCurrentFolder(target)

    items = media_storage.AddItemListToMediaPool([str(p) for p in paths])
    if not items:
        log("! Resolve refused the import. Check codec and path.")
        if prev:
            try:
                media_pool.SetCurrentFolder(prev)
            except Exception:
                pass
        return 0

    log("+ Imported {} clip(s) into bin '{}'".format(len(items), target.GetName()))

    linked = 0
    for item in items:
        try:
            fp = item.GetClipProperty("File Path")
        except Exception:
            fp = None
        info = meta.get(fp) if fp else None
        if not info:
            continue

        try:
            item.SetMetadata("Comments", info["url"])
        except Exception:
            pass

        if cfg.get("color_by_platform"):
            try:
                item.SetClipColor(platform_of(info["url"])[1])
            except Exception:
                pass

        if info.get("proxy"):
            try:
                if item.LinkProxyMedia(info["proxy"]):
                    linked += 1
            except Exception:
                pass

    if linked:
        log("+ Linked {} proxy file(s).".format(linked))
    elif any(v.get("proxy") for v in meta.values()):
        log("  Proxies were built but not linked (needs Studio 18+). "
            "They're in the Proxies subfolder.")

    mode = cfg.get("timeline_mode", TIMELINE_CHOICES[0])
    timeline = None
    if mode.startswith("Append"):
        try:
            timeline = project.GetCurrentTimeline()
            if timeline is None:
                log("  (no timeline open - skipped append)")
            else:
                media_pool.AppendToTimeline(items)
                log("  Appended to '{}'".format(timeline.GetName()))
        except Exception as e:
            log("  ! Append failed: {}".format(e))
    elif mode.startswith("New timeline"):
        try:
            name = "Yank {}".format(time.strftime("%Y-%m-%d %H%M%S"))
            timeline = media_pool.CreateTimelineFromClips(name, items)
            log("  Created timeline '{}'".format(name) if timeline
                else "  ! Timeline creation failed.")
        except Exception as e:
            log("  ! Timeline creation failed: {}".format(e))

    if cfg.get("auto_subtitles") and timeline is not None:
        if not is_studio:
            log("  Auto-subtitles need Studio - skipped.")
        else:
            try:
                ok = timeline.CreateSubtitlesFromAudio()
                log("  Transcribed audio to subtitles." if ok
                    else "  ! Transcription returned nothing.")
            except Exception as e:
                log("  ! Transcription unavailable on this build: {}".format(e))

    if prev:
        try:
            media_pool.SetCurrentFolder(prev)
        except Exception:
            pass

    return len(items)


def filter_duplicates(specs, cfg, log):
    if not cfg.get("skip_duplicates"):
        return specs
    known = existing_source_urls(cfg["bin_name"])
    if not known:
        return specs
    keep, skipped = [], 0
    for s in specs:
        url = parse_url_spec(s)[0]
        if url in known:
            skipped += 1
        else:
            keep.append(s)
    if skipped:
        log("Skipping {} link(s) already in '{}'.".format(skipped, cfg["bin_name"]))
    return keep


# ----------------------------------------------------------------------------
# UI
# ----------------------------------------------------------------------------

class YankUI(object):
    def __init__(self, fusion):
        self.fusion = fusion
        self.ui = fusion.UIManager
        self.disp = get_bmd().UIDispatcher(self.ui)

        self.cfg = load_settings()
        self.is_studio, self.tier_text = studio_info()
        self.msgq = queue.Queue()
        self.cancel = threading.Event()
        self.worker = None
        self.pending = None
        self.timer_ok = True

        self.win = self._build(True)
        if self.win is None:
            self.timer_ok = False
            self.win = self._build(False)

        self.itm = self.win.GetItems()
        self._wire()
        self._load_into_ui()

    def _build(self, with_timer):
        ui = self.ui

        def lab(text, w=104):
            return ui.Label({"Text": text, "Weight": 0, "MinimumSize": [w, 22],
                             "Alignment": {"AlignRight": True, "AlignVCenter": True}})

        rows = [
            ui.Label({"ID": "Tier", "Text": "", "Weight": 0}),
            ui.Label({"Text": "One link per line.  Add  @ 1:30-2:45  after a link to grab just that section.",
                      "Weight": 0, "WordWrap": True}),
            ui.TextEdit({"ID": "Urls", "Weight": 1.0, "MinimumSize": [600, 86],
                         "PlaceholderText": "https://www.youtube.com/watch?v=…\nhttps://www.instagram.com/reel/… @ 0:05-0:20"}),

            ui.HGroup({"Weight": 0}, [
                lab("Save to"),
                ui.LineEdit({"ID": "Dir", "Weight": 1.0}),
                ui.Button({"ID": "BrowseBtn", "Text": "…", "Weight": 0, "MinimumSize": [34, 22]}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("Bin"),
                ui.LineEdit({"ID": "BinName", "Weight": 1.0}),
                lab("Quality", 56),
                ui.ComboBox({"ID": "Quality", "Weight": 0.55}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("Grab"),
                ui.ComboBox({"ID": "Mode", "Weight": 1.0}),
                lab("Parallel", 56),
                ui.ComboBox({"ID": "Jobs", "Weight": 0.25}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("Transcode"),
                ui.ComboBox({"ID": "Transcode", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("Proxies"),
                ui.ComboBox({"ID": "Proxy", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("After import"),
                ui.ComboBox({"ID": "TimelineMode", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab("Cookies from"),
                ui.ComboBox({"ID": "Cookies", "Weight": 0.6}),
                ui.Label({"Text": "  for private / login-walled posts", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab(""),
                ui.CheckBox({"ID": "SkipDupes", "Text": "Skip links already in bin", "Weight": 0}),
                ui.CheckBox({"ID": "ColorTag", "Text": "Color by platform", "Weight": 0}),
                ui.CheckBox({"ID": "DatedFolder", "Text": "Date subfolder", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                lab(""),
                ui.CheckBox({"ID": "Playlists", "Text": "Allow playlists", "Weight": 0}),
                ui.CheckBox({"ID": "Srt", "Text": "Download .srt", "Weight": 0}),
                ui.CheckBox({"ID": "AutoSub", "Text": "Transcribe audio (Studio)", "Weight": 1.0}),
            ]),
            ui.HGroup({"Weight": 0}, [
                ui.Button({"ID": "GoBtn", "Text": "Download && Import", "Weight": 1.0,
                           "MinimumSize": [170, 30]}),
                ui.Button({"ID": "CancelBtn", "Text": "Cancel", "Weight": 0,
                           "MinimumSize": [80, 30], "Enabled": False}),
                ui.Button({"ID": "FolderBtn", "Text": "Open Folder", "Weight": 0,
                           "MinimumSize": [100, 30]}),
            ]),
            ui.Label({"ID": "Status", "Text": "Ready.", "Weight": 0}),
            ui.TextEdit({"ID": "Log", "Weight": 1.0, "ReadOnly": True,
                         "MinimumSize": [600, 150],
                         "Font": ui.Font({"Family": "Menlo", "PixelSize": 11})}),
        ]

        if with_timer:
            rows.append(ui.Timer({"ID": "Poll", "Interval": 120}))

        try:
            return self.disp.AddWindow({
                "ID": "YankWin",
                "WindowTitle": "{}  v{}".format(APP_NAME, VERSION),
                "Geometry": [180, 140, 660, 760],
            }, ui.VGroup({"Spacing": 6, "Margin": 10}, rows))
        except Exception:
            return None

    def _wire(self):
        w = self.win
        w.On.YankWin.Close = self.on_close
        w.On.GoBtn.Clicked = self.on_go
        w.On.CancelBtn.Clicked = self.on_cancel
        w.On.BrowseBtn.Clicked = self.on_browse
        w.On.FolderBtn.Clicked = self.on_open_folder
        if self.timer_ok:
            try:
                w.On.Poll.Timeout = self.on_poll
                self.itm["Poll"].Start()
            except Exception:
                self.timer_ok = False

    def _load_into_ui(self):
        itm = self.itm
        itm["Dir"].Text = self.cfg["download_dir"]
        itm["BinName"].Text = self.cfg["bin_name"]

        for combo, choices, key in (
            ("Quality", QUALITY_CHOICES, "quality"),
            ("Mode", MODE_CHOICES, "mode"),
            ("Transcode", TRANSCODE_CHOICES, "transcode"),
            ("Proxy", PROXY_CHOICES, "proxy"),
            ("TimelineMode", TIMELINE_CHOICES, "timeline_mode"),
            ("Cookies", COOKIE_CHOICES, "cookies"),
            ("Jobs", JOB_CHOICES, "jobs"),
        ):
            for c in choices:
                itm[combo].AddItem(c)
            try:
                itm[combo].CurrentIndex = choices.index(str(self.cfg[key]))
            except ValueError:
                itm[combo].CurrentIndex = 0

        itm["SkipDupes"].Checked = bool(self.cfg["skip_duplicates"])
        itm["ColorTag"].Checked = bool(self.cfg["color_by_platform"])
        itm["DatedFolder"].Checked = bool(self.cfg["subfolder_per_day"])
        itm["Playlists"].Checked = bool(self.cfg["allow_playlists"])
        itm["Srt"].Checked = bool(self.cfg["write_srt"])
        itm["AutoSub"].Checked = bool(self.cfg["auto_subtitles"]) and self.is_studio
        itm["AutoSub"].Enabled = self.is_studio

        badge = "Studio — proxy linking and transcription enabled" if self.is_studio \
            else "Free — proxies build but won't link; transcription off"
        itm["Tier"].Text = "{}   ·   {}".format(self.tier_text, badge)

        yt = find_binary("yt-dlp", self.cfg.get("ytdlp_path", ""))
        ff = find_binary("ffmpeg", self.cfg.get("ffmpeg_path", ""))
        self.log("{} v{}".format(APP_NAME, VERSION))
        self.log("yt-dlp: {}".format(yt or "NOT FOUND — see README"))
        self.log("ffmpeg: {}".format(ff or "NOT FOUND — transcode/proxies unavailable"))
        self.log("-" * 60)

    def _harvest_ui(self):
        itm = self.itm
        self.cfg["download_dir"] = itm["Dir"].Text.strip() or default_download_dir()
        self.cfg["bin_name"] = itm["BinName"].Text.strip()
        self.cfg["quality"] = QUALITY_CHOICES[itm["Quality"].CurrentIndex]
        self.cfg["mode"] = MODE_CHOICES[itm["Mode"].CurrentIndex]
        self.cfg["transcode"] = TRANSCODE_CHOICES[itm["Transcode"].CurrentIndex]
        self.cfg["proxy"] = PROXY_CHOICES[itm["Proxy"].CurrentIndex]
        self.cfg["timeline_mode"] = TIMELINE_CHOICES[itm["TimelineMode"].CurrentIndex]
        self.cfg["cookies"] = COOKIE_CHOICES[itm["Cookies"].CurrentIndex]
        self.cfg["jobs"] = JOB_CHOICES[itm["Jobs"].CurrentIndex]
        self.cfg["skip_duplicates"] = bool(itm["SkipDupes"].Checked)
        self.cfg["color_by_platform"] = bool(itm["ColorTag"].Checked)
        self.cfg["subfolder_per_day"] = bool(itm["DatedFolder"].Checked)
        self.cfg["allow_playlists"] = bool(itm["Playlists"].Checked)
        self.cfg["write_srt"] = bool(itm["Srt"].Checked)
        self.cfg["auto_subtitles"] = bool(itm["AutoSub"].Checked)
        save_settings(self.cfg)

    def log(self, text):
        if threading.current_thread() is threading.main_thread():
            self._append(text)
        else:
            self.msgq.put(text)

    def _append(self, text):
        try:
            self.itm["Log"].Append(str(text))
        except Exception:
            pass

    def _drain(self, limit=None):
        n = 0
        while limit is None or n < limit:
            try:
                self._append(self.msgq.get_nowait())
                n += 1
            except queue.Empty:
                break

    def status(self, text):
        try:
            self.itm["Status"].Text = str(text)
        except Exception:
            pass

    def on_browse(self, ev):
        try:
            picked = self.fusion.RequestDir(self.itm["Dir"].Text or os.path.expanduser("~"))
            if picked:
                self.itm["Dir"].Text = str(picked).rstrip("/\\")
        except Exception:
            self.log("! Folder picker unavailable — type the path instead.")

    def on_open_folder(self, ev):
        d = os.path.expanduser(self.itm["Dir"].Text.strip())
        if not os.path.isdir(d):
            self.log("! Not a folder: {}".format(d))
            return
        try:
            if IS_MAC:
                subprocess.Popen(["open", d])
            elif IS_WIN:
                os.startfile(d)  # noqa
            else:
                subprocess.Popen(["xdg-open", d])
        except Exception as e:
            self.log("! {}".format(e))

    def on_cancel(self, ev):
        self.cancel.set()
        self.status("Cancelling…")

    def on_go(self, ev):
        if self.worker and self.worker.is_alive():
            return

        raw = self.itm["Urls"].PlainText or ""
        specs = [u.strip() for u in raw.splitlines()
                 if u.strip() and not u.strip().startswith("#")]
        if not specs:
            self.log("! Paste a link first.")
            return

        self._harvest_ui()
        if not find_binary("yt-dlp", self.cfg.get("ytdlp_path", "")):
            self.log("! yt-dlp not found. See the README.")
            return

        # Dedupe check touches the Resolve API, so it happens here on the
        # main thread before the worker starts.
        specs = filter_duplicates(specs, self.cfg, self._append)
        if not specs:
            self.status("Everything was already in the bin.")
            return

        self.cancel.clear()
        self.itm["GoBtn"].Enabled = False
        self.itm["CancelBtn"].Enabled = True
        self.status("Working on {} link(s)…".format(len(specs)))
        self._append("")

        cfg = dict(self.cfg)
        studio = self.is_studio

        def run():
            try:
                self.pending = process_urls(specs, cfg, self.log, self.cancel, studio)
            except Exception as e:
                self.log("! Worker crashed: {}".format(e))
                self.pending = ([], {})

        self.worker = threading.Thread(target=run, daemon=True)
        self.worker.start()

        if not self.timer_ok:
            self.worker.join()
            self._drain()
            self._finish()

    def on_poll(self, ev):
        self._drain(limit=50)
        if self.worker and not self.worker.is_alive() and self.pending is not None:
            self._drain()
            self._finish()

    def _finish(self):
        paths, meta = self.pending or ([], {})
        self.pending = None
        self.worker = None

        if paths:
            import_to_media_pool(paths, meta, self.cfg, self._append)
            self.status("Done — {} clip(s).".format(len(paths)))
        elif self.cancel.is_set():
            self.status("Cancelled.")
        else:
            self.status("Nothing downloaded.")

        self._append("-" * 60)
        self.itm["GoBtn"].Enabled = True
        self.itm["CancelBtn"].Enabled = False

    def on_close(self, ev):
        self.cancel.set()
        self._harvest_ui()
        self.disp.ExitLoop()

    def run(self):
        self.win.Show()
        self.disp.RunLoop()
        self.win.Hide()


# ----------------------------------------------------------------------------
# Watch mode
# ----------------------------------------------------------------------------

def run_watch(cfg, path, interval=3.0):
    """
    Append a URL to a text file from anywhere — a Shortcut, Alfred, Stream Deck,
    a shell alias — and it lands in the open project. Needs Studio, since it
    drives Resolve from outside the app.
    """
    path = os.path.expanduser(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if not os.path.exists(path):
        open(path, "a").close()

    is_studio, tier = studio_info()
    print("{} v{} — watching {}".format(APP_NAME, VERSION, path))
    print(tier)
    if not is_studio:
        print("! External scripting needs Studio. Downloads will work; imports won't.")
    print("Append a URL to that file and it'll be pulled in. Ctrl-C to stop.\n")

    cancel = threading.Event()
    offset = os.path.getsize(path)

    try:
        while True:
            time.sleep(interval)
            try:
                size = os.path.getsize(path)
            except OSError:
                continue
            if size < offset:          # file was truncated/replaced
                offset = 0
            if size == offset:
                continue
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                fh.seek(offset)
                chunk = fh.read()
                offset = fh.tell()

            specs = [l.strip() for l in chunk.splitlines()
                     if l.strip() and not l.strip().startswith("#")]
            if not specs:
                continue

            print("\n--- {} new link(s) at {} ---".format(len(specs), time.strftime("%H:%M:%S")))
            specs = filter_duplicates(specs, cfg, print)
            if not specs:
                continue
            paths, meta = process_urls(specs, cfg, print, cancel, is_studio)
            if paths:
                import_to_media_pool(paths, meta, cfg, print)
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def run_cli(argv):
    p = argparse.ArgumentParser(
        prog="Yank_for_Resolve.py",
        description="Download media by URL and import it into DaVinci Resolve.")
    p.add_argument("--cli", action="store_true", help="run headless")
    p.add_argument("urls", nargs="*", help="URLs, optionally with ' @ 1:30-2:45'")
    p.add_argument("--dir", help="download folder")
    p.add_argument("--bin", dest="binname", help="Media Pool bin name")
    p.add_argument("--quality", choices=QUALITY_CHOICES)
    p.add_argument("--audio", action="store_true", help="audio only (WAV)")
    p.add_argument("--mp3", action="store_true", help="audio only (MP3)")
    p.add_argument("--transcode", choices=["auto", "none", "dnxhr", "prores"])
    p.add_argument("--proxy", choices=["none", "half-dnxhr", "half-prores",
                                       "720-dnxhr", "720-prores"])
    p.add_argument("--timeline", choices=["bin", "append", "new"])
    p.add_argument("--subtitles", action="store_true", help="transcribe audio (Studio)")
    p.add_argument("--srt", action="store_true", help="also download subtitle files")
    p.add_argument("--cookies", choices=COOKIE_CHOICES[1:])
    p.add_argument("--jobs", type=int, help="parallel downloads (default 3)")
    p.add_argument("--playlist", action="store_true", help="allow playlists/channels")
    p.add_argument("--no-import", action="store_true", help="download only")
    p.add_argument("--watch", nargs="?", const=WATCH_DEFAULT, metavar="FILE",
                   help="watch a text file for URLs and pull them as they appear")
    p.add_argument("--info", action="store_true", help="print environment and exit")
    args = p.parse_args(argv)

    cfg = load_settings()
    if args.dir:
        cfg["download_dir"] = args.dir
    if args.binname:
        cfg["bin_name"] = args.binname
    if args.quality:
        cfg["quality"] = args.quality
    if args.audio:
        cfg["mode"] = "Audio only (WAV)"
    if args.mp3:
        cfg["mode"] = "Audio only (MP3)"
    if args.cookies:
        cfg["cookies"] = args.cookies
    if args.jobs:
        cfg["jobs"] = str(args.jobs)
    if args.playlist:
        cfg["allow_playlists"] = True
    if args.srt:
        cfg["write_srt"] = True
    if args.subtitles:
        cfg["auto_subtitles"] = True
    if args.transcode:
        cfg["transcode"] = {"auto": TRANSCODE_CHOICES[0], "none": TRANSCODE_CHOICES[1],
                            "dnxhr": TRANSCODE_CHOICES[2],
                            "prores": TRANSCODE_CHOICES[3]}[args.transcode]
    if args.proxy:
        cfg["proxy"] = {"none": PROXY_CHOICES[0], "half-dnxhr": PROXY_CHOICES[1],
                        "half-prores": PROXY_CHOICES[2], "720-dnxhr": PROXY_CHOICES[3],
                        "720-prores": PROXY_CHOICES[4]}[args.proxy]
    if args.timeline:
        cfg["timeline_mode"] = {"bin": TIMELINE_CHOICES[0], "append": TIMELINE_CHOICES[1],
                                "new": TIMELINE_CHOICES[2]}[args.timeline]

    if args.info:
        is_studio, tier = studio_info()
        print("{} v{}".format(APP_NAME, VERSION))
        print("  Resolve : {} ({})".format(tier, "Studio" if is_studio else "Free"))
        print("  yt-dlp  : {}".format(find_binary("yt-dlp", cfg["ytdlp_path"]) or "not found"))
        print("  ffmpeg  : {}".format(find_binary("ffmpeg", cfg["ffmpeg_path"]) or "not found"))
        print("  settings: {}".format(SETTINGS_PATH))
        return 0

    if args.watch:
        return run_watch(cfg, args.watch)

    if not args.urls:
        p.error("give me at least one URL (or use --watch / --info)")

    is_studio, _ = studio_info()
    cancel = threading.Event()
    specs = filter_duplicates(list(args.urls), cfg, print) if not args.no_import else list(args.urls)
    if not specs:
        print("Everything was already in the bin.")
        return 0

    paths, meta = process_urls(specs, cfg, lambda m: print(m, flush=True), cancel, is_studio)
    if not paths:
        print("Nothing downloaded.")
        return 1

    if args.no_import:
        for pth in paths:
            print(pth)
        return 0

    if import_to_media_pool(paths, meta, cfg, print) == 0:
        print("Files are on disk here:")
        for pth in paths:
            print("  " + pth)
    return 0


# ----------------------------------------------------------------------------

def main():
    flags = ("--cli", "--watch", "--info", "-h", "--help")
    if any(a in sys.argv for a in flags):
        argv = [a for a in sys.argv[1:] if a != "--cli"]
        sys.exit(run_cli(["--cli"] + argv))

    fusion = None
    g = globals()
    for name in ("fusion", "fu"):
        if name in g and g[name] is not None:
            fusion = g[name]
            break
    if fusion is None:
        b = get_bmd()
        if b is not None:
            try:
                fusion = b.scriptapp("Fusion")
            except Exception:
                fusion = None

    if fusion is None:
        print("Couldn't reach Fusion's UI Manager.")
        print("Inside Resolve:  Workspace > Scripts > Yank_for_Resolve")
        print("Headless:        python3 Yank_for_Resolve.py --cli \"<url>\"")
        print("Environment:     python3 Yank_for_Resolve.py --info")
        sys.exit(1)

    YankUI(fusion).run()


if __name__ == "__main__":
    main()
