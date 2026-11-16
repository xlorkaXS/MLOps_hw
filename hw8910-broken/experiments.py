"""Actual subprocess evidence. Never runs experiments against an existing workspace."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from release import atomic_json, file_digest, read


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('root', type=Path)
    parser.add_argument('output', type=Path)
    a = parser.parse_args()
    root = a.root.resolve()
    log = a.output
    log.mkdir(parents=True, exist_ok=True)
    results = []

    def run(label, script, args, code=0):
        pointer = root / 'accepted.json'
        before = file_digest(pointer) if pointer.exists() else None
        cmd = [sys.executable, script, '--workspace', str(root), *args]
        p = subprocess.run(cmd, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        (log / (label + '.log')).write_text(p.stdout)
        after = file_digest(pointer) if pointer.exists() else None
        row = dict(label=label, command=cmd[1:], exit=p.returncode, before=before, after=after)
        results.append(row)
        atomic_json(log / 'summary.json', results)
        print(label, 'exit', p.returncode, 'same pointer', before == after, flush=True)
        if (p.returncode == 0) != (code == 0):
            raise RuntimeError(p.stdout)
        return row

    run('init', 'lab.py', ['init'])
    failure = run('failure-after-prepare', 'flow.py', ['--fail-after-prepare'], 1)
    assert failure['before'] == failure['after']
    for name in ('incomplete', 'reject', 'A'):
        rejection = run('rejected-' + name, 'flow.py', ['--report', f'report-{name}.json'], 1)
        assert rejection['before'] == rejection['after']
    run('crash-after-pointer', 'flow.py', ['--fail-after-pointer'], 1)
    assert not (root / 'publications.jsonl').exists()
    recovered = run('recover', 'flow.py', [])
    assert recovered['before'] == recovered['after']
    assert len((root / 'publications.jsonl').read_text().splitlines()) == 1
    run('repeat', 'flow.py', [])
    (root / 'input.txt').write_text('Новый пример по прежнему пути\n')
    run('changed-input', 'flow.py', [])
    rows = [read(p) for p in (root / 'attempts').glob('*.json')]
    states = {r['before']: r for r in rows}
    assert sum(r['preparation_state'] == 'Cached' for r in rows) >= 2, states
    assert sum(r['preparation_state'] == 'Completed' for r in rows) == 2, states
    assert len((root / 'publications.jsonl').read_text().splitlines()) == 1
    atomic_json(log / 'attempts.json', rows)
    (log / 'publications.jsonl').write_text((root / 'publications.jsonl').read_text())


if __name__ == '__main__':
    main()
