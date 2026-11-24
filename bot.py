import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
import tempfile
from collections import Counter
import cache_store as cache
from pathlib import Path
from dotenv import load_dotenv
ENV_PATH = Path(__file__).resolve().with_name('.env')
load_dotenv(ENV_PATH, override=not bool(os.getenv("RAILWAY_ENVIRONMENT")))
import downloader as core
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, CallbackQueryHandler, ContextTypes, filters
from telegram.error import TelegramError

logging.basicConfig(level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
TOKEN = os.getenv('BOT_TOKEN', '')
ALLOWED = {int(x.strip()) for x in os.getenv('ALLOWED_USER_IDS', '').split(',') if x.strip()}
GROUPS = {int(x.strip()) for x in os.getenv('ALLOWED_CHAT_IDS', '').split(',') if x.strip()}
LIMIT = int(os.getenv('MAX_FILE_MB', '49')) * 1000000
print("Файл конфигурации:", ENV_PATH, flush=True)
print("Разрешенные пользователи:", ALLOWED, flush=True)
print("Разрешенные группы:", GROUPS, flush=True)
SLOTS = asyncio.Semaphore(2)
ACTIVE = Counter()
MAX_PENDING = 4

def finish(uid):
    ACTIVE[uid] -= 1
    if ACTIVE[uid] <= 0: ACTIVE.pop(uid, None)

def selections(context):
    items = context.user_data.setdefault("selections", {})
    for key, value in list(items.items()):
        if time.monotonic()-value[3] > 1800: items.pop(key, None)
    return items

HELP = '🌙 Nocturne Video\n\nВ группе используй /d ссылка. В личке отправь ссылку на YouTube, Shorts, TikTok, MP4 или HLS. Выбери качество и получи файл.\n\n/status — состояние загрузки\n/cancel — отменить выбор качества\n/id — твой Telegram ID\n/help — помощь\n\nСкачивай только видео, на которые у тебя есть права.'

def allowed(update):
    user, chat = update.effective_user, update.effective_chat
    if not user or not chat:
        return False
    permitted = (user.id in ALLOWED if chat.type == 'private'
                 else chat.type in ('group', 'supergroup') and chat.id in GROUPS)
    logger.info('Access: user=%s chat=%s type=%s allowed=%s', user.id, chat.id, chat.type, permitted)
    return permitted

async def identify(update, context):
    logger.info('ID command: user=%s chat=%s', update.effective_user.id, update.effective_chat.id)
    await update.message.reply_text(f'Твой Telegram ID: {update.effective_user.id}\nID чата: {update.effective_chat.id}')

async def start(update, context):
    await update.message.reply_text(HELP if allowed(update) else 'Доступ закрыт. /id — узнать свой ID и передать администратору.')

async def status(update, context):
    if not allowed(update): return
    await update.message.reply_text(context.user_data.get('status', 'Нет активной загрузки.'))

async def cancel(update, context):
    if not allowed(update): return
    items = selections(context)
    for key, value in list(items.items()):
        if value[4] == update.effective_chat.id: items.pop(key, None)
    await update.message.reply_text('Выбор отменён.' if update.effective_user.id not in ACTIVE else 'Текущая операция уже выполняется; отмена загрузки не поддерживается.')

async def link(update, context):
    if not allowed(update):
        await update.message.reply_text('Доступ закрыт. /id — узнать ID чата.')
        return
    uid = update.effective_user.id
    if ACTIVE[uid] >= MAX_PENDING:
        await update.message.reply_text('У тебя уже 4 операции в очереди.'); return
    url = (' '.join(context.args or []) if update.message.text.startswith('/') else update.message.text).strip()
    if not url and update.message.reply_to_message:
        await uploaded(update, context, update.message.reply_to_message); return
    fresh = url.startswith('--fresh ')
    if fresh: url = url[8:].strip()
    if not re.fullmatch(r'https?://[^\s]+', url, re.I) or len(url)>4096:
        await update.message.reply_text('Отправь одну ссылку http:// или https://.'); return
    ACTIVE[uid] += 1
    context.user_data['status'] = 'Анализ ссылки…'
    try:
        msg = await update.message.reply_text('🔎 Анализирую ссылку…')
        async with SLOTS:
            cached = []
            if not fresh and core.detect_source(url) in ('youtube','youtube_shorts','tiktok'):
                try: cached = await asyncio.to_thread(cache.get, update.effective_chat.id, url)
                except Exception: logger.warning('Cache unavailable')
            if cached:
                result = {'title':'Видео из кэша', 'source_label':'Telegram', 'message':'Повторная отправка без скачивания. Для нового анализа: /d --fresh ссылка', 'can_download':True, 'qualities':[{'id':r[0], 'label':r[1], 'cached':r[2:]} for r in cached]}
            else:
                result = await asyncio.to_thread(core.analyze, url)
        text = f"🎬 {result['title'][:500]}\nИсточник: {result['source_label']}"
        if result.get('author'): text += '\nАвтор: ' + str(result['author'])[:200]
        text += '\n\n' + result['message'][:1000]
        keyboard = [[InlineKeyboardButton('Открыть оригинал', url=url)]]
        if result['can_download']:
            nonce = uuid.uuid4().hex[:12]
            qualities = result['qualities']
            items = selections(context)
            if len(items) >= 20: items.pop(next(iter(items)))
            items[nonce] = (nonce, url, qualities, time.monotonic(), update.effective_chat.id, msg.message_id)
            keyboard += [[InlineKeyboardButton('⬇️ '+q['label'], callback_data=f'd:{nonce}:{i}')] for i,q in enumerate(qualities)]
            text += '\n\nВыбери качество:'
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    except Exception as exc:
        await update.message.reply_text(core.public_error(exc, url))
    finally:
        finish(uid)
        context.user_data['status'] = 'Есть операции в очереди.' if ACTIVE[uid] else 'Нет активной загрузки.'

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update): await q.answer('Доступ закрыт.', show_alert=True); return
    uid = update.effective_user.id
    if ACTIVE[uid] >= MAX_PENDING: await q.answer('Очередь заполнена.', show_alert=True); return
    _, nonce, idx = q.data.split(':')
    selection = selections(context).get(nonce)
    if not selection or selection[0] != nonce or time.monotonic()-selection[3]>1800 or selection[4] != update.effective_chat.id or selection[5] != q.message.message_id or not 0<=int(idx)<len(selection[2]):
        await q.answer('Кнопка устарела. Отправь ссылку заново.', show_alert=True); return
    ACTIVE[uid] += 1
    selections(context).pop(nonce, None)
    jid = uuid.uuid4().hex
    worker = None
    try:
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=None)
        msg = await q.message.reply_text('⏳ В очереди…')
        context.user_data['status'] = 'В очереди…'
        async with SLOTS:
            quality = selection[2][int(idx)]
            if quality.get('cached'):
                document, video = quality['cached']
                try:
                    await q.message.reply_document(document=document)
                    await q.message.reply_video(video=video, supports_streaming=True)
                    await msg.edit_text('✅ Готово! Из кэша Telegram.')
                except TelegramError:
                    try: await asyncio.to_thread(cache.remove, update.effective_chat.id, selection[1], quality['id'])
                    except Exception: pass
                    await msg.edit_text('Кэш недоступен. Повтори /d --fresh ссылка.')
                return
            with core.JOBS_LOCK: core.JOBS[jid] = {'status':'queued','progress':0,'updated':time.time()}
            worker = asyncio.create_task(asyncio.to_thread(core.run_download_job,jid,selection[1],selection[2][int(idx)]['id']))
            last = ''
            while not worker.done():
                with core.JOBS_LOCK: job = dict(core.JOBS[jid])
                current = f"{job.get('message','Подготовка…')} · {job.get('progress',0)}%"
                context.user_data['status'] = current
                if current != last:
                    try: await msg.edit_text(current)
                    except TelegramError: pass
                    last = current
                await asyncio.wait({worker},timeout=4)
            await worker
            with core.JOBS_LOCK: job = dict(core.JOBS[jid])
            if job['status'] != 'ready':
                await msg.edit_text(job.get('message','Не удалось скачать видео.')); return
            path = Path(job['path'])
            if path.stat().st_size >= LIMIT:
                await msg.edit_text('Файл слишком большой. Выбери качество ниже, отправив ссылку снова.'); return
            context.user_data['status'] = 'Отправляю файл…'
            await msg.edit_text('📤 Отправляю файл…')
            with path.open('rb') as f:
                document_msg = await q.message.reply_document(document=f, filename=job['filename'], write_timeout=300, read_timeout=300)
            try:
                await msg.edit_text('🎞 Подготавливаю видео для просмотра…')
                media = path.with_name('telegram_preview_' + jid + '.mp4')
                await asyncio.to_thread(make_preview, path, media)
                if media.stat().st_size >= LIMIT:
                    raise ValueError('Медиа превышает лимит')
                with media.open('rb') as f:
                    video_msg = await q.message.reply_video(video=f, supports_streaming=True, filename=job['filename'].rsplit('.', 1)[0]+'.mp4', write_timeout=300, read_timeout=300)
                if core.detect_source(selection[1]) in ('youtube','youtube_shorts','tiktok'):
                    try: await asyncio.to_thread(cache.put, update.effective_chat.id, selection[1], quality['id'], quality['label'], document_msg.document.file_id, video_msg.video.file_id)
                    except Exception: logger.warning('Cache write failed')
                await msg.edit_text('✅ Готово! Файл и видео отправлены.')
            except Exception:
                await msg.edit_text('⚠️ Файл отправлен, но медиа не удалось подготовить или отправить. Попробуй качество ниже.')
    except Exception:
        try: await q.message.reply_text('Ошибка загрузки или отправки. Попробуй снова.')
        except TelegramError: pass
    finally:
        if worker and not worker.done(): await asyncio.shield(worker)
        with core.JOBS_LOCK: job = core.JOBS.pop(jid,{})
        if job.get('folder'): shutil.rmtree(job['folder'],ignore_errors=True)
        finish(uid)
        context.user_data['status'] = 'Есть операции в очереди.' if ACTIVE[uid] else 'Нет активной загрузки.'

async def uploaded(update, context, source=None):
    if not allowed(update): return
    source = source or update.message
    media = source.video or source.document
    if not media or (source.document and not (source.document.mime_type or '').startswith('video/')):
        await update.message.reply_text('Ответь командой /d на видео или видеофайл.'); return
    if not media.file_size or media.file_size > min(LIMIT, 20_000_000):
        await update.message.reply_text('Для обработки вложений через Bot API нужен видеофайл не больше 20 МБ.'); return
    uid = update.effective_user.id
    if ACTIVE[uid] >= MAX_PENDING:
        await update.message.reply_text('Очередь заполнена.'); return
    ACTIVE[uid] += 1
    try:
        msg = await update.message.reply_text('⏳ В очереди на обработку файла…')
        async with SLOTS:
            with tempfile.TemporaryDirectory(prefix='nocturne_upload_') as folder:
                name = core.sanitize_filename(getattr(media,'file_name',None) or 'video.mp4')
                path = Path(folder) / ('input'+Path(name).suffix)
                remote = await context.bot.get_file(media.file_id)
                await remote.download_to_drive(path)
                if path.stat().st_size >= LIMIT: raise ValueError('limit')
                with path.open('rb') as f:
                    await update.message.reply_document(document=f, filename=name, write_timeout=300, read_timeout=300)
                target = Path(folder) / 'preview.mp4'
                await asyncio.to_thread(make_preview,path,target)
                if target.stat().st_size >= LIMIT: raise ValueError('limit')
                with target.open('rb') as f:
                    await update.message.reply_video(video=f,supports_streaming=True,write_timeout=300,read_timeout=300)
                await msg.edit_text('✅ Файл и видео отправлены.')
    except Exception:
        await update.message.reply_text('Не удалось полностью обработать вложение. Проверь формат и размер файла.')
    finally: finish(uid)


def make_preview(source, target):
    ffmpeg = core.find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError('FFmpeg не найден')
    # Separate media copy; the downloaded original is never modified.
    subprocess.run([ffmpeg, '-nostdin', '-hide_banner', '-loglevel', 'error', '-y',
                    '-i', str(source), '-map', '0:v:0', '-map', '0:a:0?',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '25',
                    '-pix_fmt', 'yuv420p', '-vf', 'scale=trunc(iw/2)*2:trunc(ih/2)*2',
                    '-c:a', 'aac', '-b:a', '96k', '-movflags', '+faststart', str(target)],
                   check=True, timeout=900, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

async def setup(app):
    await app.bot.set_my_commands([BotCommand('d','Скачать видео по ссылке'),BotCommand('start','Главное меню'),BotCommand('help','Помощь'),BotCommand('status','Статус'),BotCommand('cancel','Отменить выбор'),BotCommand('id','Мой ID')])

def main():
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]+', TOKEN):
        raise SystemExit('Укажи полный BOT_TOKEN из BotFather в файле .env')
    if not ALLOWED and not GROUPS: print('Список доступа пуст: доступна только команда /id. Добавь свой ID в .env и перезапусти.')
    app = Application.builder().token(TOKEN).concurrent_updates(16).post_init(setup).build()
    for name, handler in [('d',link),('start',start),('help',start),('id',identify),('status',status),('cancel',cancel)]: app.add_handler(CommandHandler(name,handler))
    app.add_handler(CallbackQueryHandler(download,pattern=r'^d:[a-f0-9]{12}:\d{1,3}$'))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,link))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & (filters.VIDEO | filters.Document.VIDEO), uploaded))
    app.run_polling(allowed_updates=['message','callback_query'])

if __name__ == '__main__': main()
