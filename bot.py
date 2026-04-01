#!/usr/bin/env python3

import os
import re
import logging
import asyncio
from pathlib import Path
from urllib.parse import quote

import requests
from requests.auth import HTTPBasicAuth
import yt_dlp
from telegram import Update
from telegram.ext import (
    Application,
    MessageHandler,
    CommandHandler,
    filters,
    ContextTypes,
)

BOT_TOKEN = os.environ["BOT_TOKEN"]
ALLOWED_USER_ID = int(os.environ["ALLOWED_USER_ID"])
MAILRU_LOGIN = os.environ["MAILRU_LOGIN"]
MAILRU_APP_PASSWORD = os.environ["MAILRU_APP_PASSWORD"]
MAILRU_FOLDER = os.environ.get("MAILRU_FOLDER", "/YouTube")
TELEGRAM_LIMIT_MB = int(os.environ.get("TELEGRAM_LIMIT_MB", "50"))

WEBDAV_BASE = "https://webdav.cloud.mail.ru"
DOWNLOAD_DIR = Path("/tmp/yt_bot_downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

YOUTUBE_RE = re.compile(
    r"(https?://)?(www\.)?"
    r"(youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)"
    r"[\w\-]+"
)


def is_youtube_url(text):
    return bool(YOUTUBE_RE.search(text))

def extract_url(text):
    match = YOUTUBE_RE.search(text)
    return match.group(0) if match else text

def file_size_mb(path):
    return os.path.getsize(path) / (1024 * 1024)

def mailru_auth():
    return HTTPBasicAuth(MAILRU_LOGIN, MAILRU_APP_PASSWORD)

def mailru_ensure_folder():
    url = f"{WEBDAV_BASE}{quote(MAILRU_FOLDER)}"
    requests.request("MKCOL", url, auth=mailru_auth(), timeout=30)

def upload_to_mailru(filepath, filename):
    try:
        mailru_ensure_folder()
        remote_path = f"{MAILRU_FOLDER}/{filename}"
        url = f"{WEBDAV_BASE}{quote(remote_path)}"
        log.info(f"Загружаю в облако: {remote_path}")
        with open(filepath, "rb") as f:
            resp = requests.put(url, data=f, auth=mailru_auth(), timeout=1200)
        if resp.status_code in (200, 201, 204):
            log.info(f"Загружено: {remote_path}")
            return True
        log.error(f"Ошибка: {resp.status_code}")
        return False
    except Exception as e:
        log.error(f"Ошибка Cloud Mail.ru: {e}")
        return False

def download_video(url, quality, suffix):
    output_template = str(DOWNLOAD_DIR / f"%(title).50s_{suffix}.%(ext)s")
    ydl_opts = {
        "format": quality,
        "outtmpl": output_template,
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            title = info.get("title", "video")
            filename = ydl.prepare_filename(info)
            mp4_path = Path(filename).with_suffix(".mp4")
            if mp4_path.exists():
                return str(mp4_path), title
            if Path(filename).exists():
                return filename, title
            for f in DOWNLOAD_DIR.glob(f"*_{suffix}.*"):
                return str(f), title
    except Exception as e:
        log.error(f"Ошибка скачивания ({suffix}): {e}")
    return None, ""

async def cmd_start(update, context):
    await update.message.reply_text("👋 Отправь ссылку на YouTube видео.\nСкачаю в 1080p и 360p.")

async def cmd_myid(update, context):
    await update.message.reply_text(f"Твой ID: {update.effective_user.id}")

async def handle_message(update, context):
    text = update.message.text or ""
    if ALLOWED_USER_ID and update.effective_user.id != ALLOWED_USER_ID:
        return
    if not is_youtube_url(text):
        await update.message.reply_text("Отправь ссылку на YouTube видео.")
        return

    url = extract_url(text)
    status = await update.message.reply_text("⏳ Скачиваю...")
    loop = asyncio.get_event_loop()

    task_1080 = loop.run_in_executor(None, download_video, url, "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "1080p")
    task_360 = loop.run_in_executor(None, download_video, url, "bestvideo[height<=360]+bestaudio/best[height<=360]", "360p")

    (file_1080, title), (file_360, _) = await asyncio.gather(task_1080, task_360)
    await status.edit_text(f"📤 {title}")

    for filepath, label in [(file_1080, "1080p"), (file_360, "360p")]:
        if not filepath or not os.path.exists(filepath):
            await update.message.reply_text(f"❌ {label} — не удалось скачать.")
            continue
        size = file_size_mb(filepath)
        filename = os.path.basename(filepath)
        if size <= TELEGRAM_LIMIT_MB:
            try:
                await update.message.reply_video(
                    video=open(filepath, "rb"),
                    caption=f"📹 {label} • {size:.0f} МБ",
                    read_timeout=300,
                    write_timeout=300,
                )
            except Exception as e:
                await update.message.reply_text(f"❌ {label}: {e}")
        else:
            await status.edit_text(f"☁️ {label} ({size:.0f} МБ) → облако...")
            ok = await loop.run_in_executor(None, upload_to_mailru, filepath, filename)
            if ok:
                await update.message.reply_text(f"☁️ {label} • {size:.0f} МБ\nЗагружен → cloud.mail.ru/home{MAILRU_FOLDER}")
            else:
                await update.message.reply_text(f"❌ {label} — ошибка загрузки в облако.")
        try:
            os.remove(filepath)
        except OSError:
            pass

    try:
        await status.delete()
    except Exception:
        pass

def main():
    try:
        resp = requests.request("PROPFIND", WEBDAV_BASE, auth=mailru_auth(), headers={"Depth": "0"}, timeout=10)
        if resp.status_code in (207, 200):
            log.info("✅ Cloud Mail.ru — ок!")
        else:
            log.warning(f"⚠️ Cloud Mail.ru: {resp.status_code}")
    except Exception as e:
        log.warning(f"⚠️ Cloud Mail.ru: {e}")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("myid", cmd_myid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    log.info("Бот запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
