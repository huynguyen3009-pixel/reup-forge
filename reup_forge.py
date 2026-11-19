#!/usr/bin/env python3
# reup_forge.py — Douyin / Bilibili Desktop Studio, Anti-Fingerprint & Gemini AI Vietsub/Dubbing Engine
# Desktop GUI (Tkinter Tabbed: QUEUE / AI VIETSUB & DUB / VISUALS / AUDIO & BGM / CONFIG) + CLI Batch Processor

import argparse
import asyncio
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

# --- CONFIG BLOCK ---
K_APP_TITLE        = "REUP FORGE // DOUYIN & BILIBILI ANTI-ID + GEMINI VIETSUB STUDIO"
K_CONFIG_PATH      = Path("reup_config.json")
K_WORK_DIR         = Path("./_work")
K_OUTPUT_DIR       = Path("./output")
K_DEFAULT_SPEED    = 1.04
K_DEFAULT_ZOOM     = 1.06
K_DEFAULT_PITCH    = 1.03
K_DEFAULT_NOISE    = 3
K_DEFAULT_GAMMA    = 1.03
K_DEFAULT_SAT      = 1.05
K_DEFAULT_CONTRAST = 1.02
K_DEFAULT_CRF      = 20
K_DEFAULT_W        = 1080
K_DEFAULT_H        = 1920
K_DEFAULT_FPS      = 60
K_DEFAULT_BGM_VOL  = 0.08
K_DEFAULT_TRIM_S   = 0.25   # cắt 0.25s đầu/cuối phá duration signature
K_DEFAULT_SWAY_PX  = 6      # biên độ lắc camera ngầm (px) phá static block match

# Gemini & TTS Defaults
K_GEMINI_MODEL      = "gemini-3-flash-preview"
K_GEMINI_FALLBACKS  = [
    "gemini-3-flash-preview",
    "gemini-flash-lite-latest",
    "gemini-3.1-flash-lite-preview",
]
K_TTS_ENGINE        = "gemini-tts"  # "gemini-tts" (dùng chung Gemini API Key), "edge-tts", hoặc "google-tts"
K_GEMINI_VOICE      = "Kore"        # Kore, Puck, Aoede, Charon, Fenrir
K_EDGE_VOICE_FEMALE = "vi-VN-HoaiMyNeural"
K_EDGE_VOICE_MALE   = "vi-VN-NamMinhNeural"


@dataclass
class ForgeConfig:
    # VISUALS
    hflip: bool = True
    vertical_9_16: bool = True
    zoom_crop: float = K_DEFAULT_ZOOM
    camera_sway: bool = True
    sway_px: int = K_DEFAULT_SWAY_PX
    gamma: float = K_DEFAULT_GAMMA
    saturation: float = K_DEFAULT_SAT
    contrast: float = K_DEFAULT_CONTRAST
    noise_luma: int = K_DEFAULT_NOISE
    vignette: bool = True
    speed_Bar: float = K_DEFAULT_SPEED
    trim_edges_sec: float = K_DEFAULT_TRIM_S
    delogo_enable: bool = False
    delogo_rect: str = "40:40:180:70"  # x:y:w:h
    overlay_text: str = ""
    target_w: int = K_DEFAULT_W
    target_h: int = K_DEFAULT_H
    target_fps: int = K_DEFAULT_FPS

    # AUDIO & BGM
    pitch_shift: float = K_DEFAULT_PITCH
    eq_bandpass: bool = True
    bgm_path: str = ""
    bgm_volume: float = K_DEFAULT_BGM_VOL
    strip_audio: bool = False

    # AI VIETSUB & DUBBING (GEMINI)
    ai_enable: bool = False
    gemini_api_key: str = ""
    gemini_model: str = K_GEMINI_MODEL
    translation_style: str = "Tự nhiên, bắt trend TikTok/Shorts, ngắn gọn khớp khẩu hình"
    burn_vietsub: bool = True
    cover_cn_hardsub: bool = True      # Đổ hộp nền mờ đè lên vị trí phụ đề tiếng Trung gốc
    sub_y_margin: int = 230            # Khoảng cách từ đáy video lên (px)
    sub_font_size: int = 54
    tts_enable: bool = True
    tts_engine: str = K_TTS_ENGINE     # gemini-tts / edge-tts / google-tts
    tts_voice: str = K_GEMINI_VOICE
    tts_rate_bonus: str = "+10%"       # Tăng nhẹ tốc độ đọc tiếng Việt để kịp nhịp tiếng Trung
    orig_audio_duck: float = 0.15      # Giảm âm lượng tiếng Trung gốc xuống 15% khi lồng tiếng Việt
    export_srt: bool = True

    # SYSTEM / CONFIG
    output_dir: str = str(K_OUTPUT_DIR)
    cookies_browser: str = "none"      # none / chrome / edge / firefox / brave
    cookies_file: str = ""
    hw_accel: str = "libx264"          # libx264 / h264_nvenc / h264_qsv / h264_amf
    crf: int = K_DEFAULT_CRF
    keep_raw: bool = False

    def save(self, path: Path = K_CONFIG_PATH) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: Path = K_CONFIG_PATH) -> "ForgeConfig":
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                valid = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
                return cls(**valid)
            except Exception:
                pass
        return cls()


def probe_media(path: Path) -> dict:
    """Lấy duration, width, height và kiểm tra stream audio qua ffprobe."""
    cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration:stream=codec_type,width,height",
        "-of", "json",
        str(path),
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    info = {"duration": 0.0, "width": 1080, "height": 1920, "has_audio": False}
    if res.returncode != 0:
        return info
    try:
        data = json.loads(res.stdout)
        info["duration"] = float(data.get("format", {}).get("duration", 0.0))
        for s in data.get("streams", []):
            if s.get("codec_type") == "video":
                info["width"] = int(s.get("width", 1080))
                info["height"] = int(s.get("height", 1920))
            elif s.get("codec_type") == "audio":
                info["has_audio"] = True
    except Exception:
        pass
    return info


def download_douyin_api_fallback(url: str, out_dir: Path, log_fn=print) -> Optional[Path]:
    """Resolver trực tiếp cho link rút gọn v.douyin.com / iesdouyin khi yt-dlp cần cookie."""
    try:
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) "
                    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Mobile/15E148 Safari/604.1"
                )
            },
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            final_url = resp.geturl()
            html = resp.read().decode("utf-8", errors="ignore")

        m = re.search(r"/video/(\d+)", final_url) or re.search(r'"aweme_id"\s*:\s*"(\d+)"', html)
        if not m:
            return None
        vid = m.group(1)
        play_url_match = re.search(r'"play_addr"\s*:\s*\{[^}]*"url_list"\s*:\s*\[\s*"([^"]+)"', html)
        if not play_url_match:
            return None
        raw_video_url = play_url_match.group(1).encode("utf-8").decode("unicode_escape").replace("playwm", "play")
        target = out_dir / f"raw_dy_{vid}.mp4"
        log_fn(f"[*] fallback douyin direct stream: {vid}")
        vreq = urllib.request.Request(raw_video_url, headers={"User-Agent": req.get_header("User-agent")})
        with urllib.request.urlopen(vreq, timeout=30) as vresp, open(target, "wb") as f:
            shutil.copyfileobj(vresp, f)
        if target.exists() and target.stat().st_size > 10000:
            return target
    except Exception as e:
        log_fn(f"[!] douyin fallback skip: {e}")
    return None


def fetch_video(source: str, cfg: ForgeConfig, log_fn=print) -> Path:
    """Tải video từ Douyin / Bilibili hoặc nhận file local."""
    if not source.startswith(("http://", "https://")):
        p = Path(source)
        if not p.exists():
            raise FileNotFoundError(f"Không tìm thấy file: {source}")
        return p

    K_WORK_DIR.mkdir(parents=True, exist_ok=True)
    tag = uuid.uuid4().hex[:8]
    out_tmpl = str(K_WORK_DIR / f"raw_{tag}.%(ext)s")

    yt_dlp_bin = shutil.which("yt-dlp")
    base_cmd = [yt_dlp_bin] if yt_dlp_bin else [sys.executable, "-m", "yt_dlp"]

    cmd = base_cmd + [
        "--no-playlist",
        "--no-warnings",
        "--geo-bypass",
        "-f", "bv*+ba/b",
        "--merge-output-format", "mp4",
        "-o", out_tmpl,
    ]

    if "bilibili.com" in source or "b23.tv" in source:
        cmd.extend(["--referer", "https://www.bilibili.com/"])

    if cfg.cookies_file and os.path.exists(cfg.cookies_file):
        cmd.extend(["--cookies", cfg.cookies_file])
    elif cfg.cookies_browser and cfg.cookies_browser != "none":
        cmd.extend(["--cookies-from-browser", cfg.cookies_browser])

    cmd.append(source)
    log_fn(f"[*] Đang tải stream: {source}")
    proc = subprocess.run(cmd, capture_output=True, text=True)

    matches = list(K_WORK_DIR.glob(f"raw_{tag}.*"))
    if proc.returncode == 0 and matches:
        return matches[0]

    if "douyin.com" in source:
        fb = download_douyin_api_fallback(source, K_WORK_DIR, log_fn=log_fn)
        if fb:
            return fb

    err_tail = (proc.stderr or proc.stdout or "yt-dlp failed").strip().splitlines()[-1]
    raise RuntimeError(f"Tải thất bại ({err_tail}). Nếu Douyin yêu cầu cookie, chọn trình duyệt ở tab CONFIG.")


# =====================================================================
# GEMINI AI: AUTO ASR (NHẬN SUB GỐC) + DỊCH TIẾNG VIỆT + TTS LỒNG TIẾNG
# =====================================================================
def extract_16k_audio_for_gemini(src_video: Path, trim_sec: float, duration: float, speed_factor: float) -> Path:
    """Trích xuất audio mono MP3 32kbps ĐÃ qua bước trim và tăng tốc để timestamp khớp 100% với video xuất."""
    K_WORK_DIR.mkdir(parents=True, exist_ok=True)
    out_mp3 = K_WORK_DIR / f"asr_{uuid.uuid4().hex[:8]}.mp3"

    trim_args = []
    if duration > (trim_sec * 3 + 1.0) and trim_sec > 0:
        trim_args = ["-ss", str(round(trim_sec, 3)), "-to", str(round(duration - trim_sec, 3))]

    spd = max(0.5, min(2.0, speed_factor))
    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + trim_args
        + ["-i", str(src_video)]
        + [
            "-vn",
            "-af", f"atempo={spd}",
            "-ac", "1",
            "-ar", "16000",
            "-b:a", "32k",
            str(out_mp3),
        ]
    )
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0 or not out_mp3.exists():
        raise RuntimeError(f"Không thể tách âm thanh cho Gemini ASR: {res.stderr}")
    return out_mp3


def discover_available_gemini_models(api_key: str) -> list[str]:
    """Truy vấn danh sách model khả dụng thực tế của API Key từ endpoint /v1beta/models."""
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = []
        for m in data.get("models", []):
            methods = m.get("supportedGenerationMethods", [])
            name = m.get("name", "").replace("models/", "")
            if "generateContent" in methods and "gemini" in name and "tts" not in name and "image" not in name:
                models.append(name)
        # Ưu tiên dòng flash mới nhất (3.x -> 2.5 -> latest)
        models.sort(key=lambda x: ("flash" not in x, "3" not in x, x))
        return models
    except Exception:
        return []


def gemini_transcribe_and_translate_vi(audio_mp3: Path, cfg: ForgeConfig, log_fn=print) -> list[dict]:
    """
    Gửi audio trực tiếp tới Gemini API (generateContent) để nhận diện giọng nói gốc (Trung/Anh/...)
    và dịch thẳng sang các câu phụ đề tiếng Việt có timestamp [start, end, text_origin, text_vi].
    Tự động fallback hoặc dò model từ /v1beta/models nếu model cũ (vd gemini-2.5-flash) bị 404.
    """
    api_key = (cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise ValueError("Chưa nhập Gemini API Key! Hãy nhập API Key ở tab 'AI VIETSUB & DUB' hoặc biến môi trường GEMINI_API_KEY.")

    audio_bytes = audio_mp3.read_bytes()
    b64_audio = base64.b64encode(audio_bytes).decode("ascii")

    prompt = (
        "Bạn là chuyên gia nhận diện giọng nói (ASR) và biên dịch phụ đề video ngắn (Douyin/Bilibili sang TikTok/YouTube Shorts tiếng Việt).\n"
        f"Phong cách dịch: {cfg.translation_style}.\n"
        "Hãy nghe file âm thanh này, chia thành từng câu ngắn theo đúng nhịp nói, nhận diện lời gốc và dịch sang tiếng Việt.\n"
        "YÊU CẦU BẮT BUỘC:\n"
        "1. Trả về DUY NHẤT một mảng JSON (JSON Array), mỗi phần tử có cấu trúc:\n"
        '   {"start": 0.0, "end": 2.4, "origin": "lời gốc", "vi": "lời dịch tiếng Việt ngắn gọn, tự nhiên"}\n'
        "2. `start` và `end` là số thực tính bằng giây (ví dụ 1.25), chính xác theo thời điểm bắt đầu và kết thúc câu nói trong file audio.\n"
        "3. Câu dịch `vi` phải cô đọng để đọc kịp trong khoảng (end - start) giây, không quá 14 từ mỗi câu.\n"
        "4. Nếu đoạn nào chỉ có nhạc không có lời nói thì bỏ qua, không tạo segment."
    )

    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt},
                    {
                        "inline_data": {
                            "mime_type": "audio/mp3",
                            "data": b64_audio,
                        }
                    },
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.2,
            "responseMimeType": "application/json",
        },
    }

    preferred = cfg.gemini_model.strip() or K_GEMINI_MODEL
    # Khám phá trước danh sách thực tế từ API Key để luôn có đủ toàn bộ model khả dụng
    discovered_all = discover_available_gemini_models(api_key)
    candidate_models = [preferred]
    for m in K_GEMINI_FALLBACKS + discovered_all:
        if m not in candidate_models:
            candidate_models.append(m)

    resp_data = None
    last_error_msg = ""

    for idx, model_name in enumerate(candidate_models):
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
        # Cho phép retry tối đa 3 lần trên mỗi model nếu gặp 503 (High Demand / Overloaded) hoặc 429 (Rate Limit)
        for attempt in range(1, 4):
            log_fn(
                f"[*] Gửi audio ({len(audio_bytes)//1024} KB) lên Gemini ({model_name}"
                f"{f' - thử lần {attempt}' if attempt > 1 else ''}) để nhận sub & dịch tiếng Việt..."
            )
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=240) as resp:
                    resp_data = json.loads(resp.read().decode("utf-8"))
                    cfg.gemini_model = model_name
                    break
            except urllib.error.HTTPError as e:
                err_body = e.read().decode("utf-8", errors="ignore")
                last_error_msg = f"Gemini API HTTP {e.code} ({model_name}): {err_body}"

                # Lỗi 503 (Server quá tải tạm thời) hoặc 429 (Quota/Rate limit): chờ ngắn rồi thử lại hoặc nhảy sang model khác
                if e.code in (503, 429, 500, 502, 504):
                    wait_s = 3 * attempt
                    log_fn(f"[!] Server {model_name} đang quá tải (HTTP {e.code}), chờ {wait_s}s rồi thử lại...")
                    time.sleep(wait_s)
                    continue

                # Lỗi 404 / 400 (Model không tồn tại hoặc khóa với user mới): nhảy ngay sang model kế tiếp
                if e.code in (404, 400):
                    suggested = re.findall(r"models/(gemini-[a-zA-Z0-9._-]+)", err_body)
                    for sm in suggested:
                        if sm != model_name and sm not in candidate_models:
                            candidate_models.insert(idx + 1, sm)
                    log_fn(f"[!] Model {model_name} không khả dụng (HTTP {e.code}), chuyển sang model kế tiếp...")
                    break

                raise RuntimeError(last_error_msg)
            except (urllib.error.URLError, TimeoutError) as net_err:
                last_error_msg = f"Lỗi kết nối tới Gemini ({model_name}): {net_err}"
                log_fn(f"[!] Nghẽn mạng ({net_err}), chờ 4s thử lại...")
                time.sleep(4)
                continue

        if resp_data is not None:
            break
        log_fn(f"[*] Chuyển từ {model_name} sang model dự phòng tiếp theo...")

    if resp_data is None:
        raise RuntimeError(last_error_msg or "Không tìm thấy Gemini model khả dụng cho API Key này.")

    try:
        raw_text = resp_data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError):
        raise RuntimeError(f"Phản hồi Gemini không hợp lệ: {json.dumps(resp_data)[:300]}")

    # Bóc JSON an toàn kể cả khi model bọc markdown ```json
    raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
    raw_text = re.sub(r"\s*```$", "", raw_text)
    segments = json.loads(raw_text)

    cleaned: list[dict] = []
    for seg in segments:
        vi_txt = str(seg.get("vi", "")).strip()
        if not vi_txt:
            continue
        st = max(0.0, float(seg.get("start", 0.0)))
        ed = max(st + 0.4, float(seg.get("end", st + 1.5)))
        cleaned.append({
            "start": round(st, 2),
            "end": round(ed, 2),
            "origin": str(seg.get("origin", "")).strip(),
            "vi": vi_txt,
        })

    log_fn(f"[+] Gemini nhận diện & dịch thành công {len(cleaned)} câu thoại tiếng Việt.")
    return cleaned


def format_srt_time(seconds: float) -> str:
    ms = int(round((seconds - int(seconds)) * 1000))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def format_ass_time(seconds: float) -> str:
    cs = int(round((seconds - int(seconds)) * 100))
    if cs >= 100:
        cs = 99
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:01d}:{m:02d}:{s:02d}.{cs:02d}"


def write_srt_file(segments: list[dict], srt_path: Path) -> None:
    lines = []
    for idx, seg in enumerate(segments, 1):
        lines.append(str(idx))
        lines.append(f"{format_srt_time(seg['start'])} --> {format_srt_time(seg['end'])}")
        lines.append(seg["vi"])
        lines.append("")
    srt_path.write_text("\n".join(lines), encoding="utf-8")


def write_ass_subtitle(segments: list[dict], ass_path: Path, cfg: ForgeConfig, play_w: int, play_h: int) -> None:
    """
    Tạo file phụ đề .ASS chuẩn TikTok/Shorts:
    - Nếu bật `cover_cn_hardsub`: dùng BorderStyle=3 (Opaque Box nền đen mờ &H55000000) đè trực tiếp lên vị trí sub Trung gốc.
    - Nếu tắt `cover_cn_hardsub`: dùng chữ vàng/trắng viền đen đậm nổi bật.
    """
    border_style = 3 if cfg.cover_cn_hardsub else 1
    # Màu ASS theo chuẩn &HAABBGGRR: Primary Vàng tươi (&H0000FFFF), Outline/Back Đen bán trong suốt (&H45000000)
    primary_color = "&H0033FFFF"
    back_color = "&H45000000" if cfg.cover_cn_hardsub else "&H00000000"
    outline_size = 16 if cfg.cover_cn_hardsub else 4
    shadow_size = 0 if cfg.cover_cn_hardsub else 2

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {play_w}
PlayResY: {play_h}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Vietsub,Arial,{cfg.sub_font_size},{primary_color},&H000000FF,{back_color},{back_color},-1,0,0,0,100,100,0,0,{border_style},{outline_size},{shadow_size},2,50,50,{cfg.sub_y_margin},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events = []
    for seg in segments:
        st = format_ass_time(seg["start"])
        ed = format_ass_time(seg["end"])
        txt = seg["vi"].replace("\n", "\\N").replace("{", "(").replace("}", ")")
        events.append(f"Dialogue: 0,{st},{ed},Vietsub,,0,0,0,,{txt}")

    ass_path.write_text(header + "\n".join(events) + "\n", encoding="utf-8")


async def _edge_tts_synthesize_segment(text: str, voice: str, rate: str, out_mp3: Path) -> None:
    import edge_tts
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate)
    await comm.save(str(out_mp3))


def google_translate_tts_fallback(text: str, out_mp3: Path) -> None:
    """Fallback Google Translate Vietnamese TTS qua HTTPS (không cần key phụ, luôn sẵn sàng)."""
    import urllib.parse
    q = urllib.parse.quote(text)
    url = f"https://translate.google.com/translate_tts?ie=UTF-8&tl=vi&client=tw-ob&q={q}"
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        data = resp.read()
    if len(data) < 256:
        raise RuntimeError("Google TTS returned empty audio")
    out_mp3.write_bytes(data)


def gemini_tts_synthesize_segment(text: str, cfg: ForgeConfig, out_wav: Path) -> None:
    """Sử dụng Gemini 2.5 Flash Preview TTS với chính Gemini API Key đã nhập để đọc câu tiếng Việt (PCM 24kHz -> WAV)."""
    api_key = (cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY", "")).strip()
    if not api_key:
        raise RuntimeError("No Gemini API key for gemini-tts")
    tts_model = "gemini-2.5-flash-preview-tts"
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{tts_model}:generateContent?key={api_key}"
    voice_name = cfg.tts_voice if cfg.tts_voice in ("Kore", "Puck", "Aoede", "Charon", "Fenrir") else K_GEMINI_VOICE

    payload = {
        "contents": [{"parts": [{"text": f"Đọc rõ ràng, tự nhiên bằng tiếng Việt: {text}"}]}],
        "generationConfig": {
            "responseModalities": ["AUDIO"],
            "speechConfig": {
                "voiceConfig": {
                    "prebuiltVoiceConfig": {"voiceName": voice_name}
                }
            },
        },
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    b64_pcm = data["candidates"][0]["content"]["parts"][0]["inlineData"]["data"]
    pcm_bytes = base64.b64decode(b64_pcm)
    with wave.open(str(out_wav), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(pcm_bytes)


def synthesize_vi_segment_with_fallback(text: str, cfg: ForgeConfig, base_path: Path) -> Path:
    """
    Chuỗi TTS 3 tầng tự động fallback:
    1. Engine được chọn (`gemini-tts`, `edge-tts`, hoặc `google-tts`)
    2. Nếu lỗi -> thử `edge-tts` (HoaiMyNeural) -> thử `google-tts` (HTTPS)
    """
    rate = cfg.tts_rate_bonus.strip() or "+10%"
    voice = cfg.tts_voice.strip() or K_GEMINI_VOICE

    engines_order = [cfg.tts_engine]
    for e in ("gemini-tts", "edge-tts", "google-tts"):
        if e not in engines_order:
            engines_order.append(e)

    last_err = None
    for eng in engines_order:
        try:
            if eng == "gemini-tts":
                out_w = base_path.with_suffix(".wav")
                gemini_tts_synthesize_segment(text, cfg, out_w)
                if out_w.exists() and out_w.stat().st_size > 400:
                    return out_w
            elif eng == "edge-tts":
                out_m = base_path.with_suffix(".mp3")
                v_name = voice if "vi-VN" in voice else K_EDGE_VOICE_FEMALE
                asyncio.run(_edge_tts_synthesize_segment(text, v_name, rate, out_m))
                if out_m.exists() and out_m.stat().st_size > 256:
                    return out_m
            elif eng == "google-tts":
                out_g = base_path.with_suffix(".mp3")
                google_translate_tts_fallback(text, out_g)
                if out_g.exists() and out_g.stat().st_size > 256:
                    return out_g
        except Exception as err:
            last_err = err
            continue

    raise RuntimeError(f"Tất cả TTS engine đều thất bại: {last_err}")


def build_vietnamese_dub_track(segments: list[dict], total_dur: float, cfg: ForgeConfig, log_fn=print) -> Optional[Path]:
    """
    Sinh giọng đọc tiếng Việt cho từng câu sub, tự động co giãn tốc độ đọc (atempo) nếu câu tiếng Việt
    dài hơn khe thời gian (end - start), sau đó ghép chuẩn xác theo timestamp vào 1 track WAV duy nhất.
    """
    if not segments:
        return None

    K_WORK_DIR.mkdir(parents=True, exist_ok=True)
    seg_files: list[tuple[float, Path]] = []
    voice = cfg.tts_voice.strip() or K_EDGE_VOICE_FEMALE
    rate = cfg.tts_rate_bonus.strip() or "+10%"

    log_fn(f"[*] Đang tạo giọng đọc tiếng Việt ({cfg.tts_engine} | voice={voice}) cho {len(segments)} câu...")

    for idx, seg in enumerate(segments):
        raw_base = K_WORK_DIR / f"tts_raw_{idx}_{uuid.uuid4().hex[:5]}"
        fitted_path = K_WORK_DIR / f"tts_fit_{idx}_{uuid.uuid4().hex[:5]}.wav"
        slot_dur = max(0.5, seg["end"] - seg["start"])

        try:
            raw_tts_path = synthesize_vi_segment_with_fallback(seg["vi"], cfg, raw_base)
        except Exception as e:
            log_fn(f"[!] Bỏ qua TTS câu {idx+1}: {e}")
            continue

        tts_info = probe_media(raw_tts_path)
        tts_dur = tts_info["duration"]
        if tts_dur <= 0.05:
            continue

        # Nếu câu đọc dài hơn khe thời gian cho phép, tăng nhẹ atempo (tối đa 1.45x) để không đè câu sau
        speedup = 1.0
        if tts_dur > slot_dur:
            speedup = min(1.45, round(tts_dur / slot_dur, 3))

        af_fit = f"atempo={speedup},aresample=44100"
        cmd_fit = [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(raw_tts_path),
            "-af", af_fit,
            "-ac", "1", "-ar", "44100",
            str(fitted_path),
        ]
        subprocess.run(cmd_fit, check=False)
        if raw_tts_path.exists():
            raw_tts_path.unlink(missing_ok=True)
        if fitted_path.exists():
            seg_files.append((seg["start"], fitted_path))

    if not seg_files:
        return None

    # Ghép tất cả các câu TTS vào timeline bằng bộ lọc adelay + amix (normalize=0)
    master_dub = K_WORK_DIR / f"dub_master_{uuid.uuid4().hex[:6]}.wav"
    inputs = ["-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={max(1.0, round(total_dur, 2))}"]
    filter_chains = []
    mix_labels = ["[0:a]"]

    for i, (start_sec, wav_p) in enumerate(seg_files, 1):
        inputs.extend(["-i", str(wav_p)])
        delay_ms = max(0, int(round(start_sec * 1000)))
        filter_chains.append(f"[{i}:a]adelay={delay_ms}|{delay_ms}[s{i}]")
        mix_labels.append(f"[s{i}]")

    n_inputs = len(mix_labels)
    filter_chains.append(
        f"{''.join(mix_labels)}amix=inputs={n_inputs}:duration=first:dropout_transition=0:normalize=0,volume=1.35[dubout]"
    )

    cmd_merge = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + inputs
        + ["-filter_complex", ";".join(filter_chains), "-map", "[dubout]", "-c:a", "pcm_s16le", str(master_dub)]
    )
    res = subprocess.run(cmd_merge, capture_output=True, text=True)

    for _, wav_p in seg_files:
        wav_p.unlink(missing_ok=True)

    if res.returncode != 0 or not master_dub.exists():
        log_fn(f"[!] Lỗi ghép track lồng tiếng: {res.stderr}")
        return None

    log_fn(f"[+] Đã tổng hợp xong track lồng tiếng Việt ({len(seg_files)} câu).")
    return master_dub


# =====================================================================
# FFMPEG ANTI-FINGERPRINT + SUBTITLE BURN + AUDIO MIXING ENGINE
# =====================================================================
def escape_ffmpeg_filter_path(p: Path) -> str:
    """Escape đường dẫn Windows (C:\\...) để nhúng vào filter `ass='...'` của FFmpeg."""
    s = str(p.resolve()).replace("\\", "/")
    s = s.replace(":", "\\:")
    s = s.replace("'", "\\'")
    return s


def build_ffmpeg_command(
    src: Path,
    dst: Path,
    cfg: ForgeConfig,
    media_info: dict,
    ass_path: Optional[Path] = None,
    dub_wav_path: Optional[Path] = None,
) -> list[str]:
    dur = media_info["duration"]
    has_orig_audio = media_info["has_audio"] and not cfg.strip_audio

    inputs = ["-i", str(src)]
    next_input_idx = 1

    bgm_idx = None
    if cfg.bgm_path and os.path.exists(cfg.bgm_path):
        inputs.extend(["-stream_loop", "-1", "-i", cfg.bgm_path])
        bgm_idx = next_input_idx
        next_input_idx += 1

    dub_idx = None
    if dub_wav_path and dub_wav_path.exists():
        inputs.extend(["-i", str(dub_wav_path)])
        dub_idx = next_input_idx
        next_input_idx += 1

    # Trim đầu/cuối phá duration hash
    trim_args = []
    if dur > (cfg.trim_edges_sec * 3 + 1.0) and cfg.trim_edges_sec > 0:
        start_t = round(cfg.trim_edges_sec, 3)
        end_t = round(dur - cfg.trim_edges_sec, 3)
        trim_args = ["-ss", str(start_t), "-to", str(end_t)]

    # --- Xây dựng chuỗi Video Filter ---
    pts_factor = round(1.0 / max(0.5, min(2.0, cfg.speed_Bar)), 5)
    zoom = max(1.01, float(cfg.zoom_crop))
    sway = max(1, int(cfg.sway_px)) if cfg.camera_sway else 0

    fg_filters = []
    if cfg.hflip:
        fg_filters.append("hflip")

    if cfg.delogo_enable and cfg.delogo_rect:
        parts = cfg.delogo_rect.split(":")
        if len(parts) == 4 and all(p.strip().isdigit() for p in parts):
            x, y, w, h = [p.strip() for p in parts]
            fg_filters.append(f"delogo=x={x}:y={y}:w={w}:h={h}:show=0")

    # Zoom + Dynamic Micro-Panning (lắc tâm cắt theo hàm sin/cos của thời gian t)
    fg_filters.append(f"scale=trunc(iw*{zoom}/2)*2:trunc(ih*{zoom}/2)*2")
    if sway > 0:
        fg_filters.append(
            f"crop=trunc(iw/{zoom}/2)*2:trunc(ih/{zoom}/2)*2:"
            f"'(iw-ow)/2+{sway}*sin(1.7*t)':"
            f"'(ih-oh)/2+{sway}*cos(1.3*t)'"
        )
    else:
        fg_filters.append(f"crop=trunc(iw/{zoom}/2)*2:trunc(ih/{zoom}/2)*2:4:4")

    post_filters = [
        f"eq=gamma={cfg.gamma}:saturation={cfg.saturation}:contrast={cfg.contrast}",
    ]
    if cfg.noise_luma > 0:
        post_filters.append(f"noise=alls={cfg.noise_luma}:allf=t")
    if cfg.vignette:
        post_filters.append("vignette=PI/10")
    if cfg.overlay_text.strip():
        safe_txt = (
            cfg.overlay_text.strip()
            .replace("\\", "\\\\")
            .replace(":", "\\:")
            .replace("'", "")
        )
        post_filters.append(
            f"drawtext=text='{safe_txt}':fontcolor=white@0.35:fontsize=28:"
            f"x='(w-text_w)/2+20*sin(0.8*t)':y='h-120'"
        )
    post_filters.extend([
        f"setpts={pts_factor}*PTS",
        f"fps={cfg.target_fps}",
    ])

    # Burn phụ đề ASS Tiếng Việt SAU bước setpts & lật hình (để chữ không bị ngược và khớp timestamp đã tăng tốc)
    if ass_path and ass_path.exists() and cfg.burn_vietsub:
        esc_ass = escape_ffmpeg_filter_path(ass_path)
        post_filters.append(f"ass='{esc_ass}'")

    post_filters.append("format=yuv420p")

    fg_chain_str = ",".join(fg_filters)
    post_chain_str = ",".join(post_filters)

    if cfg.vertical_9_16:
        tw, th = cfg.target_w, cfg.target_h
        vf_complex = (
            f"[0:v]split=2[bg][fg];"
            f"[bg]scale={tw}:{th}:force_original_aspect_ratio=increase,"
            f"crop={tw}:{th},boxblur=25:5[bg_blur];"
            f"[fg]{fg_chain_str},"
            f"scale={tw}:{th}:force_original_aspect_ratio=decrease[fg_scaled];"
            f"[bg_blur][fg_scaled]overlay=(W-w)/2:(H-h)/2,{post_chain_str}[vout]"
        )
    else:
        vf_complex = f"[0:v]{fg_chain_str},{post_chain_str}[vout]"

    # --- Xây dựng chuỗi Audio Filter (Gốc + Lồng tiếng Việt + Nhạc nền BGM) ---
    filter_parts = [vf_complex]
    map_args = ["-map", "[vout]"]
    audio_mix_Streams = []

    if has_orig_audio:
        pitch = max(0.8, min(1.25, cfg.pitch_shift))
        atempo_comp = round(cfg.speed_Bar / pitch, 5)
        atempo_comp = max(0.5, min(2.0, atempo_comp))

        # Nếu có lồng tiếng Việt (dub_idx), tự động hạ nhỏ âm lượng tiếng Trung gốc xuống orig_audio_duck (15%)
        orig_vol = cfg.orig_audio_duck if (dub_idx is not None) else 1.02

        af_chain = [
            f"asetrate=44100*{pitch}",
            "aresample=44100",
            f"atempo={atempo_comp}",
        ]
        if cfg.eq_bandpass:
            af_chain.extend([
                "highpass=f=50",
                "lowpass=f=15200",
                "aecho=0.8:0.88:12:0.04",
            ])
        af_chain.append(f"volume={orig_vol}")
        filter_parts.append(f"[0:a]{','.join(af_chain)}[aorig]")
        audio_mix_Streams.append("[aorig]")

    if dub_idx is not None:
        filter_parts.append(f"[{dub_idx}:a]aresample=44100,volume=1.15[adub]")
        audio_mix_Streams.append("[adub]")

    if bgm_idx is not None:
        bgm_vol = max(0.01, min(1.0, cfg.bgm_volume))
        filter_parts.append(f"[{bgm_idx}:a]aresample=44100,volume={bgm_vol}[abgm]")
        audio_mix_Streams.append("[abgm]")

    if len(audio_mix_Streams) == 1:
        only_lbl = audio_mix_Streams[0]
        filter_parts.append(f"{only_lbl}anull[aout]")
        map_args.extend(["-map", "[aout]", "-shortest"])
    elif len(audio_mix_Streams) > 1:
        n_mix = len(audio_mix_Streams)
        filter_parts.append(
            f"{''.join(audio_mix_Streams)}amix=inputs={n_mix}:duration=first:dropout_transition=2:normalize=0[aout]"
        )
        map_args.extend(["-map", "[aout]", "-shortest"])

    full_filter = ";".join(filter_parts)

    # Chọn bộ mã hoá video
    vcodec_args = ["-c:v", cfg.hw_accel]
    if cfg.hw_accel == "libx264":
        vcodec_args.extend(["-preset", "fast", "-crf", str(cfg.crf)])
    else:
        vcodec_args.extend(["-b:v", "6M"])

    cmd = (
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error"]
        + trim_args
        + inputs
        + ["-filter_complex", full_filter]
        + map_args
        + [
            "-map_metadata", "-1",
            "-map_chapters", "-1",
            "-fflags", "+bitexact",
            "-flags:v", "+bitexact",
            "-flags:a", "+bitexact",
        ]
        + vcodec_args
        + [
            "-c:a", "aac",
            "-b:a", "192k",
            "-movflags", "+faststart",
            str(dst),
        ]
    )
    return cmd


def process_single_item(source: str, cfg: ForgeConfig, out_file: Optional[Path] = None, log_fn=print) -> Path:
    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    is_remote = source.startswith(("http://", "https://"))

    raw_path = fetch_video(source, cfg, log_fn=log_fn)
    ass_temp: Optional[Path] = None
    dub_temp: Optional[Path] = None
    asr_mp3_temp: Optional[Path] = None

    try:
        info = probe_media(raw_path)
        log_fn(
            f"[*] Media gốc: {info['width']}x{info['height']} | "
            f"{info['duration']:.1f}s | audio={'có' if info['has_audio'] else 'không'}"
        )

        if out_file is None:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            uid = uuid.uuid4().hex[:5]
            out_file = out_dir / f"reup_{stamp}_{uid}.mp4"

        # Tính thời lượng sau khi trim và đổi tốc độ
        trimmed_dur = info["duration"]
        if trimmed_dur > (cfg.trim_edges_sec * 3 + 1.0) and cfg.trim_edges_sec > 0:
            trimmed_dur -= cfg.trim_edges_sec * 2
        effective_dur = trimmed_dur / max(0.5, min(2.0, cfg.speed_Bar))

        # --- Bước AI Vietsub & Dubbing qua Gemini ---
        if cfg.ai_enable and info["has_audio"]:
            asr_mp3_temp = extract_16k_audio_for_gemini(
                raw_path, cfg.trim_edges_sec, info["duration"], cfg.speed_Bar
            )
            segments = gemini_transcribe_and_translate_vi(asr_mp3_temp, cfg, log_fn=log_fn)

            if segments:
                if cfg.export_srt:
                    srt_out = out_file.with_suffix(".vi.srt")
                    write_srt_file(segments, srt_out)
                    log_fn(f"[+] Đã xuất file phụ đề rời: {srt_out.name}")

                play_w = cfg.target_w if cfg.vertical_9_16 else info["width"]
                play_h = cfg.target_h if cfg.vertical_9_16 else info["height"]
                ass_temp = K_WORK_DIR / f"sub_{uuid.uuid4().hex[:6]}.ass"
                write_ass_subtitle(segments, ass_temp, cfg, play_w, play_h)

                if cfg.tts_enable:
                    dub_temp = build_vietnamese_dub_track(segments, effective_dur, cfg, log_fn=log_fn)

        cmd = build_ffmpeg_command(
            raw_path, out_file, cfg, info, ass_path=ass_temp, dub_wav_path=dub_temp
        )
        log_fn(f"[*] Đang render anti-fingerprint + Vietsub/Dub -> {out_file.name} ...")
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"FFmpeg lỗi: {res.stderr.strip()}")

        log_fn(f"[+] Hoàn tất: {out_file.resolve()}")
        return out_file
    finally:
        for tmp in (ass_temp, dub_temp, asr_mp3_temp):
            if tmp and tmp.exists():
                tmp.unlink(missing_ok=True)
        if is_remote and not cfg.keep_raw and raw_path.exists():
            raw_path.unlink(missing_ok=True)


def generate_selftest_sample(dst: Path) -> None:
    """Sinh video test 3.5 giây có giọng nói tiếng Trung mẫu (qua edge-tts) để test cả pipeline ASR + Sub + Dub."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    tts_sample = dst.parent / "sample_zh.mp3"
    try:
        asyncio.run(
            _edge_tts_synthesize_segment(
                "大家好，欢迎来到我的频道，今天给大家分享一个非常实用的视频工具。",
                "zh-CN-XiaoxiaoNeural",
                "+0%",
                tts_sample,
            )
        )
        audio_input = ["-i", str(tts_sample)]
    except Exception:
        audio_input = ["-f", "lavfi", "-i", "sine=frequency=440:duration=3.5"]

    cmd = (
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "testsrc=duration=3.8:size=1280x720:rate=30",
        ]
        + audio_input
        + [
            "-shortest",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(dst),
        ]
    )
    subprocess.run(cmd, check=True)
    tts_sample.unlink(missing_ok=True)


# =====================================================================
# DESKTOP GUI (MODERN HIGH-CONTRAST DARK STUDIO — 5 TABS)
# =====================================================================
def launch_gui(cfg: ForgeConfig) -> int:
    import ctypes
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    # Bật DPI Awareness trên Windows để chữ sắc nét, không bị nhoè
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    root = tk.Tk()
    root.title(K_APP_TITLE)
    root.geometry("1080x800")
    root.minsize(980, 720)

    # Bảng màu Modern Zinc/Slate Dark Studio (Độ tương phản chuẩn WCAG AAA)
    c_bg_app      = "#0b0f17"   # Nền chính cửa sổ
    c_bg_card     = "#151c28"   # Nền khung panel
    c_bg_input    = "#1e293b"   # Nền ô nhập liệu (Sáng rõ, tách biệt hẳn với nền card)
    c_bg_term     = "#090d14"   # Nền hộp Log / Queue
    c_border      = "#334155"   # Viền ô input
    c_border_foc  = "#38bdf8"   # Viền khi click vào ô input (Cyan sáng)
    c_fg_primary  = "#f8fafc"   # Chữ chính (Trắng sáng)
    c_fg_muted    = "#94a3b8"   # Chữ phụ / chú thích
    c_fg_input    = "#ffffff"   # Chữ trong các box text (Trắng tinh 100% cực dễ đọc)
    c_accent      = "#2563eb"   # Nút chính (Royal Blue)
    c_accent_hov  = "#3b82f6"
    c_btn_sec     = "#1e293b"   # Nút phụ
    c_btn_sec_hov = "#334155"

    root.configure(bg=c_bg_app)

    # Cấu hình Combobox dropdown listbox để không bị chữ đen trên nền tối hoặc chữ mờ
    root.option_add("*TCombobox*Listbox.background", c_bg_input)
    root.option_add("*TCombobox*Listbox.foreground", c_fg_input)
    root.option_add("*TCombobox*Listbox.selectBackground", c_accent)
    root.option_add("*TCombobox*Listbox.selectForeground", "#ffffff")
    root.option_add("*TCombobox*Listbox.font", ("Segoe UI", 11))

    style = ttk.Style()
    style.theme_use("clam")

    style.configure(".", background=c_bg_app, foreground=c_fg_primary, font=("Segoe UI", 10))
    style.configure("TFrame", background=c_bg_card)
    style.configure("App.TFrame", background=c_bg_app)
    style.configure("TLabel", background=c_bg_card, foreground=c_fg_primary, font=("Segoe UI", 10))
    style.configure("Muted.TLabel", background=c_bg_card, foreground=c_fg_muted, font=("Segoe UI", 9))
    style.configure("Section.TLabel", background=c_bg_card, foreground="#38bdf8", font=("Segoe UI", 11, "bold"))

    # Checkbutton hiện đại, rõ trạng thái
    style.configure(
        "TCheckbutton",
        background=c_bg_card,
        foreground=c_fg_primary,
        indicatorbackground=c_bg_input,
        indicatorforeground="#38bdf8",
        indicatormargin=[0, 0, 8, 0],
        font=("Segoe UI", 10),
    )
    style.map(
        "TCheckbutton",
        background=[("active", c_bg_card)],
        foreground=[("active", "#ffffff")],
        indicatorbackground=[("selected", c_accent), ("active", c_border)],
    )

    # Entry & Combobox có nền #1e293b và chữ #ffffff rõ nét
    style.configure(
        "TEntry",
        fieldbackground=c_bg_input,
        foreground=c_fg_input,
        insertcolor="#ffffff",
        bordercolor=c_border,
        lightcolor=c_border,
        darkcolor=c_border,
        borderwidth=1,
        padding=[10, 7],
    )
    style.map(
        "TEntry",
        fieldbackground=[("readonly", c_bg_input), ("disabled", "#0f172a")],
        foreground=[("readonly", c_fg_input), ("disabled", c_fg_muted)],
        lightcolor=[("focus", c_border_foc)],
        bordercolor=[("focus", c_border_foc)],
    )

    style.configure(
        "TCombobox",
        fieldbackground=c_bg_input,
        background=c_btn_sec,
        foreground=c_fg_input,
        arrowcolor="#38bdf8",
        bordercolor=c_border,
        lightcolor=c_border,
        darkcolor=c_border,
        padding=[10, 6],
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", c_bg_input), ("!disabled", c_bg_input)],
        foreground=[("readonly", c_fg_input), ("!disabled", c_fg_input)],
        selectbackground=[("readonly", c_bg_input)],
        selectforeground=[("readonly", c_fg_input)],
        bordercolor=[("focus", c_border_foc)],
    )

    # Tabs (Notebook)
    style.configure("TNotebook", background=c_bg_app, borderwidth=0)
    style.configure(
        "TNotebook.Tab",
        background="#161f2e",
        foreground=c_fg_muted,
        font=("Segoe UI", 10, "bold"),
        padding=[18, 10],
        borderwidth=0,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", c_accent), ("active", "#1e293b")],
        foreground=[("selected", "#ffffff"), ("active", "#ffffff")],
    )

    # Buttons
    style.configure(
        "TButton",
        background=c_btn_sec,
        foreground=c_fg_primary,
        bordercolor=c_border,
        font=("Segoe UI", 10, "bold"),
        padding=[14, 7],
    )
    style.map(
        "TButton",
        background=[("active", c_btn_sec_hov), ("disabled", "#0f172a")],
        foreground=[("disabled", "#475569")],
    )

    style.configure(
        "Accent.TButton",
        background=c_accent,
        foreground="#ffffff",
        bordercolor="#3b82f6",
        font=("Segoe UI", 10, "bold"),
        padding=[20, 8],
    )
    style.map(
        "Accent.TButton",
        background=[("active", c_accent_hov), ("disabled", "#1e3a8a")],
        foreground=[("disabled", "#94a3b8")],
    )

    style.configure(
        "Horizontal.TProgressbar",
        troughcolor=c_bg_term,
        background="#10b981",
        bordercolor=c_border,
        lightcolor="#10b981",
        darkcolor="#059669",
        thickness=12,
    )

    # --- HEADER BANNER ---
    header = tk.Frame(root, bg="#0f172a", height=58, highlightthickness=1, highlightbackground="#1e293b")
    header.pack(fill="x", padx=14, pady=(12, 4))
    header.pack_propagate(False)

    tk.Label(
        header,
        text="⚡ REUP FORGE STUDIO",
        bg="#0f172a",
        fg="#38bdf8",
        font=("Segoe UI", 14, "bold"),
    ).pack(side="left", padx=(16, 10))

    tk.Label(
        header,
        text="Douyin & Bilibili Anti-Fingerprint  •  Gemini AI Vietsub & Neural Voice Dubbing",
        bg="#0f172a",
        fg="#94a3b8",
        font=("Segoe UI", 10),
    ).pack(side="left", padx=4)

    status_badge = tk.Label(
        header,
        text=" ● READY ",
        bg="#064e3b",
        fg="#34d399",
        font=("Consolas", 10, "bold"),
        padx=10,
        pady=4,
    )
    status_badge.pack(side="right", padx=16)

    # Tkinter variables bound to ForgeConfig
    v_hflip          = tk.BooleanVar(value=cfg.hflip)
    v_vert           = tk.BooleanVar(value=cfg.vertical_9_16)
    v_zoom           = tk.DoubleVar(value=cfg.zoom_crop)
    v_sway           = tk.BooleanVar(value=cfg.camera_sway)
    v_sway_px        = tk.IntVar(value=cfg.sway_px)
    v_gamma          = tk.DoubleVar(value=cfg.gamma)
    v_sat            = tk.DoubleVar(value=cfg.saturation)
    v_contrast       = tk.DoubleVar(value=cfg.contrast)
    v_noise          = tk.IntVar(value=cfg.noise_luma)
    v_vignette       = tk.BooleanVar(value=cfg.vignette)
    v_speed          = tk.DoubleVar(value=cfg.speed_Bar)
    v_trim           = tk.DoubleVar(value=cfg.trim_edges_sec)
    v_delogo         = tk.BooleanVar(value=cfg.delogo_enable)
    v_delogo_rect    = tk.StringVar(value=cfg.delogo_rect)
    v_overlay_txt    = tk.StringVar(value=cfg.overlay_text)

    # AI Vietsub & Dubbing vars
    v_ai_enable      = tk.BooleanVar(value=cfg.ai_enable)
    v_api_key        = tk.StringVar(value=cfg.gemini_api_key)
    v_gemini_model   = tk.StringVar(value=cfg.gemini_model)
    v_trans_style    = tk.StringVar(value=cfg.translation_style)
    v_burn_sub       = tk.BooleanVar(value=cfg.burn_vietsub)
    v_cover_cn       = tk.BooleanVar(value=cfg.cover_cn_hardsub)
    v_sub_ymargin    = tk.IntVar(value=cfg.sub_y_margin)
    v_sub_fontsize   = tk.IntVar(value=cfg.sub_font_size)
    v_tts_enable     = tk.BooleanVar(value=cfg.tts_enable)
    v_tts_engine     = tk.StringVar(value=cfg.tts_engine)
    v_tts_voice      = tk.StringVar(value=cfg.tts_voice)
    v_tts_rate       = tk.StringVar(value=cfg.tts_rate_bonus)
    v_orig_duck      = tk.DoubleVar(value=cfg.orig_audio_duck)
    v_export_srt     = tk.BooleanVar(value=cfg.export_srt)

    # Audio & BGM vars
    v_pitch          = tk.DoubleVar(value=cfg.pitch_shift)
    v_eq             = tk.BooleanVar(value=cfg.eq_bandpass)
    v_bgm_path       = tk.StringVar(value=cfg.bgm_path)
    v_bgm_vol        = tk.DoubleVar(value=cfg.bgm_volume)
    v_strip_audio    = tk.BooleanVar(value=cfg.strip_audio)

    # System / Config vars
    v_out_dir        = tk.StringVar(value=cfg.output_dir)
    v_cookie_browser = tk.StringVar(value=cfg.cookies_browser)
    v_cookie_file    = tk.StringVar(value=cfg.cookies_file)
    v_hw_accel       = tk.StringVar(value=cfg.hw_accel)
    v_crf            = tk.IntVar(value=cfg.crf)
    v_keep_raw       = tk.BooleanVar(value=cfg.keep_raw)

    def sync_vars_to_cfg() -> ForgeConfig:
        cfg.hflip = v_hflip.get()
        cfg.vertical_9_16 = v_vert.get()
        cfg.zoom_crop = round(float(v_zoom.get()), 3)
        cfg.camera_sway = v_sway.get()
        cfg.sway_px = int(v_sway_px.get())
        cfg.gamma = round(float(v_gamma.get()), 3)
        cfg.saturation = round(float(v_sat.get()), 3)
        cfg.contrast = round(float(v_contrast.get()), 3)
        cfg.noise_luma = int(v_noise.get())
        cfg.vignette = v_vignette.get()
        cfg.speed_Bar = round(float(v_speed.get()), 3)
        cfg.trim_edges_sec = round(float(v_trim.get()), 2)
        cfg.delogo_enable = v_delogo.get()
        cfg.delogo_rect = v_delogo_rect.get().strip()
        cfg.overlay_text = v_overlay_txt.get().strip()

        cfg.ai_enable = v_ai_enable.get()
        cfg.gemini_api_key = v_api_key.get().strip()
        cfg.gemini_model = v_gemini_model.get().strip()
        cfg.translation_style = v_trans_style.get().strip()
        cfg.burn_vietsub = v_burn_sub.get()
        cfg.cover_cn_hardsub = v_cover_cn.get()
        cfg.sub_y_margin = int(v_sub_ymargin.get())
        cfg.sub_font_size = int(v_sub_fontsize.get())
        cfg.tts_enable = v_tts_enable.get()
        cfg.tts_engine = v_tts_engine.get().strip()
        cfg.tts_voice = v_tts_voice.get().strip()
        cfg.tts_rate_bonus = v_tts_rate.get().strip()
        cfg.orig_audio_duck = round(float(v_orig_duck.get()), 2)
        cfg.export_srt = v_export_srt.get()

        cfg.pitch_shift = round(float(v_pitch.get()), 3)
        cfg.eq_bandpass = v_eq.get()
        cfg.bgm_path = v_bgm_path.get().strip()
        cfg.bgm_volume = round(float(v_bgm_vol.get()), 3)
        cfg.strip_audio = v_strip_audio.get()

        cfg.output_dir = v_out_dir.get().strip() or str(K_OUTPUT_DIR)
        cfg.cookies_browser = v_cookie_browser.get().strip()
        cfg.cookies_file = v_cookie_file.get().strip()
        cfg.hw_accel = v_hw_accel.get().strip()
        cfg.crf = int(v_crf.get())
        cfg.keep_raw = v_keep_raw.get()
        return cfg

    def make_entry(parent, tvar, width=None, show=None):
        """Tạo ô nhập chữ (Entry) có nền sáng #1e293b, chữ trắng rõ #ffffff, viền highlight xanh khi focus."""
        kw = {
            "textvariable": tvar,
            "bg": c_bg_input,
            "fg": c_fg_input,
            "insertbackground": "#38bdf8",
            "selectbackground": c_accent,
            "selectforeground": "#ffffff",
            "relief": "flat",
            "font": ("Segoe UI", 10),
            "bd": 6,
            "highlightthickness": 1,
            "highlightbackground": c_border,
            "highlightcolor": c_border_foc,
        }
        if width is not None:
            kw["width"] = width
        if show is not None:
            kw["show"] = show
        return tk.Entry(parent, **kw)

    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=14, pady=(6, 14))

    tab_queue   = ttk.Frame(nb, padding=18)
    tab_ai      = ttk.Frame(nb, padding=18)
    tab_visuals = ttk.Frame(nb, padding=18)
    tab_audio   = ttk.Frame(nb, padding=18)
    tab_config  = ttk.Frame(nb, padding=18)

    nb.add(tab_queue,   text="  📥 QUEUE & BATCH  ")
    nb.add(tab_ai,      text="  🤖 AI VIETSUB & DUB  ")
    nb.add(tab_visuals, text="  🎬 VISUALS (ANTI-pHASH)  ")
    nb.add(tab_audio,   text="  🔊 AUDIO & BGM  ")
    nb.add(tab_config,  text="  ⚙️ CONFIG  ")

    def add_row(parent, row, label_txt, widget):
        lbl = ttk.Label(parent, text=label_txt, font=("Segoe UI", 10, "bold"))
        lbl.grid(row=row, column=0, sticky="w", pady=6, padx=(0, 20))
        widget.grid(row=row, column=1, sticky="ew", pady=6)

    # --- TAB 1: QUEUE & BATCH ---
    ttk.Label(
        tab_queue,
        text="DANH SÁCH NGUỒN VIDEO (Mỗi dòng 1 link Douyin / Bilibili hoặc đường dẫn file MP4 local):",
        style="Section.TLabel",
    ).pack(anchor="w", pady=(0, 6))

    url_frame = tk.Frame(tab_queue, bg=c_border, padx=1, pady=1)
    url_frame.pack(fill="x", pady=(0, 10))

    url_scroll = ttk.Scrollbar(url_frame, orient="vertical")
    txt_urls = tk.Text(
        url_frame,
        height=7,
        bg=c_bg_input,
        fg="#f8fafc",
        insertbackground="#38bdf8",
        selectbackground=c_accent,
        selectforeground="#ffffff",
        font=("Consolas", 11),
        relief="flat",
        padx=12,
        pady=10,
        yscrollcommand=url_scroll.set,
    )
    url_scroll.config(command=txt_urls.yview)
    url_scroll.pack(side="right", fill="y")
    txt_urls.pack(side="left", fill="x", expand=True)

    btn_bar = ttk.Frame(tab_queue)
    btn_bar.pack(fill="x", pady=(2, 10))

    def add_local_files():
        files = filedialog.askopenfilenames(
            title="Chọn video MP4/MKV",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.webm"), ("All files", "*.*")],
        )
        for f in files:
            txt_urls.insert("end", f + "\n")

    def open_output_folder():
        out_p = Path(v_out_dir.get().strip() or str(K_OUTPUT_DIR)).resolve()
        out_p.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(out_p))
        except Exception:
            pass

    ttk.Button(btn_bar, text="📂 + Thêm File Video Local", command=add_local_files).pack(side="left", padx=(0, 8))
    ttk.Button(btn_bar, text="🗑️ Xoá Danh Sách", command=lambda: txt_urls.delete("1.0", "end")).pack(side="left", padx=(0, 8))
    ttk.Button(btn_bar, text="📁 Mở Thư Mục Output", command=open_output_folder).pack(side="left", padx=(0, 8))

    progress = ttk.Progressbar(tab_queue, mode="determinate", style="Horizontal.TProgressbar")
    progress.pack(fill="x", pady=(2, 10))

    ttk.Label(tab_queue, text="NHẬT KÝ TIẾN TRÌNH (LIVE CONSOLE LOG):", style="Section.TLabel").pack(anchor="w", pady=(0, 6))

    log_frame = tk.Frame(tab_queue, bg=c_border, padx=1, pady=1)
    log_frame.pack(fill="both", expand=True)

    log_scroll = ttk.Scrollbar(log_frame, orient="vertical")
    txt_log = tk.Text(
        log_frame,
        height=13,
        bg=c_bg_term,
        fg="#e2e8f0",
        font=("Consolas", 10),
        relief="flat",
        padx=12,
        pady=10,
        state="disabled",
        spacing1=3,
        spacing3=3,
        yscrollcommand=log_scroll.set,
    )
    log_scroll.config(command=txt_log.yview)
    log_scroll.pack(side="right", fill="y")
    txt_log.pack(side="left", fill="both", expand=True)

    # Phân màu dòng Log thông minh để nhìn cực rõ trạng thái
    txt_log.tag_config("success", foreground="#34d399", font=("Consolas", 10, "bold"))
    txt_log.tag_config("error",   foreground="#f87171", font=("Consolas", 10, "bold"))
    txt_log.tag_config("info",    foreground="#38bdf8")
    txt_log.tag_config("header",  foreground="#fbbf24", font=("Consolas", 10, "bold"))
    txt_log.tag_config("normal",  foreground="#e2e8f0")

    log_queue: queue.Queue[str] = queue.Queue()

    def push_log(msg: str):
        log_queue.put(msg)

    def poll_logs():
        while not log_queue.empty():
            msg = log_queue.get_nowait()
            tag = "normal"
            s = msg.strip()
            if s.startswith("[+]") or "=== XONG" in s:
                tag = "success"
            elif s.startswith("[!]"):
                tag = "error"
            elif s.startswith("[*]"):
                tag = "info"
            elif s.startswith("---"):
                tag = "header"

            txt_log.configure(state="normal")
            txt_log.insert("end", msg + "\n", tag)
            txt_log.see("end")
            txt_log.configure(state="disabled")
        root.after(80, poll_logs)

    poll_logs()

    is_running = False

    def start_batch():
        nonlocal is_running
        if is_running:
            return
        raw_lines = [line.strip() for line in txt_urls.get("1.0", "end").splitlines() if line.strip()]
        if not raw_lines:
            messagebox.showwarning("Trống", "Nhập ít nhất 1 URL Douyin/Bilibili hoặc chọn file video local.")
            return

        current_cfg = sync_vars_to_cfg()
        if current_cfg.ai_enable and not (current_cfg.gemini_api_key or os.environ.get("GEMINI_API_KEY")):
            messagebox.showwarning("Thiếu Gemini API Key", "Bạn đã bật AI Vietsub & Dub nhưng chưa nhập Gemini API Key ở tab 'AI VIETSUB & DUB'.")
            nb.select(tab_ai)
            return

        current_cfg.save()
        is_running = True
        btn_run.configure(state="disabled", text="⏳ ĐANG XỬ LÝ BATCH...")
        status_badge.configure(text=" ● RUNNING ", bg="#78350f", fg="#fbbf24")
        progress["maximum"] = len(raw_lines)
        progress["value"] = 0

        def worker():
            nonlocal is_running
            ok = 0
            for idx, item in enumerate(raw_lines, 1):
                push_log(f"\n--- [{idx}/{len(raw_lines)}] Xử lý: {item} ---")
                try:
                    process_single_item(item, current_cfg, log_fn=push_log)
                    ok += 1
                except Exception as e:
                    push_log(f"[!] LỖI: {e}")
                root.after(0, lambda v=idx: progress.configure(value=v))

            push_log(f"\n=== XONG BATCH: Thành công {ok}/{len(raw_lines)} clip. Thư mục: {current_cfg.output_dir} ===")
            is_running = False
            root.after(0, lambda: btn_run.configure(state="normal", text="🚀 CHẠY BATCH (START FORGE)"))
            root.after(0, lambda: status_badge.configure(text=" ● READY ", bg="#064e3b", fg="#34d399"))

        threading.Thread(target=worker, daemon=True).start()

    btn_run = ttk.Button(btn_bar, text="🚀 CHẠY BATCH (START FORGE)", style="Accent.TButton", command=start_batch)
    btn_run.pack(side="right")

    # --- TAB 2: AI VIETSUB & DUB (GEMINI) ---
    tab_ai.columnconfigure(1, weight=1)
    add_row(tab_ai, 0, "Kích hoạt Auto Nhận Sub + Dịch + Thuyết Minh:", ttk.Checkbutton(tab_ai, variable=v_ai_enable, text="Bật pipeline Gemini ASR -> Dịch Tiếng Việt -> Lồng tiếng TTS"))

    # Ô API Key có nút Hiện/Ẩn chữ để dễ kiểm tra
    api_frame = ttk.Frame(tab_ai)
    ent_api = make_entry(api_frame, v_api_key)
    ent_api.pack(side="left", fill="x", expand=True, padx=(0, 8))
    show_key_state = tk.BooleanVar(value=True)

    def toggle_key_vis():
        if show_key_state.get():
            ent_api.configure(show="*")
            btn_eye.configure(text="👁 Hiện Key")
            show_key_state.set(False)
        else:
            ent_api.configure(show="")
            btn_eye.configure(text="🙈 Ẩn Key")
            show_key_state.set(True)

    btn_eye = ttk.Button(api_frame, text="🙈 Ẩn Key", command=toggle_key_vis)
    btn_eye.pack(side="right")
    add_row(tab_ai, 1, "Gemini API Key (AI Studio):", api_frame)

    cb_model = ttk.Combobox(
        tab_ai,
        textvariable=v_gemini_model,
        values=["gemini-3-flash-preview", "gemini-flash-lite-latest", "gemini-3.1-flash-lite-preview"],
        state="normal",
        font=("Segoe UI", 10),
    )
    add_row(tab_ai, 2, "Gemini Model (tự động đảo server nếu 404/503):", cb_model)
    add_row(tab_ai, 3, "Phong cách dịch (Prompt hướng dẫn):", make_entry(tab_ai, v_trans_style))
    add_row(tab_ai, 4, "Burn phụ đề Tiếng Việt vào video:", ttk.Checkbutton(tab_ai, variable=v_burn_sub, text="Render phụ đề .ASS trực tiếp lên khung hình"))
    add_row(tab_ai, 5, "Đổ hộp nền đen mờ đè Hardsub Trung Quốc:", ttk.Checkbutton(tab_ai, variable=v_cover_cn, text="Bật Opaque Box che kín dòng chữ Trung gốc ở đáy video"))

    sub_pos_frame = ttk.Frame(tab_ai)
    ttk.Label(sub_pos_frame, text="Cỡ chữ (px):").pack(side="left")
    make_entry(sub_pos_frame, v_sub_fontsize, width=10).pack(side="left", padx=(6, 20))
    ttk.Label(sub_pos_frame, text="Cách đáy (MarginV px):").pack(side="left")
    make_entry(sub_pos_frame, v_sub_ymargin, width=10).pack(side="left", padx=6)
    add_row(tab_ai, 6, "Cỡ chữ & Vị trí phụ đề:", sub_pos_frame)

    add_row(tab_ai, 7, "Đọc thuyết minh Tiếng Việt (Auto TTS Dubbing):", ttk.Checkbutton(tab_ai, variable=v_tts_enable, text="Tự động đọc từng câu sub và ghép khớp timestamp"))
    cb_tts_engine = ttk.Combobox(tab_ai, textvariable=v_tts_engine, values=["gemini-tts", "edge-tts", "google-tts"], state="readonly", font=("Segoe UI", 10))
    add_row(tab_ai, 8, "Bộ máy đọc TTS (tự động fallback 3 tầng):", cb_tts_engine)

    cb_voice = ttk.Combobox(
        tab_ai,
        textvariable=v_tts_voice,
        values=[
            "Kore",
            "Puck",
            "Aoede",
            "Charon",
            "Fenrir",
            "vi-VN-HoaiMyNeural",
            "vi-VN-NamMinhNeural",
        ],
        state="normal",
        font=("Segoe UI", 10),
    )
    add_row(tab_ai, 9, "Giọng đọc (Kore/Puck = Gemini, HoaiMy/NamMinh = Edge):", cb_voice)
    add_row(tab_ai, 10, "Tốc độ đọc TTS cộng thêm (vd: +10%, +15%):", make_entry(tab_ai, v_tts_rate))
    add_row(tab_ai, 11, "Âm lượng tiếng gốc khi có thuyết minh (0.0 - 0.5):", make_entry(tab_ai, v_orig_duck))
    add_row(tab_ai, 12, "Xuất kèm file phụ đề rời (.vi.srt):", ttk.Checkbutton(tab_ai, variable=v_export_srt, text="Lưu thêm file .srt cùng tên trong thư mục output"))

    # --- TAB 3: VISUALS ---
    tab_visuals.columnconfigure(1, weight=1)
    add_row(tab_visuals, 0, "Lật gương ngang (Horizontal Flip):", ttk.Checkbutton(tab_visuals, variable=v_hflip, text="Bật (Phụ đề Tiếng Việt vẫn hiển thị xuôi bình thường sau khi lật)"))
    add_row(tab_visuals, 1, "Khung dọc 9:16 + Nền mờ (1080x1920):", ttk.Checkbutton(tab_visuals, variable=v_vert, text="Ép khung Shorts/TikTok kèm nền BoxBlur cho clip ngang Bilibili"))
    add_row(tab_visuals, 2, "Hệ số Zoom & Crop viền (1.01 - 1.25):", make_entry(tab_visuals, v_zoom))
    add_row(tab_visuals, 3, "Dynamic Camera Sway (Lắc tâm ngầm):", ttk.Checkbutton(tab_visuals, variable=v_sway, text="Dịch chuyển khung cắt theo hàm Sin/Cos phá static block hash"))
    add_row(tab_visuals, 4, "Biên độ lắc Camera Sway (px):", make_entry(tab_visuals, v_sway_px))
    add_row(tab_visuals, 5, "Tốc độ phát (Speed 0.90 - 1.25):", make_entry(tab_visuals, v_speed))
    add_row(tab_visuals, 6, "Cắt đầu/cuối clip (giây):", make_entry(tab_visuals, v_trim))

    color_frame = ttk.Frame(tab_visuals)
    ttk.Label(color_frame, text="Gamma:").pack(side="left")
    make_entry(color_frame, v_gamma, width=9).pack(side="left", padx=(6, 16))
    ttk.Label(color_frame, text="Saturation:").pack(side="left")
    make_entry(color_frame, v_sat, width=9).pack(side="left", padx=(6, 16))
    ttk.Label(color_frame, text="Contrast:").pack(side="left")
    make_entry(color_frame, v_contrast, width=9).pack(side="left", padx=6)
    add_row(tab_visuals, 7, "Lệch màu Gamma / Saturation / Contrast:", color_frame)

    add_row(tab_visuals, 8, "Nhiễu hạt Luma Noise (0 - 10):", make_entry(tab_visuals, v_noise))
    add_row(tab_visuals, 9, "Bo tối góc nhẹ (Vignette PI/10):", ttk.Checkbutton(tab_visuals, variable=v_vignette, text="Làm lệch histogram vùng rìa"))
    add_row(tab_visuals, 10, "Che/Xoá Logo góc (FFmpeg Delogo):", ttk.Checkbutton(tab_visuals, variable=v_delogo, text="Bật bộ lọc nội suy xoá watermark cố định"))
    add_row(tab_visuals, 11, "Toạ độ vùng Delogo (x:y:w:h):", make_entry(tab_visuals, v_delogo_rect))
    add_row(tab_visuals, 12, "Watermark chữ kênh riêng (tuỳ chọn):", make_entry(tab_visuals, v_overlay_txt))

    # --- TAB 4: AUDIO & BGM ---
    tab_audio.columnconfigure(1, weight=1)
    add_row(tab_audio, 0, "Pitch Shift âm gốc (Lệch tông 0.90 - 1.15):", make_entry(tab_audio, v_pitch))
    add_row(tab_audio, 1, "Lọc phổ Bandpass (50Hz-15.2kHz) + Micro-Echo:", ttk.Checkbutton(tab_audio, variable=v_eq, text="Bẻ khoá Audio Content ID / Shazam peaks"))
    add_row(tab_audio, 2, "Tắt toàn bộ âm thanh gốc:", ttk.Checkbutton(tab_audio, variable=v_strip_audio, text="Chỉ giữ giọng thuyết minh Tiếng Việt + nhạc nền BGM"))

    bgm_frame = ttk.Frame(tab_audio)
    make_entry(bgm_frame, v_bgm_path).pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Button(
        bgm_frame, text="🎵 Chọn MP3/WAV",
        command=lambda: v_bgm_path.set(filedialog.askopenfilename(filetypes=[("Audio", "*.mp3 *.wav *.m4a *.aac")]) or v_bgm_path.get())
    ).pack(side="right")
    add_row(tab_audio, 3, "Trộn nhạc nền ngầm (Ambient BGM):", bgm_frame)
    add_row(tab_audio, 4, "Âm lượng nhạc nền BGM (0.01 - 0.50):", make_entry(tab_audio, v_bgm_vol))

    # --- TAB 5: CONFIG ---
    tab_config.columnconfigure(1, weight=1)
    out_frame = ttk.Frame(tab_config)
    make_entry(out_frame, v_out_dir).pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Button(
        out_frame, text="📁 Chọn thư mục",
        command=lambda: v_out_dir.set(filedialog.askdirectory() or v_out_dir.get())
    ).pack(side="right")
    add_row(tab_config, 0, "Thư mục xuất video:", out_frame)

    cb_browser = ttk.Combobox(tab_config, textvariable=v_cookie_browser, values=["none", "chrome", "edge", "firefox", "brave"], state="readonly", font=("Segoe UI", 10))
    add_row(tab_config, 1, "Lấy Cookie từ trình duyệt (vượt Douyin):", cb_browser)

    cfile_frame = ttk.Frame(tab_config)
    make_entry(cfile_frame, v_cookie_file).pack(side="left", fill="x", expand=True, padx=(0, 8))
    ttk.Button(
        cfile_frame, text="📄 Chọn cookies.txt",
        command=lambda: v_cookie_file.set(filedialog.askopenfilename(filetypes=[("Text", "*.txt")]) or v_cookie_file.get())
    ).pack(side="right")
    add_row(tab_config, 2, "Hoặc file cookies.txt (Netscape format):", cfile_frame)

    cb_encoder = ttk.Combobox(tab_config, textvariable=v_hw_accel, values=["libx264", "h264_nvenc", "h264_qsv", "h264_amf"], state="readonly", font=("Segoe UI", 10))
    add_row(tab_config, 3, "Bộ mã hoá Video (CPU / GPU):", cb_encoder)
    add_row(tab_config, 4, "Chất lượng CRF (18 nét cao - 24 nhẹ):", make_entry(tab_config, v_crf))
    add_row(tab_config, 5, "Giữ lại video gốc trong ./_work:", ttk.Checkbutton(tab_config, variable=v_keep_raw, text="Không xoá file raw sau khi render"))

    cfg_btn_frame = ttk.Frame(tab_config)
    cfg_btn_frame.grid(row=6, column=0, columnspan=2, sticky="w", pady=20)

    def save_cfg_ui():
        sync_vars_to_cfg().save()
        messagebox.showinfo("Đã lưu", f"Đã lưu cấu hình vào {K_CONFIG_PATH.resolve()}")

    ttk.Button(cfg_btn_frame, text="💾 Lưu Cấu Hình JSON", style="Accent.TButton", command=save_cfg_ui).pack(side="left", padx=(0, 12))

    root.mainloop()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Douyin/Bilibili Anti-Copyright & Gemini Vietsub/Dubbing Video Forge")
    parser.add_argument("inputs", nargs="*", help="URL Douyin/Bilibili, file video local, hoặc file .txt chứa danh sách link")
    parser.add_argument("-o", "--out", help="Đường dẫn file xuất (khi chạy 1 video ở chế độ CLI)")
    parser.add_argument("--cli", action="store_true", help="Ép chạy chế độ dòng lệnh (không mở GUI)")
    parser.add_argument("--vietsub", action="store_true", help="Bật Auto nhận sub + dịch tiếng Việt + đọc thuyết minh qua Gemini")
    parser.add_argument("--api-key", help="Gemini API Key (hoặc đặt biến môi trường GEMINI_API_KEY)")
    parser.add_argument("--voice", help="Giọng đọc TTS (Kore/Puck/Aoede hoặc vi-VN-HoaiMyNeural)")
    parser.add_argument("--tts-engine", choices=["gemini-tts", "edge-tts", "google-tts"], help="Chọn engine đọc tiếng Việt")
    parser.add_argument("--no-dub", action="store_true", help="Chỉ dịch và burn phụ đề tiếng Việt, không đọc thuyết minh")
    parser.add_argument("--no-flip", action="store_true", help="Tắt lật gương ngang")
    parser.add_argument("--no-vertical", action="store_true", help="Giữ nguyên tỉ lệ khung hình gốc (không ép 9:16)")
    parser.add_argument("--speed", type=float, help="Tốc độ phát (mặc định 1.04)")
    parser.add_argument("--pitch", type=float, help="Dịch cao độ âm thanh (mặc định 1.03)")
    parser.add_argument("--zoom", type=float, help="Hệ số phóng cắt viền (mặc định 1.06)")
    parser.add_argument("--bgm", help="Đường dẫn nhạc nền trộn ngầm")
    parser.add_argument("--cookies-from-browser", choices=["none", "chrome", "edge", "firefox", "brave"], help="Trích xuất cookie trình duyệt cho Douyin/Bilibili")
    parser.add_argument("--cookies", help="Đường dẫn file cookies.txt")
    parser.add_argument("--encoder", choices=["libx264", "h264_nvenc", "h264_qsv", "h264_amf"], help="Chọn encoder CPU hoặc GPU")
    parser.add_argument("--selftest", action="store_true", help="Tự tạo video mẫu có giọng Trung + test burn ASS & trộn thuyết minh tiếng Việt")
    args = parser.parse_args()

    cfg = ForgeConfig.load()

    if args.vietsub:
        cfg.ai_enable = True
    if args.api_key:
        cfg.gemini_api_key = args.api_key
    if args.voice:
        cfg.tts_voice = args.voice
    if args.tts_engine:
        cfg.tts_engine = args.tts_engine
    if args.no_dub:
        cfg.tts_enable = False
    if args.no_flip:
        cfg.hflip = False
    if args.no_vertical:
        cfg.vertical_9_16 = False
    if args.speed:
        cfg.speed_Bar = args.speed
    if args.pitch:
        cfg.pitch_shift = args.pitch
    if args.zoom:
        cfg.zoom_crop = args.zoom
    if args.bgm:
        cfg.bgm_path = args.bgm
    if args.cookies_from_browser:
        cfg.cookies_browser = args.cookies_from_browser
    if args.cookies:
        cfg.cookies_file = args.cookies
    if args.encoder:
        cfg.hw_accel = args.encoder

    if args.selftest:
        K_WORK_DIR.mkdir(parents=True, exist_ok=True)
        sample_in = K_WORK_DIR / "selftest_in.mp4"
        sample_out = Path(cfg.output_dir) / "selftest_vietsub_out.mp4"
        print("[*] Đang tạo video mẫu 1280x720 kèm giọng tiếng Trung...")
        generate_selftest_sample(sample_in)

        # Kiểm định luôn cả bộ tạo ASS + Edge-TTS Tiếng Việt + FFmpeg Burn & Dub Mix
        mock_segments = [
            {"start": 0.1, "end": 1.6, "origin": "大家好，欢迎来到我的频道", "vi": "Chào mọi người, chào mừng đến với kênh của mình!"},
            {"start": 1.7, "end": 3.2, "origin": "今天给大家分享一个实用工具", "vi": "Hôm nay mình chia sẻ một công cụ làm video cực đỉnh."},
        ]
        ass_test = K_WORK_DIR / "selftest.ass"
        write_ass_subtitle(mock_segments, ass_test, cfg, cfg.target_w, cfg.target_h)
        dub_test = build_vietnamese_dub_track(mock_segments, 3.3, cfg)
        info = probe_media(sample_in)
        cmd = build_ffmpeg_command(sample_in, sample_out, cfg, info, ass_path=ass_test, dub_wav_path=dub_test)
        subprocess.run(cmd, check=True)
        ass_test.unlink(missing_ok=True)
        if dub_test:
            dub_test.unlink(missing_ok=True)
        print(f"[+] Selftest ASS Vietsub + Neural VN Dubbing hoàn tất: {sample_out.resolve()}")
        return 0

    if not args.inputs and not args.cli:
        return launch_gui(cfg)

    targets: list[str] = []
    for item in args.inputs:
        p = Path(item)
        if p.exists() and p.suffix.lower() == ".txt":
            targets.extend([line.strip() for line in p.read_text(encoding="utf-8").splitlines() if line.strip() and not line.startswith("#")])
        else:
            targets.append(item)

    if not targets:
        parser.print_help()
        return 1

    for idx, t in enumerate(targets, 1):
        print(f"\n[{idx}/{len(targets)}] Nguồn: {t}")
        out_path = Path(args.out) if (args.out and len(targets) == 1) else None
        process_single_item(t, cfg, out_file=out_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
