"""The cloud backend speaks the OpenAI-compatible protocol; checked against a local mock server."""
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch
from vidsum import cloud

SEEN = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        SEEN.append((self.path, dict(self.headers), body))
        if self.path.endswith('/chat/completions'):
            payload = json.loads(body)
            if 'max_tokens' in payload and payload['model'] == 'strict-model':
                return self.reply(400, {'error': {'message': "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens'."}})
            return self.reply(200, {'choices': [{'message': {'content': 'a red square'}, 'finish_reason': 'stop'}], 'usage': {'completion_tokens': 3}})
        if self.path.endswith('/audio/transcriptions'):
            return self.reply(200, {'text': 'hello', 'language': 'english', 'segments': [{'start': 0.0, 'end': 1.5, 'text': 'hello'}]})
        self.reply(404, {})

    def reply(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(data)))
        self.end_headers(); self.wfile.write(data)


class CloudProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = HTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()
        cls.base = f'http://127.0.0.1:{cls.httpd.server_port}/v1'

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    def env(self, **extra):
        return patch.dict(os.environ, {'VIDSUM_API_KEY': 'test-key', 'VIDSUM_API_BASE': self.base, 'VIDSUM_CLOUD_MODEL': 'vision-model', **extra})

    def test_describe_sends_images_and_key(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as work, self.env():
            path = Path(work)/'f.jpg'; Image.new('RGB', (64, 64), 'red').save(path)
            SEEN.clear()
            result = cloud.describe([path], 'what is this?')
            self.assertEqual(result['description'], 'a red square')
            _, headers, body = SEEN[-1]
            payload = json.loads(body)
            self.assertEqual(headers['Authorization'], 'Bearer test-key')
            parts = payload['messages'][0]['content']
            self.assertEqual(parts[0]['type'], 'text'); self.assertTrue(parts[1]['image_url']['url'].startswith('data:image/jpeg;base64,'))

    def test_chat_switches_to_max_completion_tokens(self):
        with self.env():
            SEEN.clear()
            cloud.chat(self.base, 'k', 'strict-model', 'hi', 100)
            self.assertEqual(len(SEEN), 2)
            self.assertIn('max_completion_tokens', json.loads(SEEN[-1][2]))

    def test_transcribe_multipart_and_offsets(self):
        with tempfile.TemporaryDirectory() as work, self.env(VIDSUM_CLOUD_ASR_MODEL='asr-model'):
            audio = Path(work)/'a.wav'
            subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'sine=duration=5', str(audio)], check=True)
            SEEN.clear()
            result = cloud.transcribe(audio, Path(work)/'chunks', seconds=3)
            self.assertEqual(len(SEEN), 2)
            self.assertIn(b'name="model"\r\n\r\nasr-model', SEEN[0][2])
            self.assertIn(b'verbose_json', SEEN[0][2])
            self.assertEqual([s['start'] for s in result['segments']], [0.0, 3.0])
            self.assertEqual(result['language'], 'en')
            self.assertFalse(list((Path(work)/'chunks').glob('*.mp3')))


if __name__ == '__main__':
    unittest.main()
