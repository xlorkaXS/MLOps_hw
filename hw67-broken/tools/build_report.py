"""Build the conclusion from measured reports; never alter evaluation inputs."""
import json
from pathlib import Path

from mlflow import MlflowClient
from src.common import connect, load_params


def read(name):
    return json.loads(Path(name).read_text())


def main():
    connect(load_params())
    client = MlflowClient()
    a, b = [read(f'metrics/{role}-eval.json') for role in ('baseline', 'candidate')]
    spec = read('metrics/candidate-model.json')
    aliases = client.get_registered_model(spec['name']).aliases
    state = dict(name=spec['name'], candidate=aliases.get('candidate'), champion=aliases.get('champion'))
    Path('metrics/registry-state.json').write_text(json.dumps(state, indent=2) + '\n')
    diff, gate, negative = [read('metrics/' + name + '.json') for name in ('eval-diff', 'quality-gate', 'negative-gate')]
    golden = {r['id']: r for r in map(json.loads, Path('data/eval/golden.jsonl').read_text().splitlines())}
    aa, bb = [{r['id']: r for r in report['per_item']} for report in (a, b)]
    text = ['# ДЗ6–7: результаты эксперимента и решение о выпуске', '',
            '## Поиск и кандидат', '',
            'Проведён один sweep: шесть конфигураций, четыре шага каждая, train-пул 32, val 8, batch 1 × accumulation 2, seed 42. Менялись r (4/8), lr (1e-4/3e-4) и target_modules (q/v либо все семь проекций). Test не участвовал в выборе. Полная оценка выполнена только для базы и одного выбранного кандидата.', '',
            'Лучший all-r8-high: val loss 1.736899 → 0.829362; отношение 0.4775, порог заранее 1.0. Длительность измеренной обучающей части с проверками — 3.65 с; это не время установки, скачивания, упаковки и всего sweep. Короткий поиск не доказывает устойчивую обобщающую способность. Ресурсы сравниваются по одинаковому бюджету; величина Metal driver memory зависит и от кеширования между последовательными runs.', '',
            f"Candidate: {spec['uri']}; alias candidate = версия {state['candidate']}; run {spec['run_id']}.",
            f"База: {a['model']['name']}@{a['model']['revision']}. Точная спецификация — evaluation/baseline.json.",
            'Код обучения закреплён коммитом bb2742093dabd7ed4f6ad7e21cb1830285e4929b и code_sha256 в каждом run. Git dirty=True отражает сохранённые посторонние изменения старых домашних работ; исходники src дополнительно сохранены в каждом run. train/val имеют отдельные SHA256, входы побайтно совпадают с ДЗ4.', '',
            '## Данные и общие метрики', '',
            'Golden — 50 разных вопросов из 50 отложенных звёздных систем test ДЗ3; не новый независимый аудит. Полный train-index содержит 3178 вопросов. Нормализованных точных пересечений и пересечений group_id нет. Связь с тензорами проверена по id и SHA256; источник, поле, исходное число, округление, варианты и ответ для всех 50 строк приведены в golden-review.md и data-provenance.json. Шаблоны и физические закономерности между системами могут повторяться: автоматическая проверка не доказывает отсутствия всех смысловых утечек.', '',
            '| Версия | n | correct | valid | errors | accuracy | format_rate |', '|---|---:|---:|---:|---:|---:|---:|']
    for name, report in [('baseline', a), ('candidate', b)]:
        m = report['overall']
        text.append(f"| {name} | {m['n']} | {m['correct']} | {m['valid']} | {m['errors']} | {m['accuracy']:.0%} | {m['format_rate']:.0%} |")
    text += ['', 'Ошибки генерации остаются в знаменателе; здесь их ноль. Невалидный JSON у базы — два случая; это не удалённые строки. Accuracy ухудшилась на 10 процентных пунктов при улучшении формата на 4 п.п.', '',
             '## Срезы, закреплённые до оценки', '',
             'Period и mass критические: нужны корректные орбитальные и физические характеристики, а не только формат ответа. Остальные срезы покрывают другие единицы и свойства. Здесь срезы непересекающиеся; в общем случае их n не суммируется.', '',
             '| Срез | n | baseline correct/valid/errors | candidate correct/valid/errors | accuracy до → после |', '|---|---:|---|---|---|']
    for s in a['slices']:
        x, y = a['slices'][s], b['slices'][s]
        text.append(f"| {s} | {x['n']} | {x['correct']}/{x['valid']}/{x['errors']} | {y['correct']}/{y['valid']}/{y['errors']} | {x['accuracy']:.0%} → {y['accuracy']:.0%} |")
    text += ['', f"Исправлено {len(diff['improved'])} вопросов, потеряно {len(diff['regressed'])}; полный список id — metrics/eval-diff.json.",
             'Исправлены: ' + ', '.join(diff['improved']) + '.',
             'Потеряны: ' + ', '.join(diff['regressed']) + '.', '', '## Пять пар ответов', '']
    selected = diff['improved'] + diff['regressed'][:3]
    for i, id_ in enumerate(selected, 1):
        r = golden[id_]
        text += [f'### {i}. {id_}', '', r['input'], '',
                 f"Эталон: {r['expected']}. Baseline: `{aa[id_]['raw']}`. Candidate: `{bb[id_]['raw']}`.",
                 'Кандидат исправил выбор метки.' if id_ in diff['improved'] else 'Кандидат потерял правильную метку при сохранении формата JSON.', '']
    text += ['По всем пяти парам набор ключей и букв может выглядеть почти одинаково, но изменение одной метки меняет правильность. BLEU/ROUGE/BERTScore здесь не заменяют проверку ответа по эталону и могут наградить содержательно неверную строку. Для свободных ответов judge помог бы разобрать единицы, округление и полноту; риски — позиционное смещение, предпочтение стиля, незнание источника и нестабильность. Нужны скрытые имена моделей, A/B и B/A, рубрика и проверка человеком. Платный judge не использовался.', '',
             '## Behavioral и решение о выпуске', '',
             '| Кейс | Baseline valid/correct | Candidate valid/correct |', '|---|---|---|']
    for x, y in zip(a['behavioral']['per_item'], b['behavioral']['per_item']):
        text.append(f"| {x['id']} | {x['valid']}/{x['correct']} | {y['valid']}/{y['correct']} |")
    text += ['', 'Формат JSON соблюдён во всех трёх кейсах, но для зачёта behavioral требуется также правильная метка. Format-кейс и перефраз содержательно не пройдены обеими моделями; отказ на запрос рецепта корректен. Эти три записи не увеличивают n golden.', '',
             f"Гейт: passed={gate['passed']}; причина: {gate.get('reason', '')}. Кроме общей регрессии, критический mass ухудшился с 5/10 до 2/10 и не пройдены два behavioral-кейса. Порог не менялся.",
             f"Отрицательная проверка: rejected={negative['rejected']}; испорчена только копия ответов, не веса или alias.",
             f"Решение: НЕ ПРИНИМАТЬ. Champion: {state['champion']} (не создан). Candidate остаётся доступным. Promote не выполнялся, baseline не обновлялся.",
             'Следующий шаг — исследовать обучение на формате выбора меток, расширить независимую проверку и отдельно утвердить новый протокол. Не дообучать на golden и не выбирать другой run по этому же golden задним числом.', '',
             '## Проверяемые артефакты и доступ', '',
             f"Эксперимент: http://127.0.0.1:5006/#/experiments/1 ; оценка: http://127.0.0.1:5006/#/experiments/2/runs/{b['evaluation_run_id']} . Локальные ссылки доступны при запущенном сервере, преподаватель получает переносимые отчёты в отдельном приложении.",
             'Probe пережил реальное пересоздание контейнера MLflow; SHA256 и run id сохранены в storage-check.json. Make check-all проверяет девять условий и 24 синтетических теста, которые не подменяют измерения модели.',
             'DVC-указатели: eval-data.dvc и train-inputs.dvc. По умолчанию remote локальный ../dvc_remote; на чужой машине он недоступен. Отдельный архив сдачи содержит golden, behavioral, полный training-index и все JSON-отчёты без весов и обучающих тензоров. Для восстановления evaluation данных распаковать data/eval в папку проекта; затем dvc add data/eval обновлять не нужно, контрольные суммы уже закреплены.',
             'Определения единиц и столбцов: https://exoplanetarchive.ipac.caltech.edu/docs/API_PS_columns.html . Числа взяты из сохранённого снимка, не из изменяемой текущей таблицы.', '']
    Path('docs/conclusion.md').write_text('\n'.join(text))


if __name__ == '__main__':
    main()
