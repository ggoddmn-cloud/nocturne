import base64, os, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
import downloader as d
import cache_store as c
class V22Tests(unittest.TestCase):
    def test_cache(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {'CACHE_DB':str(Path(tmp)/'test.sqlite3')}):
            c.put(1,'url','q','720p','doc','vid')
            self.assertEqual(c.get(1,'url'), [('q','720p','doc','vid')])
            self.assertEqual(c.get(2,'url'), [])
            c.remove(1,'url','q'); self.assertEqual(c.get(1,'url'), [])
    def test_cookie_scope_and_cleanup(self):
        raw='# Netscape HTTP Cookie File\n.youtube.com\tTRUE\t/\tTRUE\t0\ttest\tvalue\n.example.com\tTRUE\t/\tTRUE\t0\ttest\tother\n'
        val=base64.b64encode(raw.encode()).decode()
        class Fake:
            def __init__(self,opts): self.opts=opts
            def __enter__(self): return self
            def __exit__(self,*args): pass
        with patch.dict(os.environ,{'YOUTUBE_COOKIES_B64':val,'TIKTOK_COOKIES_B64':''}), patch.object(d,'yt_dlp_module') as m:
            m.return_value.YoutubeDL=Fake
            with d.platform_session('https://youtube.com/watch?v=a',{}) as session:
                path=Path(session.opts['cookiefile']); self.assertIn('youtube.com',path.read_text()); self.assertNotIn('example.com',path.read_text())
            self.assertFalse(path.exists())
            with d.platform_session('https://tiktok.com/video/1',{}) as session: self.assertNotIn('cookiefile',session.opts)
    def test_errors(self):
        self.assertIn('YouTube',d.public_error(Exception('Sign in to confirm you’re not a bot'),'https://youtu.be/a'))
        self.assertIn('TikTok',d.public_error(Exception('HTTP 403'),'https://tiktok.com/video/1'))
