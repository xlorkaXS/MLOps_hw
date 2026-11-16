"""Файловый outbox: accepted содержит release_id, журнал восстанавливается из него."""
import json
from pathlib import Path

from release import atomic_bytes, atomic_json, fingerprint, publish, read, validate_release


def events(root):
    path = root / 'publications.jsonl'
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def recover(root):
    pointer = root / 'accepted.json'
    if not pointer.exists():
        return
    accepted = read(pointer)
    id_ = accepted.get('release_id')
    if not id_:
        return                       # первоначальная baseline, не событие выпуска
    rows = events(root)
    if not any(r['release_id'] == id_ for r in rows):
        rows.append(dict(release_id=id_, model_id=accepted['model_id']))
        atomic_bytes(root / 'publications.jsonl',
                     ''.join(json.dumps(row) + '\n' for row in rows).encode())


def commit(root, manifest, report, *, fail_after_pointer=False):
    validate_release(manifest, report)
    manifest = {k: v for k, v in manifest.items() if k != 'release_id'}
    id_ = fingerprint({'manifest': manifest, 'decision': report})
    version = dict(manifest, release_id=id_)
    target = root / 'accepted.json'
    recover(root)                    # до того, как следующий выпуск скроет outbox
    history = root / 'history'
    if target.exists():
        previous = read(target)
        atomic_json(history / (previous.get('release_id') or fingerprint(previous)), previous)
    unchanged = target.exists() and read(target) == version
    publish(version, report, target)
    if fail_after_pointer:
        raise RuntimeError('Инъекция сбоя после accepted.json, до журнала')
    recover(root)
    return 'UNCHANGED' if unchanged else 'PUBLISHED'


def rollback(root: Path, model_id, verifier):
    recover(root)
    matches = [read(p) for p in sorted((root / 'history').glob('*'))]
    matches = [m for m in matches if m['model_id'] == model_id]
    if len(matches) != 1:
        raise ValueError('Нужна однозначная ранее принятая версия')
    verifier(matches[0], root)
    atomic_json(root / 'accepted.json', matches[0])
