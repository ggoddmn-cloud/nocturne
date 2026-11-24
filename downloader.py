#!/usr/bin/env python3
import json
import base64
from contextlib import contextmanager
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from ipaddress import ip_address
from pathlib import Path

ROOT = Path(__file__).resolve().parent
INDEX = ROOT / "index.html"
TOOLS = ROOT / "tools"
HOST = "127.0.0.1"
PORT = int(os.environ.get("PORT", "8765"))
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36"
MAX_MANIFEST = 2 * 1024 * 1024
VIDEO_EXTS = {".mp4", ".webm", ".mov", ".m4v", ".mkv", ".avi"}
HLS_MIME = {"application/vnd.apple.mpegurl", "application/x-mpegurl", "audio/mpegurl"}

JOBS = {}
JOBS_LOCK = threading.Lock()
JOB_TTL = 60 * 60
MAX_BYTES = int(os.getenv("MAX_FILE_MB", "49")) * 1000000


def jdump(obj):
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


def is_safe_public_url(raw_url):
    try:
        p = urllib.parse.urlparse(raw_url)
        if p.scheme not in {"http", "https"} or not p.hostname:
            return False, "Нужна обычная ссылка http/https."
        host = p.hostname.lower().rstrip(".")
        if host in {"localhost", "localhost.localdomain"}:
            return False, "Локальные адреса заблокированы."
        try:
            infos = socket.getaddrinfo(host, p.port or (443 if p.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except socket.gaierror:
            return False, "Не удалось определить адрес сайта."
        for info in infos:
            ip = ip_address(info[4][0])
            if not ip.is_global or ip.is_multicast:
                return False, "Локальные/служебные сетевые адреса заблокированы."
        return True, None
    except Exception:
        return False, "Некорректная ссылка."


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        safe, reason = is_safe_public_url(newurl)
        if not safe:
            raise urllib.error.URLError(reason or "Unsafe redirect blocked")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def request(url, method="GET", timeout=15, headers=None):
    safe, reason = is_safe_public_url(url)
    if not safe:
        raise ValueError(reason or "Некорректная ссылка.")
    h = {"User-Agent": UA, "Accept": "*/*"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, method=method, headers=h)
    return SAFE_OPENER.open(req, timeout=timeout)


def detect_source(url):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    path = p.path or ""
    if host in {"youtu.be", "www.youtu.be"}:
        return "youtube"
    if (host == "youtube.com" or host.endswith(".youtube.com")) or (host == "youtube-nocookie.com" or host.endswith(".youtube-nocookie.com")):
        if path.startswith("/shorts/"):
            return "youtube_shorts"
        return "youtube"
    if (host == "tiktok.com" or host.endswith(".tiktok.com")):
        return "tiktok"
    ext = Path(path.lower()).suffix
    if ext == ".m3u8":
        return "hls"
    if ext in VIDEO_EXTS:
        return "direct"
    return "unknown"


def youtube_id(url):
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host.endswith("youtu.be"):
        return p.path.strip("/").split("/")[0] or None
    parts = [x for x in p.path.split("/") if x]
    if parts and parts[0] in {"shorts", "embed", "live"} and len(parts) > 1:
        return parts[1]
    return urllib.parse.parse_qs(p.query).get("v", [None])[0]


def tiktok_id(url):
    m = re.search(r"/video/(\d+)", urllib.parse.urlparse(url).path)
    return m.group(1) if m else None


def filename_from_response(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1)).strip('"')
    m = re.search(r'filename="?([^";]+)', cd, re.I)
    if m:
        return m.group(1)
    name = Path(urllib.parse.urlparse(url).path).name
    return name or "video.mp4"


def head_info(url):
    try:
        with request(url, method="HEAD", timeout=12) as r:
            return {
                "final_url": r.geturl(),
                "content_type": (r.headers.get_content_type() or "").lower(),
                "content_length": r.headers.get("Content-Length"),
                "filename": filename_from_response(r, r.geturl()),
            }
    except Exception:
        with request(url, timeout=12, headers={"Range": "bytes=0-0"}) as r:
            return {
                "final_url": r.geturl(),
                "content_type": (r.headers.get_content_type() or "").lower(),
                "content_length": r.headers.get("Content-Range") or r.headers.get("Content-Length"),
                "filename": filename_from_response(r, r.geturl()),
            }


def read_manifest(url):
    with request(url, timeout=15, headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,text/plain"}) as r:
        data = r.read(MAX_MANIFEST + 1)
        final = r.geturl()
    if len(data) > MAX_MANIFEST:
        raise ValueError("HLS-манифест слишком большой.")
    text = data.decode("utf-8", "replace")
    if "#EXTM3U" not in text:
        raise ValueError("Ссылка не похожа на HLS-манифест.")
    return text, final


def parse_hls_variants(url):
    text, final = read_manifest(url)
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    out = []
    for i, line in enumerate(lines):
        if not line.startswith("#EXT-X-STREAM-INF:") or i + 1 >= len(lines) or lines[i + 1].startswith("#"):
            continue
        attrs = line.split(":", 1)[1]
        target = urllib.parse.urljoin(final, lines[i + 1])
        res = re.search(r"RESOLUTION=(\d+)x(\d+)", attrs)
        bw = re.search(r"(?:AVERAGE-)?BANDWIDTH=(\d+)", attrs)
        height = int(res.group(2)) if res else 0
        label = f"{height}p" if height else "Вариант"
        if bw:
            label += f" · {int(bw.group(1)) / 1_000_000:.1f} Mbps"
        out.append({"height": height, "bandwidth": int(bw.group(1)) if bw else 0, "url": target, "label": label})
    out.sort(key=lambda x: (x["height"], x["bandwidth"]), reverse=True)
    for i, item in enumerate(out):
        item["id"] = str(i)
    return out


def find_ffmpeg():
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg
        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and Path(path).exists():
            return path
    except Exception:
        pass
    return None


def find_deno():
    candidates = [TOOLS / ("deno.exe" if os.name == "nt" else "deno")]
    path = shutil.which("deno")
    if path:
        candidates.append(Path(path))
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def yt_dlp_module():
    try:
        import yt_dlp
        return yt_dlp
    except Exception as exc:
        raise ValueError("yt-dlp не установлен. Запусти приложение через start_windows.bat / start_mac_linux.sh.") from exc


def ydl_base_opts():
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 3,
        "http_headers": {"User-Agent": UA},
    }
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    deno = find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


@contextmanager
def platform_session(url, opts):
    """Use only an explicitly configured, platform-specific session."""
    source = detect_source(url)
    prefix = 'YOUTUBE' if source in ('youtube', 'youtube_shorts') else 'TIKTOK' if source == 'tiktok' else None
    with tempfile.TemporaryDirectory(prefix='nocturne_auth_') as folder:
        value = os.getenv(prefix + '_COOKIES_B64', '') if prefix else ''
        if value:
            try:
                raw = base64.b64decode(value.strip(), validate=True).decode('utf-8-sig')
                if not raw.startswith(('# Netscape HTTP Cookie File', '# HTTP Cookie File')):
                    raise ValueError()
                # Do not import unrelated browser sessions.
                domains = ('youtube.com', 'google.com') if prefix == 'YOUTUBE' else ('tiktok.com',)
                lines = ['# Netscape HTTP Cookie File']
                for line in raw.splitlines():
                    entry = line.removeprefix('#HttpOnly_')
                    if not entry or entry.startswith('#'): continue
                    fields = entry.split('\t')
                    if len(fields) != 7: raise ValueError()
                    host = fields[0].lstrip('.').lower()
                    if any(host == d or host.endswith('.'+d) for d in domains): lines.append(line)
                if len(lines) == 1: raise ValueError()
                cookiefile = Path(folder) / 'cookies.txt'
                cookiefile.write_text('\n'.join(lines)+'\n', encoding='utf-8')
                cookiefile.chmod(0o600)
                opts = dict(opts, cookiefile=str(cookiefile))
            except Exception:
                raise ValueError('Некорректный файл авторизации для '+prefix) from None
        with yt_dlp_module().YoutubeDL(opts) as ydl:
            yield ydl


def public_error(exc, url):
    text = str(exc).lower()
    source = detect_source(url)
    if 'файл авторизации' in text:
        return 'Ошибка настройки авторизации. Сообщи администратору.'
    if any(x in text for x in ('sign in to confirm', 'not a bot', 'ip address is blocked', '403', 'forbidden')):
        name = 'YouTube' if source.startswith('youtube') else 'TikTok' if source == 'tiktok' else 'Источник'
        return name + ' отклонил запрос сервера (403 или требуется авторизация). Попробуй позже или отправь исходный видеофайл, на который у тебя есть права.'
    if 'лимит' in text or 'max-filesize' in text:
        return 'Файл превышает лимит. Выбери качество ниже.'
    return 'Не удалось получить видео. Проверь доступность ссылки или отправь исходный файл.'


def normalize_info(info):
    if not isinstance(info, dict):
        raise ValueError("Источник не вернул данные о видео.")
    if info.get("_type") in {"playlist", "multi_video"}:
        entries = [e for e in (info.get("entries") or []) if e]
        if not entries:
            raise ValueError("Не удалось найти видео по этой ссылке.")
        info = entries[0]
    return info


def extract_platform_info(url):
    yt_dlp = yt_dlp_module()
    opts = ydl_base_opts()
    with platform_session(url, opts) as ydl:
        return normalize_info(ydl.extract_info(url, download=False))


def effective_height(f):
    try:
        w = int(f.get("width") or 0)
        h = int(f.get("height") or 0)
    except Exception:
        return 0
    if w > 0 and h > 0:
        return min(w, h)
    return h or w


def is_watermarked_format(f):
    note = str(f.get("format_note") or "").lower()
    fid = str(f.get("format_id") or "").lower()
    return "watermark" in note or fid == "download"


def format_heights(info, source=None):
    buckets = {}
    for f in info.get("formats") or []:
        if not isinstance(f, dict):
            continue
        if f.get("vcodec") in {None, "none"}:
            continue
        if source == "tiktok" and is_watermarked_format(f):
            continue
        h = effective_height(f)
        if h <= 0:
            continue
        b = buckets.setdefault(h, {"fps": 0, "hdr": False, "raw_height": 0})
        try:
            b["raw_height"] = max(b["raw_height"], int(f.get("height") or h))
        except Exception:
            b["raw_height"] = max(b["raw_height"], h)
        try:
            b["fps"] = max(b["fps"], float(f.get("fps") or 0))
        except Exception:
            pass
        dr = str(f.get("dynamic_range") or "").upper()
        if dr and dr not in {"SDR", "NONE"}:
            b["hdr"] = True
    return buckets


def build_quality_options(info, source=None):
    formats = []
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or f.get("vcodec") in {None, "none"}:
            continue
        if source == "tiktok" and is_watermarked_format(f):
            continue
        formats.append(f)
    buckets = format_heights({"formats": formats}, source=None)
    heights = sorted(buckets, reverse=True)
    qs = []
    if heights:
        qs.append({"id": "best", "label": f"Лучшее доступное · до {heights[0]}p"})
        for h in heights[:10]:
            bits = [f"{h}p"]
            fps = buckets[h]["fps"]
            if fps >= 50:
                bits.append(f"{int(round(fps))} fps")
            if buckets[h]["hdr"]:
                bits.append("HDR")
            raw_h = buckets[h].get("raw_height") or h
            qs.append({"id": f"h:{h}:{raw_h}", "label": " · ".join(bits)})
    elif formats:
        qs.append({"id": "best", "label": "Лучшее доступное"})
    return qs


def platform_preview(source, info, original_url):
    page_url = info.get("webpage_url") or original_url
    if source in {"youtube", "youtube_shorts"}:
        vid = info.get("id") or youtube_id(page_url)
        if vid:
            return {"type": "iframe", "url": f"https://www.youtube-nocookie.com/embed/{urllib.parse.quote(str(vid))}?rel=0"}
    if source == "tiktok":
        tid = info.get("id") or tiktok_id(page_url)
        if tid:
            return {"type": "iframe", "url": f"https://www.tiktok.com/player/v1/{urllib.parse.quote(str(tid))}?controls=1&description=1"}
    return {"type": "link", "url": page_url}


def analyze(url):
    safe, msg = is_safe_public_url(url)
    if not safe:
        raise ValueError(msg)
    source = detect_source(url)

    if source in {"youtube", "youtube_shorts", "tiktok"}:
        info = extract_platform_info(url)
        qualities = build_quality_options(info, source=source)
        label = "YouTube Shorts" if source == "youtube_shorts" else ("YouTube" if source == "youtube" else "TikTok")
        ffmpeg_ok = bool(find_ffmpeg())
        deno_ok = bool(find_deno())
        notes = []
        if source in {"youtube", "youtube_shorts"} and not deno_ok:
            notes.append("Deno не найден — часть YouTube-форматов может быть недоступна")
        if not ffmpeg_ok:
            notes.append("ffmpeg не найден — высокие качества с отдельным аудио могут не собраться")
        if not notes:
            notes.append(f"Найдено вариантов качества: {len(qualities)}")
        if source == "tiktok":
            if qualities:
                notes.append("для скачивания используются только форматы, которые extractor не помечает как watermarked")
            else:
                notes.append("источник не отдал формат без водяного знака, поэтому скачивание отключено")
        return {
            "source": source,
            "source_label": label,
            "title": info.get("title") or f"{label} video",
            "author": info.get("uploader") or info.get("channel") or info.get("creator"),
            "preview": platform_preview(source, info, url),
            "qualities": qualities,
            "can_download": bool(qualities),
            "message": ". ".join(notes) + ".",
        }

    info = head_info(url)
    final_url = info["final_url"]
    ct = info["content_type"]
    ext = Path(urllib.parse.urlparse(final_url).path.lower()).suffix
    hls = source == "hls" or ext == ".m3u8" or ct in HLS_MIME
    direct = source == "direct" or ext in VIDEO_EXTS or ct.startswith("video/")

    if hls:
        variants = parse_hls_variants(final_url)
        if not variants:
            variants = [{"id": "auto", "label": "Оригинал / Auto", "height": 0, "bandwidth": 0}]
        return {
            "source": "hls",
            "source_label": "Direct HLS",
            "title": info.get("filename") or "HLS stream",
            "author": None,
            "preview": {"type": "video", "url": final_url},
            "qualities": variants,
            "can_download": bool(find_ffmpeg()),
            "message": "Качество берётся из HLS master playlist." if find_ffmpeg() else "Для сохранения HLS нужен ffmpeg.",
        }
    if direct:
        return {
            "source": "direct",
            "source_label": "Direct video",
            "title": info.get("filename") or "video",
            "author": None,
            "preview": {"type": "video", "url": final_url},
            "qualities": [{"id": "original", "label": "Оригинал"}],
            "can_download": True,
            "message": "Прямая ссылка на видеофайл — скачивание без перекодирования.",
        }

    return {
        "source": "unknown",
        "source_label": "Web page",
        "title": urllib.parse.urlparse(url).hostname or "Ссылка",
        "author": None,
        "preview": {"type": "link", "url": url},
        "qualities": [],
        "can_download": False,
        "message": "Для этой страницы extractor не подключён. Сейчас приложение полноценно обрабатывает YouTube, Shorts, TikTok, прямые видео и HLS.",
    }


def sanitize_filename(name, default="video.mp4"):
    name = os.path.basename(name or default)
    name = re.sub(r"[\x00-\x1f\\/:*?\"<>|]+", "_", name).strip(" .")
    return name[:180] or default


def choose_hls_url(master_url, quality_id):
    variants = parse_hls_variants(master_url)
    if not variants or quality_id == "auto":
        return master_url, "video.mp4"
    try:
        item = variants[int(quality_id)]
    except Exception as exc:
        raise ValueError("Такое качество не найдено.") from exc
    suffix = f"-{item['height']}p" if item.get("height") else ""
    return item["url"], f"video{suffix}.mp4"


def quality_selector(quality):
    if quality == "best" or not quality:
        return "bv*+ba/b"
    if quality.startswith("h:"):
        try:
            parts = quality.split(":")
            display_h = int(parts[1])
            raw_h = int(parts[2]) if len(parts) > 2 else display_h
        except Exception as exc:
            raise ValueError("Некорректное качество.") from exc
        # raw_h keeps portrait Shorts intuitive: label can be 1080p while yt-dlp sees 1080x1920.
        return (
            f"bv*[height<={raw_h}][ext=mp4]+ba[ext=m4a]/"
            f"b[height<={raw_h}][ext=mp4]/"
            f"bv*[height<={raw_h}]+ba/b[height<={raw_h}]"
        )
    raise ValueError("Такое качество не найдено.")


def update_job(job_id, **fields):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)
            job["updated"] = time.time()


def progress_hook_for(job_id):
    def hook(d):
        status = d.get("status")
        if status == "downloading":
            done = d.get("downloaded_bytes") or 0
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            progress = int(done * 92 / total) if total else 12
            progress = max(2, min(92, progress))
            speed = d.get("speed") or 0
            eta = d.get("eta")
            msg = "Скачиваю источник"
            if speed:
                msg += f" · {speed / 1024 / 1024:.1f} MB/s"
            if eta is not None:
                msg += f" · ~{int(eta)} сек"
            update_job(job_id, status="downloading", progress=progress, message=msg)
        elif status == "finished":
            update_job(job_id, status="processing", progress=94, message="Собираю итоговый файл…")
    return hook


def pick_downloaded_file(folder):
    files = []
    for p in Path(folder).iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in VIDEO_EXTS:
            continue
        files.append(p)
    if not files:
        raise ValueError("Загрузчик не создал итоговый видеофайл.")
    return max(files, key=lambda p: p.stat().st_size)


def choose_tiktok_format(info, quality):
    candidates = []
    max_h = None
    if quality.startswith("h:"):
        try:
            max_h = int(quality.split(":")[1])
        except Exception as exc:
            raise ValueError("Некорректное качество.") from exc
    elif quality != "best":
        raise ValueError("Такое качество не найдено.")
    for f in info.get("formats") or []:
        if not isinstance(f, dict) or f.get("vcodec") in {None, "none"}:
            continue
        if is_watermarked_format(f):
            continue
        h = effective_height(f)
        if max_h and h and h > max_h:
            continue
        candidates.append(f)
    if not candidates:
        raise ValueError("TikTok не отдал доступный формат без водяного знака для выбранного качества.")
    def score(f):
        return (
            effective_height(f),
            float(f.get("fps") or 0),
            float(f.get("tbr") or 0),
            int(f.get("filesize") or f.get("filesize_approx") or 0),
        )
    best = max(candidates, key=score)
    fid = str(best.get("format_id") or "")
    if not fid:
        raise ValueError("У выбранного TikTok-формата нет ID.")
    if best.get("acodec") in {None, "none"}:
        return fid + "+ba"
    return fid


def download_platform_to(job_id, url, quality, folder):
    yt_dlp = yt_dlp_module()
    source = detect_source(url)
    selector = quality_selector(quality)
    if source == "tiktok":
        probe_opts = ydl_base_opts()
        with platform_session(url, probe_opts) as probe:
            probe_info = normalize_info(probe.extract_info(url, download=False))
        selector = choose_tiktok_format(probe_info, quality)
    opts = ydl_base_opts()
    opts.update({
        "format": selector,
        "outtmpl": str(Path(folder) / "video.%(ext)s"),
        "progress_hooks": [progress_hook_for(job_id)],
        "overwrites": True,
        "continuedl": True,
        "max_filesize": MAX_BYTES,
    })
    if find_ffmpeg():
        opts["merge_output_format"] = "mp4"
    update_job(job_id, status="downloading", progress=2, message="Получаю выбранный формат…")
    with platform_session(url, opts) as ydl:
        info = normalize_info(ydl.extract_info(url, download=True))
    path = pick_downloaded_file(folder)
    title = sanitize_filename(info.get("title") or "video", "video")
    ext = path.suffix or ".mp4"
    filename = sanitize_filename(title + ext)
    return path, filename


def download_direct_to(job_id, url, folder):
    info = head_info(url)
    final_url = info["final_url"]
    with request(final_url, timeout=60) as r:
        ct = (r.headers.get("Content-Type") or "application/octet-stream").lower()
        ext = Path(urllib.parse.urlparse(r.geturl()).path.lower()).suffix
        if not (ct.startswith("video/") or ext in VIDEO_EXTS):
            raise ValueError("Источник больше не отдаёт видеофайл.")
        total = int(r.headers.get("Content-Length") or 0)
        name = sanitize_filename(filename_from_response(r, r.geturl()))
        path = Path(folder) / name
        done = 0
        with open(path, "wb") as f:
            while True:
                chunk = r.read(512 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if done > MAX_BYTES:
                    raise ValueError("Файл превышает лимит загрузки.")
                progress = int(done * 92 / total) if total else 20
                update_job(job_id, status="downloading", progress=max(2, min(92, progress)), message="Скачиваю видео…")
    return path, name


def download_hls_to(job_id, master_url, quality, folder):
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise ValueError("ffmpeg не найден.")
    stream_url, name = choose_hls_url(master_url, quality)
    safe, msg = is_safe_public_url(stream_url)
    if not safe:
        raise ValueError(msg)
    path = Path(folder) / sanitize_filename(name)
    update_job(job_id, status="processing", progress=18, message="Сохраняю HLS-поток через ffmpeg…")
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-protocol_whitelist", "http,https,tcp,tls,crypto", "-i", stream_url,
           "-map", "0:v?", "-map", "0:a?", "-c", "copy", "-fs", str(MAX_BYTES + 1000000), str(path)]
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=60 * 60)
    if proc.returncode != 0:
        err = proc.stderr.decode("utf-8", "replace")[-900:]
        raise ValueError("ffmpeg не смог сохранить поток. " + err)
    return path, path.name


def run_download_job(job_id, url, quality):
    folder = tempfile.mkdtemp(prefix="nocturne_")
    update_job(job_id, folder=folder)
    try:
        safe, msg = is_safe_public_url(url)
        if not safe:
            raise ValueError(msg)
        source = detect_source(url)
        if source in {"youtube", "youtube_shorts", "tiktok"}:
            path, filename = download_platform_to(job_id, url, quality, folder)
        else:
            info = head_info(url)
            final_url = info["final_url"]
            ct = info["content_type"]
            ext = Path(urllib.parse.urlparse(final_url).path.lower()).suffix
            hls = source == "hls" or ext == ".m3u8" or ct in HLS_MIME
            direct = source == "direct" or ext in VIDEO_EXTS or ct.startswith("video/")
            if hls:
                path, filename = download_hls_to(job_id, final_url, quality, folder)
            elif direct:
                path, filename = download_direct_to(job_id, final_url, folder)
            else:
                raise ValueError("Не удалось найти поддерживаемый видеоисточник.")
        size = path.stat().st_size
        if size >= MAX_BYTES:
            raise ValueError("Файл превышает лимит. Выбери качество ниже.")
        update_job(job_id, status="ready", progress=100, message="Файл готов.", path=str(path), filename=filename, size=size)
    except Exception as exc:
        update_job(job_id, status="error", progress=0, message=public_error(exc, url))


def cleanup_jobs():
    now = time.time()
    stale = []
    with JOBS_LOCK:
        for jid, job in list(JOBS.items()):
            if now - job.get("updated", now) > JOB_TTL:
                stale.append((jid, job.get("folder")))
                JOBS.pop(jid, None)
    for _, folder in stale:
        if folder:
            shutil.rmtree(folder, ignore_errors=True)


