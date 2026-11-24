import unittest
from unittest.mock import patch
import downloader as d
class CoreTests(unittest.TestCase):
    def test_sources(self):
        for u,s in [('https://youtu.be/abc','youtube'),('https://www.youtube.com/shorts/a','youtube_shorts'),('https://vm.tiktok.com/a','tiktok'),('https://bad-tiktok.com/a','unknown'),('https://example.org/a.m3u8','hls'),('https://example.org/a.mp4','direct')]:
            self.assertEqual(d.detect_source(u),s)
    def test_local(self):
        self.assertFalse(d.is_safe_public_url('http://localhost')[0]); self.assertFalse(d.is_safe_public_url('file:///etc/passwd')[0])
    def test_quality(self):
        self.assertIn('height<=1920',d.quality_selector('h:1080:1920'))
    def test_direct_analysis(self):
        with patch.object(d,'is_safe_public_url',return_value=(True,None)),patch.object(d,'head_info',return_value={'final_url':'https://example.org/a.mp4','content_type':'video/mp4','filename':'a.mp4'}):
            self.assertEqual(d.analyze('https://example.org/a.mp4')['qualities'][0]['id'],'original')
if __name__=='__main__': unittest.main()
