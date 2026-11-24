from contextlib import closing
import os
import sqlite3
import time
from pathlib import Path

TTL = 7 * 86400

def connect():
    root = Path(os.getenv('RAILWAY_VOLUME_MOUNT_PATH') or Path(__file__).parent)
    db = Path(os.getenv('CACHE_DB') or root / 'nocturne_cache.sqlite3')
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db, timeout=5)
    conn.execute('CREATE TABLE IF NOT EXISTS videos (chat INTEGER, url TEXT, quality TEXT, label TEXT, document TEXT, video TEXT, saved REAL, PRIMARY KEY(chat,url,quality))')
    return conn

def get(chat, url):
    with closing(connect()) as c, c:
        c.execute('DELETE FROM videos WHERE saved < ?', (time.time()-TTL,))
        return c.execute('SELECT quality,label,document,video FROM videos WHERE chat=? AND url=? ORDER BY saved DESC', (chat,url)).fetchall()

def put(chat, url, quality, label, document, video):
    with closing(connect()) as c, c:
        c.execute('INSERT OR REPLACE INTO videos VALUES (?,?,?,?,?,?,?)', (chat,url,quality,label,document,video,time.time()))

def remove(chat,url,quality):
    with closing(connect()) as c, c:
        c.execute('DELETE FROM videos WHERE chat=? AND url=? AND quality=?',(chat,url,quality))
