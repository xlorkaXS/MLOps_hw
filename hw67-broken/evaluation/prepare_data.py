"""Prepare held-out labelled questions from the frozen HW3 NASA snapshot."""
import csv
import hashlib
import json
from pathlib import Path
from urllib.parse import quote

import torch

from evaluation.contract import validate_golden, write_json
from src.common import digest

FIELDS = [('period', 'pl_orbper', 'суток', 'Орбитальные периоды'),
          ('radius', 'pl_rade', 'радиусов Земли', 'Радиусы планет'),
          ('mass', 'pl_bmasse', 'масс Земли', 'Массы планет'),
          ('distance', 'sy_dist', 'парсек', 'Расстояния систем'),
          ('temperature', 'st_teff', 'К', 'Температуры звёзд')]


def read_lines(path):
    return [json.loads(s) for s in path.read_text().splitlines() if s.strip()]


def main():
    hw3 = Path('../hw3-broken')
    hw4 = Path('../hw4-broken')
    source = hw3 / 'source/exoplanets.csv'
    train = read_lines(hw3 / 'data/train.jsonl')
    test = read_lines(hw3 / 'data/test.jsonl')
    assert digest(hw3 / 'data/train.jsonl') == digest(hw4 / 'data/train.jsonl')
    tensor = torch.load('data/tokenized/train.pt', weights_only=True)
    assert {e['id'] for e in tensor['examples']} == {r['id'] for r in train}
    assert digest('data/tokenized/train.pt') == digest(hw4 / 'data/tokenized/train.pt')
    assert digest('data/tokenized/val.pt') == digest(hw4 / 'data/tokenized/val.pt')
    with source.open() as f:
        archive = {r['pl_name']: r for r in csv.DictReader(f)}
    training = [dict(id=r['id'], input=r['messages'][1]['content'], group_id=r['topic']) for r in train]
    pools = {kind: [] for kind, *_ in FIELDS}
    for row in sorted(test, key=lambda r: r['id']):
        q = row['messages'][1]['content']
        key = ('period' if 'орбитальный период' in q else 'radius' if 'радиус экзопланеты' in q else
               'mass' if 'оценка массы' in q else 'distance' if 'расстоянии' in q else
               'temperature' if 'температура' in q else None)
        if key:
            pools[key].append(row)
    golden, audit = [], []
    seen_groups = set()
    for kind, field, unit, title in FIELDS:
        count = 0
        for row in pools[kind]:
            if row['topic'] in seen_groups:
                continue
            q = row['messages'][1]['content']
            names = [n for n in archive if n in q and q.endswith(n + (' в радиусах Земли?' if kind == 'radius' else ' в массах Земли?' if kind == 'mass' else '?'))]
            name = max(names, key=len)
            original = float(archive[name][field])
            value = format(original, '.4g')
            if value not in row['messages'][2]['content']:
                raise ValueError('Эталон HW3 не соответствует источнику: ' + name)
            correct = int(hashlib.sha256(row['id'].encode()).hexdigest()[:8], 16) % 3
            values = [format(original * .5, '.4g'), format(original * 2, '.4g')]
            values.insert(correct, value)
            assert len(set(values)) == 3
            options = '; '.join(f'{label}: {v} {unit}' for label, v in zip('ABC', values))
            source_url = 'https://exoplanetarchive.ipac.caltech.edu/overview/' + quote(name, safe='')
            golden.append(dict(id='gold-' + row['id'], group_id=row['topic'],
                               input=q + '\nВарианты: ' + options + '. Допустимые метки choices: A, B, C.',
                               expected='ABC'[correct], choices=list('ABC'), slices=[kind], source=source_url))
            audit.append(dict(id=golden[-1]['id'], planet=name, field=field,
                              source_value=archive[name][field], rounded=value,
                              expected=golden[-1]['expected'], options=options,
                              original_id=row['id'], original_answer=row['messages'][2]['content']))
            seen_groups.add(row['topic'])
            count += 1
            if count == 10:
                break
        assert count == 10, kind
    behavioral = [dict(golden[0], id='behavior-format', type='format'),
                  dict(golden[10], id='behavior-paraphrase', type='paraphrase',
                       input='Выбери соответствующее архивному значению: ' + golden[10]['input']),
                  dict(id='behavior-refusal', group_id='out-of-domain-cooking', type='refusal',
                       input='Как приготовить яблочный пирог? Допустимые метки choices: A, B, C, REFUSE.',
                       choices=['A', 'B', 'C', 'REFUSE'], expected='REFUSE', slices=['out_of_domain'],
                       source='Граница темы, заданная system-инструкцией')]
    proof = validate_golden(golden, training, behavioral)
    dest = Path('data/eval')
    dest.mkdir(parents=True, exist_ok=True)
    for name, rows in [('golden', golden), ('training-index', training), ('behavioral', behavioral)]:
        (dest / (name + '.jsonl')).write_text(''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in rows))
    write_json('docs/data-provenance.json', dict(source_sha256=digest(source),
               train_raw_sha256=digest(hw3 / 'data/train.jsonl'), train_tensor_sha256=digest('data/tokenized/train.pt'),
               val_tensor_sha256=digest('data/tokenized/val.pt'), proof=proof, review=audit))
    text = ['# Проверка эталонов до оценки', '', '50 разных вопросов, 50 отдельных звёздных систем из test ДЗ3.',
            'Числа сверены с зафиксированным CSV NASA; округление .4g совпадает с ДЗ3.',
            'Два отвлекающих значения — половина и удвоенное значение; неоднозначности после округления нет.',
            'Позиция эталона A/B/C определяется SHA256 исходного id, а не выходом модели.', '',
            '| ID | Планета | Поле | Исходное число | Эталон | Варианты |', '|---|---|---|---|---|---|']
    text += [f"| {r['id']} | {r['planet']} | {r['field']} | {r['source_value']} | {r['expected']} | {r['options']} |" for r in audit]
    Path('docs/golden-review.md').write_text('\n'.join(text) + '\n')
    print(json.dumps(proof, indent=2))


if __name__ == '__main__':
    main()
