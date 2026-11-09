# Burmese Voice-over (web app)

1. Upload video -> transcript + Burmese translation (text to read)
2. Record your own voice, upload the audio
3. Original audio removed, your voice added, video length matched, subtitles optional -> MP4

## Files
- `main.py` – backend (FastAPI)
- `static/index.html` – phone-friendly web page
- `requirements.txt`, `Dockerfile` – deploy (Dockerfile is unchanged)

## Environment variables
| Name | Meaning | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Better Burmese translation (Claude). Empty = free Google Translate (often rate-limited on shared hosts) | empty |
| `ACCESS_CODE` | Password so strangers can't use your server | empty |
| `WHISPER_MODEL` | `tiny` / `base` / `small` | `base` |
| `MAX_UPLOAD_MB` | Video upload limit | `300` |
| `STRETCH_MIN` / `STRETCH_MAX` | Video is slowed/sped up only inside this ratio range to match your audio; outside it the video is trimmed or the last frame is held | `0.7` / `1.4` |

## Notes
- Free hosts sleep and lose files after idle time. If the server was asleep between step 1 and step 3, the page re-sends the video automatically (keep the page open).
- Use short videos (1-3 min) on free tiers.
