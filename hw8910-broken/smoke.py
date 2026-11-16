"""Load the actual pinned model twice, exercise HTTP and benchmark, then shut down."""
import argparse
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from release import atomic_json, file_digest


def main():
    p = argparse.ArgumentParser()
    p.add_argument('root', type=Path)
    p.add_argument('output', type=Path)
    a = p.parse_args()
    a.output.mkdir(parents=True, exist_ok=True)
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
    url = f'http://127.0.0.1:{port}'
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1')
    result = dict(accepted_sha256=file_digest(a.root / 'accepted.json'), attempts=[])

    def call(path, body=None):
        data = None if body is None else json.dumps(body).encode()
        try:
            response = urlopen(Request(url + path, data=data,
                headers={'Content-Type': 'application/json'}), timeout=60)
        except HTTPError as e:
            response = e
        with response:
            return dict(status=response.code, body=json.load(response))

    for attempt in (1, 2):
        with (a.output / f'api-{attempt}.log').open('w') as f:
            server = subprocess.Popen([sys.executable, 'service.py', '--backend', 'real',
                '--workspace', str(a.root), '--port', str(port), '--warmup-seconds', '.5'],
                stdout=f, stderr=subprocess.STDOUT, env=env)
            try:
                until = time.monotonic() + 70
                while True:
                    if server.poll() is not None or time.monotonic() > until:
                        raise RuntimeError('API не запущен; см. api log')
                    try:
                        ready = call('/ready')
                        if ready['status'] == 200:
                            break
                    except OSError:
                        pass
                    time.sleep(.1)
                entry = dict(process=attempt, health=call('/health'), ready=ready,
                             rejected=call('/generate', {'prompt': 'test', 'max_new_tokens': 4096}),
                             real=call('/generate', {'prompt': 'Как приготовить яблочный пирог? choices: A, B, C, REFUSE.'}))
                assert entry['real']['status'] == 200 and entry['rejected']['status'] == 422
                assert entry['real']['body']['model_id'] == ready['body']['model_id']
                assert entry['real']['body']['runtime_sha256'] == ready['body']['runtime_sha256']
                assert 'STUB ONLY' not in entry['real']['body']['answer']
                result['attempts'].append(entry)
                atomic_json(a.output / 'api-smoke.json', result)
                if attempt == 1:
                    subprocess.run([sys.executable, 'bench.py', '--url', url, '--out', str(a.output)], check=True)
            finally:
                if server.poll() is None:
                    server.terminate()
                    server.wait(timeout=15)
    assert result['attempts'][0]['ready'] == result['attempts'][1]['ready']
    assert result['accepted_sha256'] == file_digest(a.root / 'accepted.json')
    print('Verified real API, restart, limits and baseline snapshot; server stopped.')


if __name__ == '__main__':
    main()
