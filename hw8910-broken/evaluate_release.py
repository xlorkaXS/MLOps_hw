"""Re-evaluate pinned HW67 artifacts through the exact serving runtime."""
import argparse
import gc
import json
import sys
import time
from pathlib import Path

import model_runtime as rt
from release import BINDINGS, atomic_json, file_digest, fingerprint, read

HW67 = Path(__file__).resolve().parents[1] / 'hw67-broken'
sys.path.insert(0, str(HW67))
from evaluation import cli as hw_eval
from evaluation.solution import quality_gate, score


def hw_inputs():
    from contextlib import chdir
    with chdir(HW67):
        return hw_eval.inputs()


def files(path):
    return {p.name: file_digest(p) for p in sorted(path.iterdir()) if p.is_file()}


def reference(root, path):
    return dict(path=path, sha256=file_digest(root / path))


def report_from_raw(root, role, rows, behavioral, protocol, golden, base_spec, run_id, spec):
    outputs = read(root / f'evidence/{role}-outputs.json')
    by_id = {o['id']: o for o in outputs}
    if len(by_id) != len(outputs) or set(by_id) != {r['id'] for r in rows + behavioral}:
        raise ValueError('Сырые ответы неполны')
    result = score(rows, [by_id[r['id']] for r in rows])
    result.update(status='complete', model=spec, protocol_sha256=protocol, golden_sha256=golden,
                  baseline_spec_sha256=base_spec, evaluation_run_id=run_id,
                  behavioral=score(behavioral, [by_id[r['id']] for r in behavioral]))
    return result


def check_decision(root, manifest):
    bundle = read(root / 'evidence/protocol.json')
    cfg, rows, behavior, base, _, old_hash = hw_inputs()
    runtimes = {}
    for role in ('baseline', 'candidate'):
        pinned = read(root / f'manifest-{role}.json')
        rt.verify_runtime(pinned, root)
        runtimes[role] = pinned['runtime_sha256']
    source = bundle['hw67']
    if (source != old_hash or bundle['runtime_sha256'] != runtimes
            or bundle['new'] != fingerprint({'hw67': source, 'runtimes': runtimes})
            or manifest['protocol_sha256'] != bundle['new']
            or manifest['golden_sha256'] != fingerprint(rows)):
        raise ValueError('Протокол/данные изменены; нужна новая оценка')
    source_reports = {}
    for role in ('baseline', 'candidate'):
        pinned = read(root / f'manifest-{role}.json')
        spec = pinned['evaluation_model']
        report = report_from_raw(root, role, rows, behavior, bundle['new'],
                                 fingerprint(rows), fingerprint(base), bundle['run_id'], spec)
        if report != read(root / pinned['evaluation_report']['path']):
            raise ValueError('Отчёт не совпадает с пересчётом из raw')
        source_reports[role] = report
    role = 'baseline' if manifest['kind'] == 'base' else 'candidate'
    expected_manifest = read(root / f'manifest-{role}.json')
    if any(manifest[k] != expected_manifest[k] for k in BINDINGS):
        raise ValueError('Модель не совпадает с оцененной')
    decision = decide(manifest, source_reports, cfg, root, bundle)
    if decision != read(root / expected_manifest['release_report']['path']):
        raise ValueError('Сохранённое решение не совпало с пересчитанным')
    return decision


def decide(manifest, reports, cfg, root, bundle):
    role = 'baseline' if manifest['kind'] == 'base' else 'candidate'
    a, b = (reports[k] for k in ('baseline', 'candidate'))
    try:
        quality_gate(a, b, cfg['critical_slices'])
        passed, reason = True, 'quality_gate PASS'
    except ValueError as exc:
        passed, reason = False, str(exc)
    if role == 'baseline':
        # Initial trusted reference, not a promoted challenger. Keep this policy explicit.
        passed = a['overall']['errors'] == a['behavioral']['overall']['errors'] == 0
        reason = 'Закреплённая исходная baseline; это не PASS кандидата'
    return dict({k: manifest[k] for k in BINDINGS}, status='complete', passed=passed,
                evaluation_run_id=bundle['run_id'], metrics=reports[role]['overall'],
                policy='retained-reference' if role == 'baseline' else 'hw67-quality-gate',
                reason=reason, sources={name: reference(root, f'evidence/{name}.json')
                for name in ('baseline-eval', 'candidate-eval', 'baseline-outputs', 'candidate-outputs')},
                source_eval_run_id=bundle['source_eval_run_id'])


def evaluate(root):
    from datetime import datetime, timezone
    import torch
    root = root.resolve()
    cfg, rows, behavior, base_spec, proof, old_hash = hw_inputs()
    old = {k: read(HW67 / f'submission/reports/{k}-eval.json') for k in ('baseline', 'candidate')}
    candidate_spec = old['candidate']['model']
    adapter_cfg = read(root / 'models/adapter/manifest.json')['model']
    if (adapter_cfg['revision'] != base_spec['revision'] or adapter_cfg['name'] != base_spec['name']
            or not torch.backends.mps.is_available()):
        raise ValueError('Не совпадает база кандидата или недоступен прежний MPS runtime')
    common = dict(base_name=base_spec['name'], base_revision=base_spec['revision'],
                  dtype=adapter_cfg['dtype'], device='mps', packages=rt.versions(), max_input_tokens=1024,
                  inference_code_sha256=file_digest(rt.__file__),
                  generation={**{k: cfg[k] for k in ('seed', 'system', 'max_new_tokens', 'enable_thinking')},
                              'do_sample': False})
    base = rt.snapshot(common)
    common['base_file_hashes'] = files(base)
    manifests = {}
    for role, spec, artifact in [('baseline', base_spec, base),
                                  ('candidate', candidate_spec, root / 'models/adapter')]:
        f = files(artifact)
        runtime = dict(common, tokenizer_sha256=f['tokenizer.json'], template_sha256=rt.template_digest(artifact))
        model_id = base_spec['name'] + '@' + base_spec['revision'] if role == 'baseline' else candidate_spec['name'] + '/' + candidate_spec['version']
        training_run_id = candidate_spec['run_id'] if role == 'candidate' else None
        manifests[role] = dict(schema_version=1, kind=spec['kind'], model_id=model_id,
            runtime=runtime, runtime_sha256=fingerprint(runtime), file_hashes=f, model_sha256=fingerprint(f),
            artifact_uri=f'runs:/{training_run_id}/model' if training_run_id else 'hf://' + model_id,
            training_run_id=training_run_id, golden_sha256=fingerprint(rows), evaluation_model=spec)
    runtimes = {k: v['runtime_sha256'] for k, v in manifests.items()}
    protocol = fingerprint({'hw67': old_hash, 'runtimes': runtimes})
    run_id = 'local-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    bundle = dict(hw67=old_hash, new=protocol, runtime_sha256=runtimes, run_id=run_id,
                  source_eval_run_id=old['candidate']['evaluation_run_id'], leakage=proof)
    atomic_json(root / 'evidence/protocol.json', bundle)
    from release import atomic_bytes, canonical
    atomic_bytes(root / 'input.jsonl', b''.join(canonical({'id': r['id'], 'input': r['input']}) + b'\n' for r in rows))
    reports = {}
    for role, m in manifests.items():
        m['protocol_sha256'] = protocol
        backend = rt.TransformersBackend(m, root)
        outputs = []
        for row in rows + behavior:
            start = time.perf_counter()
            raw, error = '', None
            try:
                raw = backend.generate(row['input'], cfg['max_new_tokens'])['answer']
            except Exception as exc:
                error = f'{type(exc).__name__}: {exc}'
            outputs.append(dict(id=row['id'], raw=raw, error=error, latency_seconds=time.perf_counter()-start))
        del backend
        gc.collect()
        torch.mps.empty_cache()
        atomic_json(root / f'evidence/{role}-outputs.json', outputs)
        report = report_from_raw(root, role, rows, behavior, protocol, fingerprint(rows),
                                 fingerprint(base_spec), run_id, m['evaluation_model'])
        reports[role] = report
        atomic_json(root / f'evidence/{role}-eval.json', report)
        print(role, report['overall'], 'behavioral', report['behavioral']['overall'], flush=True)
    for role, m in manifests.items():
        decision = decide(m, reports, cfg, root, bundle)
        atomic_json(root / f'evidence/{role}-release-report.json', decision)
        m.update(evaluation_report=reference(root, f'evidence/{role}-eval.json'),
                 raw_outputs=reference(root, f'evidence/{role}-outputs.json'),
                 release_report=reference(root, f'evidence/{role}-release-report.json'))
        atomic_json(root / f'manifest-{role}.json', m)
    print('Evaluation run:', run_id, flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    evaluate(parser.parse_args().workspace)
