"""Метаданные, проверка файлов и атомарная смена указателя (один писатель)."""
import hashlib
import json
import math
import os
import re
import tempfile
from pathlib import Path


def cache_key(data_path: Path, config: dict) -> str:
    return fingerprint({'data_sha256': file_digest(data_path), 'config': config})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_digest(path):
    with Path(path).open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def atomic_bytes(target, data):
    target = Path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, suffix='.tmp', delete=False) as f:
            tmp = Path(f.name)
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if tmp is not None:
            tmp.unlink(missing_ok=True)


def atomic_json(target, value):
    atomic_bytes(target, canonical(value) + b'\n')


BINDINGS = ('model_id', 'model_sha256', 'protocol_sha256', 'golden_sha256', 'runtime_sha256')


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def is_sha(value):
    return isinstance(value, str) and re.fullmatch('[a-f0-9]{64}', value) is not None


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or type(manifest.get('schema_version')) is not int
            or manifest['schema_version'] != 1 or not nonempty(manifest.get('model_id'))
            or not nonempty(manifest.get('artifact_uri'))
            or any(not is_sha(manifest.get(k)) for k in BINDINGS[1:])):
        raise ValueError('Некорректный manifest')
    runtime = manifest.get('runtime')
    if not isinstance(runtime, dict) or not runtime or fingerprint(runtime) != manifest['runtime_sha256']:
        raise ValueError('Runtime не совпадает с хешем')


def verify_files(manifest, root):
    validate_manifest(manifest)
    files = manifest.get('file_hashes')
    if not isinstance(files, dict) or not files or fingerprint(files) != manifest['model_sha256']:
        raise ValueError('Не закреплён состав артефакта')
    for name, expected in files.items():
        relative = Path(name)
        if relative.is_absolute() or '..' in relative.parts or not is_sha(expected):
            raise ValueError('Некорректный путь или хеш артефакта')
        if file_digest(Path(root) / relative) != expected:
            raise ValueError(f'Изменён файл: {name}')


def validate_release(manifest: dict, report: dict) -> None:
    validate_manifest(manifest)
    if (not isinstance(report, dict) or report.get('status') != 'complete'
            or report.get('passed') is not True or not nonempty(report.get('evaluation_run_id'))):
        raise ValueError('Кандидат отклонён или отчёт неполный')
    if any(report.get(k) != manifest[k] for k in BINDINGS):
        raise ValueError('Отчёт относится к другой версии или протоколу')
    metrics = report.get('metrics')
    if not isinstance(metrics, dict):
        raise ValueError('Нет метрик')
    for name in ('accuracy', 'format_rate'):
        value = metrics.get(name)
        if type(value) not in (float, int) or not 0 <= value <= 1 or not math.isfinite(value):
            raise ValueError(f'Некорректная метрика: {name}')


def publish(manifest: dict, report: dict, target: Path) -> None:
    validate_release(manifest, report)
    payload = canonical(manifest) + b'\n'
    if not target.exists() or target.read_bytes() != payload:
        atomic_bytes(target, payload)
