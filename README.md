# Burmese AI Dubbing (web app)

Video upload → English (etc.) speech to text → Burmese translation → Burmese voice → subtitles burned in → MP4.

## Files
- `main.py` – backend (FastAPI)
- `static/index.html` – phone-friendly web page
- `requirements.txt`, `Dockerfile` – deploy

## Environment variables
| Name | Meaning | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Better Burmese translation (Claude). If empty, free Google Translate is used | empty |
| `ACCESS_CODE` | Password so strangers can't use your server | empty (no password) |
| `WHISPER_MODEL` | `tiny` / `base` / `small` (bigger = slower, more accurate) | `base` |
| `TTS_VOICE` | `my-MM-NilarNeural` (female) or `my-MM-ThihaNeural` (male) | Nilar |
| `BG_VOLUME` | Keep original audio quietly, e.g. `0.1`. `0` = remove | `0` |
| `MAX_UPLOAD_MB` | Upload limit | `300` |

## Deploy from a phone (Render / Railway)
1. Create a GitHub account, make a new repository, upload all these files (keep the `static` folder).
2. On Render.com (or Railway.app): New → Web Service → connect the repository → it detects the `Dockerfile`.
3. Add the environment variables above (at least `ACCESS_CODE`).
4. Open the URL it gives you in your phone browser.

Note: free tiers have little RAM/CPU, so use `WHISPER_MODEL=tiny` or `base` and short videos (1–3 minutes) first. Files in `jobs/` are lost when the server restarts, so download your result right away.

## Run on a computer (optional)
```
pip install -r requirements.txt
# ffmpeg must be installed
uvicorn main:app --port 8000
```
