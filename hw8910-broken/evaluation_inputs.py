"""Strict, lightweight validation for the real preparation task."""
import json


def check_rows(lines):
    ids = set()
    for line in lines:
        row = json.loads(line)
        if (not isinstance(row, dict) or not isinstance(row.get('id'), str)
                or not row['id'] or row['id'] in ids or not isinstance(row.get('input'), str)
                or not row['input'].strip() or len(row['input']) > 4000):
            raise ValueError('Неверная схема или повтор id')
        ids.add(row['id'])
