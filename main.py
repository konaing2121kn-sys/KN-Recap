"""
Burmese AI dubbing web app (backend).

Pipeline:
  upload video -> ffmpeg extract audio -> Whisper (speech-to-text)
  -> translate to Burmese -> edge-tts (Burmese voice)
  -> mix dubbed audio, burn Burmese subtitles -> MP4
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# ---------------- settings (environment variables) ----------------
WORK_DIR = Path(os.getenv("WORK_DIR", "jobs"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "300"))
ACCESS_CODE = os.getenv("ACCESS_CODE", "")  # optional password
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "base")  # tiny/base/small
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
TTS_VOICE = os.getenv("TTS_VOICE", "my-MM-NilarNeural")  # or my-MM-ThihaNeural
SUB_FONT = os.getenv("SUB_FONT", "Noto Sans Myanmar")
BG_VOLUME = float(os.getenv("BG_VOLUME", "0"))  # 0 = remove original audio
MAX_SPEEDUP = float(os.getenv("MAX_SPEEDUP", "1.5"))

WORK_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="Burmese AI Dubbing")
JOBS: dict = {}
_whisper = None


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
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def check_code(code):
    if ACCESS_CODE and code != ACCESS_CODE:
        raise HTTPException(401, "Access code မှားနေပါတယ်")


# ---------------- pipeline steps ----------------
def transcribe(audio_path):
    global _whisper
    from faster_whisper import WhisperModel
    if _whisper is None:
        _whisper = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    segs, _info = _whisper.transcribe(str(audio_path), vad_filter=True)
    return [{"start": s.start, "end": s.end, "text": s.text.strip()}
            for s in segs if s.text.strip()]


def translate_claude(texts):
    import anthropic
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    out = []
    for i in range(0, len(texts), 40):
        batch = texts[i:i + 40]
        prompt = (
            "Translate each line below into natural, spoken Burmese (Myanmar language) "
            "suitable for movie narration. Keep each translation concise. "
            "Return ONLY a JSON array of strings with exactly "
            f"{len(batch)} items, in the same order.\n\n"
            + json.dumps(batch, ensure_ascii=False)
        )
        msg = client.messages.create(
            model=CLAUDE_MODEL, max_tokens=4000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = msg.content[0].text
        m = re.search(r"\[.*\]", text, re.S)
        arr = json.loads(m.group(0)) if m else []
        if len(arr) != len(batch):
            raise RuntimeError("Translation count mismatch")
        out.extend(str(x) for x in arr)
    return out


def translate_free(texts):
    from deep_translator import GoogleTranslator
    tr = GoogleTranslator(source="auto", target="my")
    return [tr.translate(t) or t for t in texts]


def translate(texts):
    if ANTHROPIC_API_KEY:
        return translate_claude(texts)
    return translate_free(texts)


async def _tts_all(items, voice):
    import edge_tts
    for text, path in items:
        await edge_tts.Communicate(text, voice).save(str(path))


def fit_audio(src: Path, dst: Path, slot: float):
    """Speed up TTS if longer than its time slot (limited), output 44.1k mono wav."""
    dur = duration_of(src)
    factor = 1.0
    if slot > 0.3 and dur > slot:
        factor = min(dur / slot, MAX_SPEEDUP)
    af = "aformat=sample_rates=44100:channel_layouts=mono"
    if factor > 1.01:
        af = f"atempo={factor:.3f}," + af
    run(["ffmpeg", "-y", "-i", str(src), "-af", af, str(dst)])


def build_dub(segments, tts_files, total, jobdir: Path, orig_audio: Path):
    cmd = ["ffmpeg", "-y", "-f", "lavfi", "-t", f"{total:.2f}",
           "-i", "anullsrc=r=44100:cl=mono"]
    filters = []
    labels = ["[0:a]"]
    idx = 1
    if BG_VOLUME > 0:
        cmd += ["-i", str(orig_audio)]
        filters.append(f"[{idx}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                       f"volume={BG_VOLUME}[bg]")
        labels.append("[bg]")
        idx += 1
    for seg, f in zip(segments, tts_files):
        cmd += ["-i", str(f)]
        d = int(seg["start"] * 1000)
        filters.append(f"[{idx}:a]adelay={d}:all=1[a{idx}]")
        labels.append(f"[a{idx}]")
        idx += 1
    filters.append("".join(labels) +
                   f"amix=inputs={len(labels)}:normalize=0:duration=first[out]")
    out = jobdir / "dub.wav"
    cmd += ["-filter_complex", ";".join(filters), "-map", "[out]", str(out)]
    run(cmd)
    return out


def process(job_id: str):
    job = JOBS[job_id]
    d = WORK_DIR / job_id
    try:
        video = d / "input.mp4"
        job["step"] = "အသံထုတ်နေပါတယ်"
        audio = d / "audio.wav"
        run(["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", str(audio)])
        total = duration_of(video)

        job["step"] = "အသံကို စာအဖြစ်ပြောင်းနေပါတယ် (Whisper)"
        segments = transcribe(audio)
        if not segments:
            raise RuntimeError("စကားပြောသံ မတွေ့ပါ")

        job["step"] = "မြန်မာဘာသာ ပြန်နေပါတယ်"
        mm = translate([s["text"] for s in segments])
        for s, t in zip(segments, mm):
            s["mm"] = t

        job["step"] = "မြန်မာအသံ ဖန်တီးနေပါတယ်"
        items = [(s["mm"], d / f"tts_{i}.mp3") for i, s in enumerate(segments)]
        asyncio.run(_tts_all(items, TTS_VOICE))

        job["step"] = "အသံအချိန် ညှိနေပါတယ်"
        fitted = []
        for i, s in enumerate(segments):
            nxt = segments[i + 1]["start"] if i + 1 < len(segments) else total
            slot = max(s["end"], min(nxt, s["start"] + (s["end"] - s["start"]) * 1.4)) - s["start"]
            out = d / f"fit_{i}.wav"
            fit_audio(items[i][1], out, slot)
            fitted.append(out)

        job["step"] = "အသံ ပေါင်းစပ်နေပါတယ်"
        dub = build_dub(segments, fitted, total, d, audio)

        srt = d / "sub.srt"
        with open(srt, "w", encoding="utf-8") as f:
            for i, s in enumerate(segments, 1):
                f.write(f"{i}\n{srt_time(s['start'])} --> {srt_time(s['end'])}\n{s['mm']}\n\n")

        job["step"] = "စာတန်းထိုးပြီး video ထုတ်နေပါတယ် (အချိန်ကြာနိုင်ပါတယ်)"
        style = f"FontName={SUB_FONT},FontSize=18,Outline=2,MarginV=25"
        run(["ffmpeg", "-y", "-i", "input.mp4", "-i", "dub.wav",
             "-vf", f"subtitles=sub.srt:force_style='{style}'",
             "-map", "0:v:0", "-map", "1:a:0",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "23",
             "-c:a", "aac", "-shortest", "result.mp4"], cwd=str(d))

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
    size = 0
    with open(d / "input.mp4", "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                f.close()
                shutil.rmtree(d, ignore_errors=True)
                raise HTTPException(413, f"ဖိုင်က {MAX_UPLOAD_MB}MB ထက် ကြီးနေပါတယ်")
            f.write(chunk)
    JOBS[job_id] = {"status": "running", "step": "စတင်နေပါတယ်", "error": ""}
    background.add_task(process, job_id)
    return {"id": job_id}


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
    return FileResponse(p, media_type="video/mp4", filename="myanmar_dub.mp4")


@app.get("/api/jobs/{job_id}/srt")
def job_srt(job_id: str):
    p = WORK_DIR / job_id / "sub.srt"
    if job_id not in JOBS or not p.exists():
        raise HTTPException(404, "မပြီးသေးပါ")
    return FileResponse(p, media_type="text/plain", filename="myanmar.srt")


app.mount("/", StaticFiles(directory="static", html=True), name="static")
