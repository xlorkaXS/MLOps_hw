"""HTTP benchmark; write every attempt, including errors/timeouts."""
import argparse
import concurrent.futures
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from release import atomic_json

PROMPTS = [
    'Каков орбитальный период Kepler-17 b? A: 1.486 суток; B: 3 суток; C: 7 суток. choices: A, B, C.',
    'Каков радиус Kepler-44 b? A: 3 радиуса Земли; B: 12.22 радиусов Земли; C: 25 радиусов Земли. choices: A, B, C.',
    'Как приготовить яблочный пирог? choices: A, B, C, REFUSE.',
    'Что измеряет орбитальный период? A: массу; B: время полного оборота; C: расстояние. choices: A, B, C.',
    'Что измеряют в кельвинах? A: температуру; B: расстояние; C: радиус. choices: A, B, C.',
    'Каков радиус Земли в радиусах Земли? A: 3; B: 2; C: 1. choices: A, B, C.',
    'Каков орбитальный период Земли в сутках? A: 365.25; B: 88; C: 687. choices: A, B, C, REFUSE.',
    'Что такое экзопланета? A: планета вне Солнечной системы; B: звезда; C: астероид. choices: A, B, C.',
]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--url', default='http://127.0.0.1:8091')
    p.add_argument('--out', type=Path, required=True)
    a = p.parse_args()
    a.out.mkdir(parents=True, exist_ok=True)
    with urlopen(a.url + '/ready', timeout=10) as response:
        ready = json.load(response)
    assert ready['ready']
    results = []

    def ask(pair):
        index, prompt = pair
        started = time.perf_counter()
        record = dict(mode=mode, input_id=index, prompt=prompt, status=None, error=None)
        try:
            req = Request(a.url + '/generate', data=json.dumps(dict(prompt=prompt, max_new_tokens=32)).encode(),
                          headers={'Content-Type': 'application/json'})
            with urlopen(req, timeout=60) as response:
                record['status'] = response.status
                record.update(json.load(response))
            if record['model_id'] != ready['model_id'] or record['runtime_sha256'] != ready['runtime_sha256']:
                raise ValueError('Изменилась загруженная версия')
        except HTTPError as e:
            record.update(status=e.code, error=e.read().decode())
        except Exception as e:
            record['error'] = str(e)
        record['client_seconds'] = time.perf_counter() - started
        return record

    for mode in (1, 2):
        start = time.perf_counter()
        with concurrent.futures.ThreadPoolExecutor(max_workers=mode) as pool:
            rows = list(pool.map(ask, enumerate(PROMPTS, 1)))
        elapsed = time.perf_counter() - start
        ok = [r for r in rows if r['status'] == 200 and not r['error']]
        latencies = [r['client_seconds'] for r in ok]
        results.append(dict(concurrency=mode, n=len(rows), successes=len(ok), errors=len(rows)-len(ok),
                            batch_seconds=elapsed, successes_per_second=len(ok)/elapsed,
                            success_mean_seconds=sum(latencies)/len(latencies) if ok else None,
                            success_max_seconds=max(latencies) if ok else None,
                            generated_tokens=sum(r['output_tokens'] for r in ok)))
        with (a.out / 'requests.jsonl').open('w' if mode == 1 else 'a') as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False) + '\n')
    atomic_json(a.out / 'benchmark.json', results)
    print(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
