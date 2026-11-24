import asyncio
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from dotenv import load_dotenv
ENV_PATH = Path(__file__).resolve().with_name('.env')
load_dotenv(ENV_PATH, override=True)
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
ACTIVE = set()
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
    context.user_data.pop('selection', None)
    await update.message.reply_text('Выбор отменён.' if update.effective_user.id not in ACTIVE else 'Текущая операция уже выполняется; отмена загрузки не поддерживается.')

async def link(update, context):
    if not allowed(update):
        await update.message.reply_text('Доступ закрыт. /id — узнать ID чата.')
        return
    uid = update.effective_user.id
    if uid in ACTIVE:
        await update.message.reply_text('Дождись завершения текущей операции.'); return
    url = (' '.join(context.args or []) if update.message.text.startswith('/') else update.message.text).strip()
    if not re.fullmatch(r'https?://[^\s]+', url, re.I) or len(url)>4096:
        await update.message.reply_text('Отправь одну ссылку http:// или https://.'); return
    ACTIVE.add(uid)
    context.user_data.pop('selection', None)
    context.user_data['status'] = 'Анализ ссылки…'
    try:
        msg = await update.message.reply_text('🔎 Анализирую ссылку…')
        async with SLOTS:
            result = await asyncio.to_thread(core.analyze, url)
        text = f"🎬 {result['title'][:500]}\nИсточник: {result['source_label']}"
        if result.get('author'): text += '\nАвтор: ' + str(result['author'])[:200]
        text += '\n\n' + result['message'][:1000]
        keyboard = [[InlineKeyboardButton('Открыть оригинал', url=url)]]
        if result['can_download']:
            nonce = uuid.uuid4().hex[:12]
            qualities = result['qualities']
            context.user_data['selection'] = (nonce, url, qualities, time.monotonic(), update.effective_chat.id, msg.message_id)
            keyboard += [[InlineKeyboardButton('⬇️ '+q['label'], callback_data=f'd:{nonce}:{i}')] for i,q in enumerate(qualities)]
            text += '\n\nВыбери качество:'
        await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True)
    except Exception:
        await update.message.reply_text('Не удалось проанализировать ссылку. Проверь доступность видео и попробуй снова.')
    finally:
        ACTIVE.discard(uid)
        context.user_data['status'] = 'Нет активной загрузки.'

async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not allowed(update): await q.answer('Доступ закрыт.', show_alert=True); return
    uid = update.effective_user.id
    if uid in ACTIVE: await q.answer('Уже выполняется операция.', show_alert=True); return
    selection = context.user_data.get('selection')
    _, nonce, idx = q.data.split(':')
    if not selection or selection[0] != nonce or time.monotonic()-selection[3]>1800 or selection[4] != update.effective_chat.id or selection[5] != q.message.message_id or not 0<=int(idx)<len(selection[2]):
        await q.answer('Кнопка устарела. Отправь ссылку заново.', show_alert=True); return
    ACTIVE.add(uid)
    context.user_data.pop('selection', None)
    jid = uuid.uuid4().hex
    worker = None
    try:
        await q.answer()
        await q.edit_message_reply_markup(reply_markup=None)
        msg = await q.message.reply_text('⏳ В очереди…')
        context.user_data['status'] = 'В очереди…'
        async with SLOTS:
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
                await msg.edit_text('Не удалось скачать видео. Попробуй другое качество или ссылку.'); return
            path = Path(job['path'])
            if path.stat().st_size >= LIMIT:
                await msg.edit_text('Файл слишком большой. Выбери качество ниже, отправив ссылку снова.'); return
            context.user_data['status'] = 'Отправляю файл…'
            await msg.edit_text('📤 Отправляю файл…')
            with path.open('rb') as f:
                await q.message.reply_document(document=f, filename=job['filename'], write_timeout=300, read_timeout=300)
            try:
                await msg.edit_text('🎞 Подготавливаю видео для просмотра…')
                media = path.with_name('telegram_preview_' + jid + '.mp4')
                await asyncio.to_thread(make_preview, path, media)
                if media.stat().st_size >= LIMIT:
                    raise ValueError('Медиа превышает лимит')
                with media.open('rb') as f:
                    await q.message.reply_video(video=f, supports_streaming=True, filename=job['filename'].rsplit('.', 1)[0]+'.mp4', write_timeout=300, read_timeout=300)
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
        ACTIVE.discard(uid)
        context.user_data['status'] = 'Нет активной загрузки.'

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
    app.run_polling(allowed_updates=['message','callback_query'])

if __name__ == '__main__': main()
