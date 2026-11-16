import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from unittest.mock import patch

import release

PROJECT = Path(__file__).resolve().parents[1]


class Integration(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cwd = Path(self.tmp.name)
        self.root = self.cwd / 'workspace'
        self.pointer = self.root / 'accepted.json'
        self.lab('init', ok=True)
        self.server = None
        self.addCleanup(self.stop)

    def lab(self, *args, ok=None):
        p = subprocess.run([sys.executable, str(PROJECT / 'lab.py'), '--workspace',
                            str(self.root), *args], cwd=self.cwd, text=True,
                           capture_output=True, timeout=45)
        if ok is not None:
            self.assertEqual(p.returncode == 0, ok, p.stdout + p.stderr)
        return p

    def pointer_hash(self):
        return hashlib.sha256(self.pointer.read_bytes()).hexdigest()

    def stop(self):
        if self.server and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=5)

    def start(self, warmup=0):
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            self.url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        self.server = subprocess.Popen([sys.executable, str(PROJECT / 'service.py'),
            '--workspace', str(self.root), '--port', self.url.rsplit(':', 1)[-1],
            '--warmup-seconds', str(warmup)], cwd=self.cwd,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.wait('/health')

    def call(self, path, body=None):
        payload = None if body is None else json.dumps(body).encode()
        try:
            response = urlopen(Request(self.url + path, data=payload,
                               headers={'Content-Type': 'application/json'}), timeout=2)
        except HTTPError as exc:
            response = exc
        with response:
            return response.code, json.load(response)

    def wait(self, path):
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            try:
                status, result = self.call(path)
                if status == 200:
                    return result
            except OSError:
                pass
            if self.server.poll() is not None:
                self.fail(f'HTTP process exited {self.server.returncode}')
            time.sleep(.05)
        self.fail(f'Timeout waiting for {path}')

    def test_cache_survives_failure_and_tracks_bytes(self):
        before = self.pointer_hash()
        fail = self.lab('run', '--fail-after-prepare', ok=False)
        self.assertIn('PREPARED', fail.stdout)
        self.assertEqual(self.pointer_hash(), before)
        self.assertIn('CACHE_HIT', self.lab('run', ok=True).stdout)
        (self.root / 'input.txt').write_text('Новый пример по тому же пути\n')
        changed = self.lab('run', ok=True)
        self.assertIn('PREPARED', changed.stdout)
        self.assertNotIn('CACHE_HIT', changed.stdout)

    def test_incomplete_foreign_and_rejected_reports(self):
        before = self.pointer_hash()
        for name in ('report-reject.json', 'report-incomplete.json', 'report-A.json'):
            with self.subTest(report=name):
                self.lab('run', '--report', name, ok=False)
                self.assertEqual(self.pointer_hash(), before)

    def test_atomic_write_error_preserves_pointer(self):
        m, r = [json.loads((self.root / f'{n}-B.json').read_text()) for n in ('manifest', 'report')]
        before = self.pointer_hash()
        with patch('os.replace', side_effect=OSError('injected write failure')):
            with self.assertRaises(OSError):
                release.publish(m, r, self.pointer)
        self.assertEqual(self.pointer_hash(), before)
        self.assertEqual(list(self.root.glob('*.tmp')), [])

    def test_repeat_publication_in_new_process(self):
        self.lab('run', ok=True)
        before = self.pointer_hash()
        self.lab('run', ok=True)
        self.assertEqual(self.pointer_hash(), before)
        self.assertEqual(len((self.root / 'publications.jsonl').read_text().splitlines()), 1)

    def test_recover_publication_after_pointer_crash(self):
        failure = self.lab('run', '--fail-after-pointer', ok=False)
        self.assertIn('Инъекция сбоя после accepted.json', failure.stderr)
        self.assertEqual(json.loads(self.pointer.read_text())['model_id'], 'fixture-B')
        self.assertFalse((self.root / 'publications.jsonl').exists())
        before = self.pointer_hash()
        self.lab('run', ok=True)
        self.assertEqual(self.pointer_hash(), before)
        self.assertEqual(len((self.root / 'publications.jsonl').read_text().splitlines()), 1)

    def test_http_readiness(self):
        self.start(warmup=2)
        self.assertEqual(self.call('/ready')[0], 503)
        self.assertEqual(self.call('/generate', {'prompt': 'test'})[0], 503)
        self.assertEqual(self.call('/debug')[1]['calls'], 0)
        self.wait('/ready')
        self.assertEqual(self.call('/generate', {'prompt': 'test'})[0], 200)
        print('HTTP readiness: health=200, warmup ready/generate=503, calls=0; ready then generate=200', flush=True)

    def test_http_loaded_version_and_rollback(self):
        self.start()
        self.wait('/ready')
        self.assertEqual(self.call('/generate', {'prompt': 'test'})[1]['model_id'], 'fixture-A')
        self.lab('run', ok=True)
        status, old = self.call('/generate', {'prompt': 'test'})
        self.assertEqual(status, 200)
        self.assertEqual(old['answer'], 'STUB ONLY: fixture-A')
        self.assertEqual(old['model_id'], 'fixture-A')
        self.stop()
        self.start()
        self.wait('/ready')
        self.assertEqual(self.call('/generate', {'prompt': 'test'})[1]['model_id'], 'fixture-B')
        self.lab('rollback', '--to', 'fixture-A', ok=True)
        self.stop()
        self.start()
        self.wait('/ready')
        self.assertEqual(self.call('/generate', {'prompt': 'test'})[1]['model_id'], 'fixture-A')
        print('STUB ONLY: A loaded → B accepted/A still loaded → restart B → rollback/restart A', flush=True)

    def test_http_bad_request_does_not_call_backend(self):
        self.start()
        self.wait('/ready')
        before = self.call('/debug')[1]['calls']
        for prompt, limit in [('', 32), ('x', 0), ('x', 100000), ('x', '32'), ('x', True)]:
            with self.subTest(prompt=prompt, limit=limit):
                status, _ = self.call('/generate', {'prompt': prompt, 'max_new_tokens': limit})
                self.assertTrue(400 <= status < 500, status)
        self.assertEqual(self.call('/debug')[1]['calls'], before)
        self.assertEqual(self.call('/generate', {'prompt': 'test', 'max_new_tokens': 32})[0], 200)
        self.assertEqual(self.call('/debug')[1]['calls'], before + 1)
        print('HTTP validation: all invalid requests 4xx with unchanged calls; valid request adds one', flush=True)
