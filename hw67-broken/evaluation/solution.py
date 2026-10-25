"""Строгий scorer и fail-closed гейт без допуска регрессии."""
import json
import math


def _summary(items):
    n = len(items)
    correct = sum(x['correct'] for x in items)
    valid = sum(x['valid'] for x in items)
    return dict(n=n, correct=correct, valid=valid,
                errors=sum(x['error'] is not None for x in items),
                accuracy=correct / n if n else 0.0, format_rate=valid / n if n else 0.0)


def _ids(rows):
    ids = [r.get('id') for r in rows]
    if not ids or any(not isinstance(i, str) or not i for i in ids) or len(ids) != len(set(ids)):
        raise ValueError('Нужны непустые уникальные строковые id')
    return set(ids)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Повтор JSON-ключа')
        result[key] = value
    return result


def score(rows, outputs):
    """Верните overall, slices и per_item. Не удаляйте плохие ответы из знаменателя.

    Строгий ответ: JSON с единственным строковым полем answer из choices.
    outputs: [{id, raw, error, latency_seconds}, ...].
    В overall и каждом срезе: n, correct, valid, errors, accuracy, format_rate.
    В per_item: id, raw, parsed, valid, correct, error, slices.
    Несовпадающие/дублирующиеся id и пустой набор должны вызвать ValueError.
    """
    if _ids(rows) != _ids(outputs):
        raise ValueError('ID ответов и заданий не совпадают')
    by_id = {x['id']: x for x in outputs}
    items = []
    for row in rows:
        choices, slices = row.get('choices'), row.get('slices')
        if (not isinstance(choices, list) or not choices or
                any(not isinstance(c, str) for c in choices) or row.get('expected') not in choices or
                not isinstance(slices, list) or not slices or len(slices) != len(set(slices)) or
                any(not isinstance(s, str) or not s for s in slices)):
            raise ValueError('Неверный контракт задания')
        out = by_id[row['id']]
        raw, error = out.get('raw'), out.get('error')
        parsed, valid = None, False
        if error is None and isinstance(raw, str):
            try:
                obj = json.loads(raw, object_pairs_hook=_unique_object)
                if (isinstance(obj, dict) and set(obj) == {'answer'} and
                        isinstance(obj['answer'], str) and obj['answer'] in choices):
                    parsed, valid = obj, True
            except (ValueError, TypeError):
                pass
        items.append(dict(id=row['id'], raw=raw, parsed=parsed, valid=valid,
                          correct=bool(valid and parsed['answer'] == row['expected']),
                          error=error, slices=list(slices)))
    names = sorted({s for x in items for s in x['slices']})
    return dict(overall=_summary(items), per_item=items,
                slices={s: _summary([x for x in items if s in x['slices']]) for s in names})


def _validate_score(report, minimum):
    try:
        items = report['per_item']
        ids = _ids(items)
        if len(ids) < minimum:
            raise ValueError('Недостаточный размер набора')
        for x in items:
            if (type(x['valid']) is not bool or type(x['correct']) is not bool or
                    (x['correct'] and not x['valid']) or (x['error'] is not None and x['valid']) or
                    not isinstance(x['slices'], list) or not x['slices'] or
                    len(x['slices']) != len(set(x['slices'])) or
                    any(not isinstance(s, str) or not s for s in x['slices'])):
                raise ValueError('Противоречивый per_item')
        expected = {s: _summary([x for x in items if s in x['slices']])
                    for s in sorted({s for x in items for s in x['slices']})}
        if set(report['slices']) != set(expected):
            raise ValueError('Наборы срезов не совпадают')
        for actual, wanted in [(report['overall'], _summary(items)),
                               *[(report['slices'][s], v) for s, v in expected.items()]]:
            if set(actual) != set(wanted):
                raise ValueError('Неполные counts/метрики')
            for k, value in wanted.items():
                v = actual[k]
                if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
                    raise ValueError('Неконечная метрика')
                if k in ('n', 'correct', 'valid', 'errors') and type(v) is not int:
                    raise ValueError('Counts должны быть целыми')
                if not math.isclose(v, value, rel_tol=0, abs_tol=1e-12):
                    raise ValueError('Counts/метрики не соответствуют per_item')
        return {x['id']: x for x in items}
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Неполный отчёт') from exc


def quality_gate(baseline, candidate, critical_slices):
    """Верните {passed: True, ...} или вызовите ValueError с причиной отказа.

    Проверьте совместимость протокола/набора/baseline, метрики и counts,
    все id и срезы, общую accuracy/format_rate, критические срезы и behavior.
    Missing, NaN, пустой набор и ошибки генерации означают отказ.
    Пороги задаются до измерения; учебный допуск регрессии равен нулю.
    """
    try:
        for key in ('protocol_sha256', 'golden_sha256', 'baseline_spec_sha256'):
            if not isinstance(baseline[key], str) or not baseline[key] or baseline[key] != candidate[key]:
                raise ValueError('Несовместимый ' + key)
        for report in (baseline, candidate):
            if report['status'] != 'complete' or not isinstance(report['model'], dict) or not report['model']:
                raise ValueError('Неполный отчёт или модель')
        a, b = _validate_score(baseline, 50), _validate_score(candidate, 50)
        if set(a) != set(b) or any(a[i]['slices'] != b[i]['slices'] for i in a):
            raise ValueError('ID или принадлежность к срезам отличаются')
        if len(baseline['slices']) < 3 or set(baseline['slices']) != set(candidate['slices']):
            raise ValueError('Нужно >=3 одинаковых среза')
        for report in (baseline, candidate):
            _validate_score(report['behavioral'], 3)
            if report['overall']['errors'] or report['behavioral']['overall']['errors']:
                raise ValueError('Есть ошибки генерации')
        if {x['id'] for x in baseline['behavioral']['per_item']} != {x['id'] for x in candidate['behavioral']['per_item']}:
            raise ValueError('Behavioral id отличаются')
        for metric in ('accuracy', 'format_rate'):
            if candidate['overall'][metric] < baseline['overall'][metric]:
                raise ValueError('Регрессия общей ' + metric)
        for s in critical_slices:
            if s not in baseline['slices'] or s not in candidate['slices']:
                raise ValueError('Нет критического среза ' + s)
            if candidate['slices'][s]['accuracy'] < baseline['slices'][s]['accuracy']:
                raise ValueError('Регрессия критического среза ' + s)
        behavior = candidate['behavioral']['overall']
        if behavior['correct'] != behavior['n']:
            raise ValueError('Не все behavioral-кейсы пройдены')
        return dict(passed=True, policy='zero regression; all behavioral required',
                    n=candidate['overall']['n'], critical_slices=list(critical_slices))
    except (KeyError, TypeError, AttributeError) as exc:
        raise ValueError('Неполный отчёт допуска') from exc
