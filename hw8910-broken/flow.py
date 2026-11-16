"""Persistent local Prefect flow. No stage retrains the model."""
import argparse
import hashlib
import json
from pathlib import Path

from prefect import flow, get_run_logger, task
from prefect.cache_policies import NO_CACHE
from prefect.runtime import flow_run
from prefect.settings import PREFECT_LOCAL_STORAGE_PATH, temporary_settings

import journal
import release


def settings(root, mode):
    if mode == 'stub':
        return release.read(root / 'config.json')
    m = release.read(root / 'manifest-candidate.json')
    return dict(tokenizer_revision=m['runtime']['base_revision'],
                tokenizer_sha256=m['runtime']['tokenizer_sha256'],
                template_sha256=m['runtime']['template_sha256'],
                max_length=m['runtime']['max_input_tokens'],
                code_revision=release.file_digest(__file__))


def preparation_key(_context, args):
    return release.cache_key(Path(args['path']), args['config'])


@task(name='validate-input', cache_key_fn=preparation_key, persist_result=True, result_serializer='json')
def prepare(path, config, mode):
    data = Path(path).read_bytes()
    lines = data.decode('utf-8').splitlines()
    if not lines or any(not line.strip() for line in lines):
        raise ValueError('Пустой вход или пустая строка')
    if mode == 'real':
        from evaluation_inputs import check_rows
        check_rows(lines)
    get_run_logger().info('PREPARED rows=%s input_sha256=%s', len(lines), hashlib.sha256(data).hexdigest())
    return dict(input_sha256=hashlib.sha256(data).hexdigest(), rows=len(lines),
                config=config)


@task(name='retrieve-verified-existing-artifact', cache_policy=NO_CACHE)
def retrieve(root, mode):
    name = 'B' if mode == 'stub' else 'candidate'
    m = release.read(root / f'manifest-{name}.json')
    if mode == 'stub':
        release.verify_files(m, root)
    else:
        from model_runtime import verify_runtime
        verify_runtime(m, root)
    get_run_logger().info('Existing artifact %s; no training', m['artifact_uri'])
    return m


@task(name='verify-evaluation-and-gate', cache_policy=NO_CACHE)
def assess(root, mode, m, report_name):
    if mode == 'stub':
        r = release.read(root / report_name)
    else:
        from evaluate_release import check_decision
        r = check_decision(root, m)
    release.atomic_json(root / 'release-report.json', r)
    release.validate_release(m, r)
    return r


@task(name='atomic-publication', cache_policy=NO_CACHE)
def publish(root, manifest, decision, fail_after_pointer):
    if manifest['model_id'].startswith('fixture-'):
        release.verify_files(manifest, root)
    else:
        from model_runtime import verify_runtime
        verify_runtime(manifest, root)
    return journal.commit(root, manifest, decision, fail_after_pointer=fail_after_pointer)


@flow(name='hw8910-release', retries=0)
def release_flow(root: Path, mode='stub', report_name='report-B.json',
                 fail_after_prepare=False, fail_after_pointer=False):
    root = root.resolve()
    path = root / 'input.txt' if mode == 'stub' else root / 'input.jsonl'
    log = get_run_logger()
    pointer = root / 'accepted.json'
    before = release.file_digest(pointer) if pointer.exists() else None
    attempt = dict(run_id=str(flow_run.id), mode=mode, before=before)
    try:
        cfg = settings(root, mode)
        with temporary_settings({PREFECT_LOCAL_STORAGE_PATH: root / 'cache' / 'prefect'}):
            state = prepare(str(path), cfg, mode, return_state=True)
            prepared = state.result()
        attempt.update(preparation_state=state.name, prepared=prepared)
        log.info('PREPARATION_STATE=%s input_sha256=%s', state.name, prepared['input_sha256'])
        if fail_after_prepare:
            raise RuntimeError('Инъекция сбоя после подготовки')
        manifest = retrieve(root, mode)
        if mode == 'real':
            from evaluate_release import hw_inputs
            _, rows, _, _, _, _ = hw_inputs()
            expected = b''.join(release.canonical({'id': r['id'], 'input': r['input']}) + b'\n' for r in rows)
            if release.file_digest(path) != hashlib.sha256(expected).hexdigest():
                raise ValueError('Вход расходится с оценённым golden')
        decision = assess(root, mode, manifest, report_name)
        action = publish(root, manifest, decision, fail_after_pointer)
        attempt.update(action=action, model_id=manifest['model_id'])
        log.info('%s %s', action, manifest['model_id'])
        return attempt
    except Exception as exc:
        attempt.update(action='FAILED_OR_REJECTED', error=str(exc))
        raise
    finally:
        attempt['after'] = release.file_digest(pointer) if pointer.exists() else None
        release.atomic_json(root / 'attempts' / f"{attempt['run_id']}.json", attempt)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--mode', choices=['stub', 'real'], default='stub')
    parser.add_argument('--report', default='report-B.json')
    parser.add_argument('--fail-after-prepare', action='store_true')
    parser.add_argument('--fail-after-pointer', action='store_true')
    a = parser.parse_args()
    release_flow(a.workspace, a.mode, a.report, a.fail_after_prepare, a.fail_after_pointer)
