import asyncio, logging, os, re, shutil, tempfile
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from pipeline import make_clips
from core import is_http_url

load_dotenv()
TOKEN = os.getenv('BOT_TOKEN', '').strip()
# Telegram Bot API cloud downloads are limited; configure a local Bot API server for larger files.
MAX_UPLOAD_MB = int(os.getenv('MAX_UPLOAD_MB', '20'))
MAX_URL_MB = int(os.getenv('MAX_URL_MB', '2048'))
MAX_CLIPS = int(os.getenv('MAX_CLIPS', '8'))
WEBHOOK_URL = (os.getenv('WEBHOOK_URL') or os.getenv('RENDER_EXTERNAL_URL') or '').strip().rstrip('/')
WEBHOOK_PATH = os.getenv('WEBHOOK_PATH', 'clipforge-webhook').strip('/')
WEBHOOK_SECRET = os.getenv('WEBHOOK_SECRET', '').strip()
PORT = int(os.getenv('PORT', '10000'))
USER_SETTINGS = {}
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('clipforge')

WELCOME = (
    '🎬 *ClipForge*\n\n'
    'Превращаю длинные видео в вертикальные Shorts.\n\n'
    'Пришли видео файлом или ссылку на доступное видео. Затем выберешь длительность и число клипов.\n\n'
    'Пока это MVP: оценки моментов помогают сортировать фрагменты, но не гарантируют просмотры.'
)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [[InlineKeyboardButton('⚙️ Настройки нарезки', callback_data='settings')],
                [InlineKeyboardButton('❔ Как это работает', callback_data='help')]]
    await update.message.reply_text(WELCOME, parse_mode='Markdown', reply_markup=InlineKeyboardMarkup(keyboard))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text('1. Отправь видео или ссылку.\n2. Выбери параметры.\n3. Дождись анализа и скачай клипы.\n\nИспользуй только видео, на обработку которых у тебя есть права. Время обработки зависит от длины видео и ресурсов сервера.')

async def settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = USER_SETTINGS.setdefault(uid, {'duration': 35, 'count': 5})
    kb = [[InlineKeyboardButton(f'Длительность: {cfg["duration"]} сек', callback_data='duration')],
          [InlineKeyboardButton(f'Количество: {cfg["count"]}', callback_data='count')],
          [InlineKeyboardButton('Готово', callback_data='done')]]
    await update.callback_query.edit_message_text('Настройки клипов', reply_markup=InlineKeyboardMarkup(kb))

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    cfg = USER_SETTINGS.setdefault(uid, {'duration': 35, 'count': 5})
    if q.data == 'settings':
        await settings(update, context)
    elif q.data == 'help':
        await q.edit_message_text('Отправь видео или ссылку на видео. Бот распознает речь, оценит фрагменты и соберёт вертикальные MP4 с субтитрами.')
    elif q.data == 'duration':
        await q.edit_message_text('Выбери длину одного клипа:', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('20 сек', callback_data='d20'), InlineKeyboardButton('35 сек', callback_data='d35'), InlineKeyboardButton('50 сек', callback_data='d50')],
            [InlineKeyboardButton('Назад', callback_data='settings')]]))
    elif q.data == 'count':
        await q.edit_message_text('Сколько клипов сделать?', reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton('3', callback_data='c3'), InlineKeyboardButton('5', callback_data='c5'), InlineKeyboardButton('8', callback_data='c8')],
            [InlineKeyboardButton('Назад', callback_data='settings')]]))
    elif q.data.startswith('d'):
        cfg['duration'] = int(q.data[1:]); await settings(update, context)
    elif q.data.startswith('c'):
        cfg['count'] = min(int(q.data[1:]), MAX_CLIPS); await settings(update, context)
    elif q.data == 'done':
        await q.edit_message_text(f'Готово: {cfg["count"]} клипов по ~{cfg["duration"]} сек. Теперь пришли видео или ссылку.')

URL_RE = re.compile(r'https?://\S+', re.I)

async def settings_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    cfg = USER_SETTINGS.setdefault(uid, {'duration': 35, 'count': 5})
    kb = [[InlineKeyboardButton(f'Длительность: {cfg["duration"]} сек', callback_data='duration')],
          [InlineKeyboardButton(f'Количество: {cfg["count"]}', callback_data='count')],
          [InlineKeyboardButton('Готово', callback_data='done')]]
    await update.message.reply_text('⚙️ Настройки нарезки', reply_markup=InlineKeyboardMarkup(kb))

async def handle_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    uid = update.effective_user.id
    cfg = USER_SETTINGS.setdefault(uid, {'duration': 35, 'count': 5})
    workdir = Path(tempfile.mkdtemp(prefix='clipforge_'))
    status = await msg.reply_text('⏳ Принял. Подготавливаю видео…')
    try:
        src = workdir / 'source.mp4'
        if msg.video or msg.document:
            media = msg.video or msg.document
            if msg.document and (not media.mime_type or not media.mime_type.startswith('video/')):
                await status.edit_text('Нужен видеофайл или ссылка на видео.'); return
            size = getattr(media, 'file_size', 0) or 0
            if size > MAX_UPLOAD_MB * 1024 * 1024:
                await status.edit_text(f'Файл слишком большой для текущего лимита ({MAX_UPLOAD_MB} МБ). Для длинных видео используй доступную прямую ссылку или веб-загрузчик.'); return
            tg_file = await context.bot.get_file(media.file_id)
            await tg_file.download_to_drive(custom_path=str(src))
        else:
            text = msg.text or ''
            match = URL_RE.search(text)
            if not match or not is_http_url(match.group(0)):
                await status.edit_text('Пришли видеофайл или корректную ссылку на видео.'); return
            url = match.group(0).rstrip('.,!?;:)]}')
            if not is_http_url(url):
                await status.edit_text('Ссылка выглядит некорректно. Проверь её и отправь ещё раз.'); return
            await status.edit_text('🔗 Скачиваю доступное видео…')
            proc = await asyncio.create_subprocess_exec('yt-dlp', '--no-playlist', '--max-filesize', f'{MAX_URL_MB}M', '--socket-timeout', '20', '--retries', '2', '-f', 'bv*[height<=1080]+ba/b[height<=1080]', '--merge-output-format', 'mp4', '-o', str(workdir / 'source.%(ext)s'), url, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
            _, err = await proc.communicate()
            if proc.returncode != 0:
                log.warning('yt-dlp failed: %s', err.decode(errors='ignore')[-1500:])
                await status.edit_text('Не получилось получить видео по ссылке. Попробуй загрузить файл напрямую или проверить доступность ссылки.'); return
            candidates = [p for p in workdir.glob('source.*') if p.suffix.lower() in ('.mp4','.mkv','.webm','.mov')]
            if not candidates:
                await status.edit_text('Не нашёл скачанный видеофайл.'); return
            if candidates[0].stat().st_size > MAX_URL_MB * 1024 * 1024:
                await status.edit_text(f'Видео превышает лимит обработки {MAX_URL_MB} МБ.'); return
            src = candidates[0]
        await status.edit_text('🧠 Анализирую речь и ищу самостоятельные интересные фрагменты. Это может занять время…')
        clips = await asyncio.to_thread(make_clips, str(src), str(workdir), cfg['duration'], cfg['count'])
        if not clips:
            await status.edit_text('Не удалось найти подходящие фрагменты. Попробуй видео с более разборчивой речью или другой длительностью.'); return
        await status.edit_text(f'✅ Готово: {len(clips)} клипов. Отправляю результаты…')
        for i, clip in enumerate(clips, 1):
            await context.bot.send_chat_action(chat_id=uid, action=ChatAction.UPLOAD_VIDEO)
            with open(clip['path'], 'rb') as f:
                await context.bot.send_video(chat_id=uid, video=f, caption=f'🎬 Клип {i}/{len(clips)} · оценка {clip["score"]}/100', supports_streaming=True, read_timeout=180, write_timeout=180)
        await msg.reply_text('Готово. Хочешь нарезать ещё одно видео? Просто отправь его сюда.')
    except Exception:
        log.exception('processing error')
        try: await status.edit_text('Произошла ошибка при обработке. Попробуй видео покороче или отправь его позже.')
        except Exception: pass
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

async def main():
    if not TOKEN:
        raise SystemExit('Set BOT_TOKEN environment variable. See .env.example')
    app = Application.builder().token(TOKEN).concurrent_updates(False).build()
    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('help', help_cmd))
    app.add_handler(CommandHandler('settings', settings_cmd))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler((filters.VIDEO | filters.Document.ALL | filters.TEXT) & ~filters.COMMAND, handle_input))
    await app.initialize()
    await app.start()
    if WEBHOOK_URL:
        # Hosting services such as Render need an inbound HTTP listener.
        await app.updater.start_webhook(
            listen='0.0.0.0', port=PORT, url_path=WEBHOOK_PATH,
            webhook_url=f'{WEBHOOK_URL}/{WEBHOOK_PATH}',
            secret_token=WEBHOOK_SECRET or None,
            drop_pending_updates=True,
        )
        log.info('ClipForge webhook started on port %s', PORT)
    else:
        await app.updater.start_polling(drop_pending_updates=True)
        log.info('ClipForge polling started')
    try:
        while True: await asyncio.sleep(3600)
    finally:
        if app.updater.running:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()

if __name__ == '__main__':
    asyncio.run(main())
