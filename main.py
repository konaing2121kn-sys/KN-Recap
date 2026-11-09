"""
Burmese voice-over web app (backend).

Flow:
  1. Upload video -> Whisper transcript -> Burmese translation (text for you to read)
  2. You record your own voice, upload the audio
  3. Original audio is removed, your voice is added, video length is matched
     to your audio, Burmese subtitles are optionally burned in -> MP4
"""
import json
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional

from fastapi import BackgroundTasks, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------- settings (environment variables) ----------------
WORK_DIR = Path(os.getenv("WORK_DIR", "jobs"))
MAX_VIDEO_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
MAX_AUDIO_MB = int(os.getenv("MAX_AUDIO_MB", "100"))
ACCESS_CODE = os.getenv("ACCESS_CODE", "")  # optional password
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")  # tiny/base/small
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
SUB_FONT = os.getenv("SUB_FONT", "Noto Sans Myanmar")
# video is slowed down / sped up only inside this range to match your audio
STRETCH_MIN = float(os.getenv("STRETCH_MIN", "0.7"))
STRETCH_MAX = float(os.getenv("STRETCH_MAX", "1.4"))

WORK_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Burmese Voice-over")
JOBS: dict = {}
_whisper = None
ID_RE = re.compile(r"^[0-9a-f]{12}$")


# ---------------- helpers ----------------
def run(cmd, cwd=None):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed: {p.stderr[-600:]}")
    return p.stdout


def duration_of(path) -> float:
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", str(path)])
    return float(out.strip())


def srt_time(t: float) -> str:
    ms = int(round(max(t, 0) * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def check_code(code):
    if ACCESS_CODE and code != ACCESS_CODE:
        raise HTTPException(401, "Access code မှားနေပါတယ်")


async def save_upload(up: UploadFile, path: Path, limit_mb: int):
    size = 0
    try:
        with open(path, "wb") as f:
            while chunk := await up.read(1024 * 1024):
                size += len(chunk)
                if size > limit_mb * 1024 * 1024:
                    raise HTTPException(413, f"ဖိုင်က {limit_mb}MB ထက် ကြီးနေပါတယ်")
                f.write(chunk)
    except Exception:
        path.unlink(missing_ok=True)
        raise


# ---------------- transcript + translation ----------------
def transcribe(audio_path):
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segs, _info = _whisper.transcribe(str(audio_path), vad_filter=True)
    return [{"start": s.start, "end": s.end, "en": s.text.strip()}
            for s in segs if s.text.strip()]


def translate_claude(texts):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    out = []
    for i in range(0, len(texts), 40):
        batch = texts[i:i + 40]
        prompt = (
            "Translate each line below into natural, spoken Burmese (Myanmar language) "
            "that is easy to read aloud for movie narration. "
            "Return ONLY a JSON array of strings with exactly "
            f"{len(batch)} items, in the same order.\n\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        m = re.search(r"\[.*\]", msg.content[0].text, re.S)
        arr = json.loads(m.group(0)) if m else []
        if len(arr) != len(batch):
            raise RuntimeError("Translation count mismatch")
        out.extend(str(x) for x in arr)
    return out


def translate_free(texts):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target="my")
    out = []
    for t in texts:
        for attempt in range(6):
            try:
                out.append(tr.translate(t) or t)
                break
            except Exception:  # noqa: BLE001  (rate limit etc.)
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError("Free translator is busy. Wait a few minutes and retry.")
        time.sleep(0.5)
    return out


def translate(texts):
    return translate_claude(texts) if ANTHROPIC_API_KEY else translate_free(texts)


def transcribe_job(job_id: str):
    job = JOBS[job_id]
    d = WORK_DIR / job_id
    try:
        job["step"] = "အသံထုတ်နေပါတယ်"
        audio = d / "audio.wav"
        run(["ffmpeg", "-y", "-i", str(d / "input.mp4"), "-vn", "-ac", "1",
             "-ar", "16000", str(audio)])
        job["step"] = "အသံကို စာအဖြစ်ပြောင်းနေပါတယ် (အချိန်ကြာနိုင်ပါတယ်)"
        segments = transcribe(audio)
        if not segments:
            raise RuntimeError("စကားပြောသံ မတွေ့ပါ")
        job["step"] = "မြန်မာဘာသာ ပြန်နေပါတယ်"
        mm = translate([s["en"] for s in segments])
        for s, t in zip(segments, mm):
            s["mm"] = t
        job["segments"] = segments
        job["status"], job["step"] = "ready", "ပြီးပါပြီ"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "error", str(e)


# ---------------- merge ----------------
def merge_job(job_id: str, segments: list, subs: bool):
    job = JOBS[job_id]
    d = WORK_DIR / job_id
    try:
        job["step"] = "အသံနဲ့ video ကြာချိန် ညှိနေပါတယ်"
        vd = duration_of(d / "input.mp4")
        ad = duration_of(d / "voice_in")
        ratio = ad / vd
        vf, scale, note, trim_only = [], 1.0, "ကြာချိန် တူပါတယ်", False
        if abs(ratio - 1) < 0.01:
            trim_only = True
        elif STRETCH_MIN <= ratio <= STRETCH_MAX:
            vf.append(f"setpts={ratio:.6f}*PTS")
            scale = ratio
            note = f"Video ကို {1 / ratio:.2f}x မြန်နှုန်းနဲ့ ချိန်ထားပါတယ်"
        elif ad > vd:
            vf.append(f"tpad=stop_mode=clone:stop_duration={ad - vd:.3f}")
            note = "အသံက ပိုရှည်လို့ video နောက်ဆုံးပုံကို ရပ်ထားပါတယ်"
        else:
            trim_only = True
            note = "အသံက ပိုတိုလို့ video ကို ဖြတ်ထားပါတယ်"

        use_subs = subs and bool(segments)
        if use_subs:
            with open(d / "sub.srt", "w", encoding="utf-8") as f:
                for i, s in enumerate(segments, 1):
                    st = float(s["start"]) * scale
                    en = min(float(s["end"]) * scale, ad)
                    if st >= ad:
                        continue
                    f.write(f"{i}\n{srt_time(st)} --> {srt_time(en)}\n{s['mm']}\n\n")
            style = f"FontName={SUB_FONT},FontSize=18,Outline=2,MarginV=25"
            vf.append(f"subtitles=sub.srt:force_style='{style}'")

        job["step"] = "Video ထုတ်နေပါတယ် (အချိန်ကြာနိုင်ပါတယ်)"
        cmd = ["ffmpeg", "-y", "-i", "input.mp4", "-i", "voice_in"]
        if vf:
            cmd += ["-vf", ",".join(vf), "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23"]
        else:
            cmd += ["-c:v", "copy"] if trim_only else ["-c:v", "libx264"]
        cmd += ["-map", "0:v:0", "-map", "1:a:0", "-c:a", "aac", "-b:a", "128k",
                "-t", f"{ad:.3f}", "result.mp4"]
        run(cmd, cwd=str(d))
        job["note"] = note
        job["status"], job["step"] = "done", "ပြီးပါပြီ"
    except Exception as e:  # noqa: BLE001
        job["status"], job["error"] = "error", str(e)


# ---------------- API ----------------
@app.post("/api/jobs")
async def create_job(background: BackgroundTasks, file: UploadFile = File(...),
                     x_access_code: str = Header(default="")):
    check_code(x_access_code)
    job_id = uuid.uuid4().hex[:12]
    d = WORK_DIR / job_id
    d.mkdir(parents=True)
    try:
        await save_upload(file, d / "input.mp4", MAX_VIDEO_MB)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    JOBS[job_id] = {"status": "running", "step": "စတင်နေပါတယ်", "error": ""}
    background.add_task(transcribe_job, job_id)
    return {"id": job_id}


@app.post("/api/merge")
async def create_merge(
    background: BackgroundTasks,
    audio: UploadFile = File(...),
    video: Optional[UploadFile] = File(None),
    job_id: str = Form(""),
    segments: str = Form("[]"),
    subtitles: str = Form("1"),
    x_access_code: str = Header(default=""),
):
    check_code(x_access_code)
    try:
        segs = json.loads(segments)
        if not isinstance(segs, list):
            raise ValueError
    except ValueError:
        raise HTTPException(400, "segments မမှန်ပါ")
    new_id = uuid.uuid4().hex[:12]
    d = WORK_DIR / new_id
    d.mkdir(parents=True)
    try:
        if video is not None and video.filename:
            await save_upload(video, d / "input.mp4", MAX_VIDEO_MB)
        else:
            src = WORK_DIR / job_id / "input.mp4" if ID_RE.match(job_id) else None
            if not src or not src.exists():
                raise HTTPException(410, "video_missing")
            shutil.copy(src, d / "input.mp4")
        await save_upload(audio, d / "voice_in", MAX_AUDIO_MB)
    except Exception:
        shutil.rmtree(d, ignore_errors=True)
        raise
    JOBS[new_id] = {"status": "running", "step": "စတင်နေပါတယ်", "error": "", "note": ""}
    background.add_task(merge_job, new_id, segs, subtitles == "1")
    return {"id": new_id}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "မတွေ့ပါ")
    return JOBS[job_id]


@app.get("/api/jobs/{job_id}/result")
def job_result(job_id: str):
    p = WORK_DIR / job_id / "result.mp4"
    if job_id not in JOBS or not p.exists():
        raise HTTPException(404, "မပြီးသေးပါ")
    return FileResponse(p, media_type="video/mp4", filename="myanmar_voiceover.mp4")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
