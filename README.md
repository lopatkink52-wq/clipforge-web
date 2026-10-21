# ClipForge — mobile-first Shorts bot (MVP v0.3)

ClipForge is a starter project for turning long videos into vertical short clips. You control it in Telegram on your phone; actual processing runs on a server.

## Included
- Telegram bot with `/start`, `/help`, `/settings` and inline clip settings.
- Video upload or supported public video URL.
- Speech transcription with faster-whisper (tiny model by default to reduce memory use).
- Heuristic candidate ranking; this is not a prediction of virality.
- FFmpeg vertical 9:16 MP4 exports and word-group subtitles.
- Webhook mode for web hosts such as Render, with polling mode available when `WEBHOOK_URL` is blank.
- Docker image with FFmpeg installed.
- Mobile UI concept in `mobile_ui_prototype.html`; it is a design prototype, not a processing interface.

## Deploy from a phone (Render, experimental free tier)
1. Create a Telegram bot with the official `@BotFather`. Copy its token privately; never post it in a chat or public repository.
2. Upload these project files to a GitHub repository. Keep it private if possible. Do not upload `.env`, tokens, source videos, or generated clips.
3. Open Render and create a new Blueprint from that GitHub repository, using `render.yaml`. If you prefer a manual service, create a **Web Service** with Docker runtime and the included `Dockerfile`.
4. In Render's Environment settings, add `BOT_TOKEN` with the token from BotFather. The code automatically uses Render's `RENDER_EXTERNAL_URL` for the webhook. Keep `WEBHOOK_SECRET` private; the Blueprint generates it.
5. Deploy and watch the logs. When it starts successfully, open your bot in Telegram and send `/start`.

## Free-tier limitations — read before relying on it
- This is a best-effort experiment, not guaranteed 24/7 hosting. Render free web services sleep after 15 minutes without inbound traffic and may restart; the first message after sleep may be delayed. See Render's current free-service docs.
- The free instance has limited RAM/CPU. `WHISPER_MODEL=tiny` is configured to reduce memory use, but long videos can still fail or run very slowly. A paid worker or stronger host may be needed for dependable processing.
- Telegram's hosted Bot API limits downloads through `get_file` to 20 MB. Larger uploads are rejected; URL input may work if the source site permits downloading and the file stays under `MAX_URL_MB`.
- Temporary files are stored on ephemeral disk and removed after each job. They are not durable storage.
- Do not use this to bypass DRM, logins, or access controls. Only process content you have permission to use.

## Environment variables
- `BOT_TOKEN` — required Telegram bot token.
- `WEBHOOK_URL` — optional public HTTPS service URL. On Render, `RENDER_EXTERNAL_URL` is detected automatically. If blank on other hosts, bot uses polling.
- `WEBHOOK_PATH` and `WEBHOOK_SECRET` — webhook path and Telegram secret token.
- `WHISPER_MODEL` — `tiny` by default; larger models can improve transcription but need more RAM and CPU.
- `MAX_UPLOAD_MB` — 20 MB default for cloud Bot API downloads.
- `MAX_URL_MB` — maximum video download from URL.

## Local/container run
```bash
docker build -t clipforge .
docker run --rm -e BOT_TOKEN='YOUR_TOKEN' clipforge
```

## Known limitations / next engineering tasks
- Ranking is currently heuristic, not an AI semantic judge.
- Cropping is a center crop, not face/speaker tracking.
- There is no browser uploader, persistent queue, durable storage, user quota system, or production abuse protection.
- End-to-end video processing has not yet been verified on a live host. The included smoke tests only validate small helper functions.

## Website version (ClipForge Web)

A mobile-first web interface and FastAPI backend are included in `web_app.py`. It accepts an uploaded video or a public video URL, queues one processing job, runs the existing transcription/clip pipeline, and returns a ZIP of vertical MP4 clips.

### Run locally

```bash
pip install -r requirements-web.txt
uvicorn web_app:app --host 0.0.0.0 --port 8000
```

Open `http://127.0.0.1:8000`. FFmpeg and ffprobe must be installed. First processing may download the Whisper model. For Render, create a Web Service from the repository and use `Dockerfile.web`, or use the included `render-web.yaml` as a starting point. This has not yet been deployed or end-to-end tested on a hosted service.

### MVP limitations

- Free Render instances sleep after idle time and use ephemeral local storage; jobs and files can disappear after restart/sleep/redeploy.
- This MVP processes one job at a time and limits uploaded files to 150 MB by default. Hosting limits can vary.
- Candidate selection is heuristic based on transcript features, not a large language model judging full semantic context.
- Video framing uses a center crop; no face tracking yet.
- Public video URL support depends on the source platform, its access rules, and downloader compatibility.
- Do not use this to download content you do not have rights to use.
