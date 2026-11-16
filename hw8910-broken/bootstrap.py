"""Retain the evaluated original reference; never use candidate PASS as a shortcut."""
import argparse
from pathlib import Path

from evaluate_release import check_decision
from journal import commit, rollback
from model_runtime import verify_runtime
from release import read


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--workspace', type=Path, required=True)
    parser.add_argument('--rollback')
    a = parser.parse_args()
    root = a.workspace.resolve()
    if a.rollback:
        rollback(root, a.rollback, verify_runtime)
    else:
        if (root / 'accepted.json').exists():
            raise SystemExit('Первоначальный указатель уже существует')
        m = read(root / 'manifest-baseline.json')
        decision = check_decision(root, m)
        print(commit(root, m, decision), m['model_id'])
