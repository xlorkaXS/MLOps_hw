"""Demonstrate one intended defect after green without changing the working tree."""
import hashlib
import sys
import unittest
from pathlib import Path

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
import release

release.cache_key = lambda path, _config: hashlib.sha256(str(path).encode()).hexdigest()
suite = unittest.defaultTestLoader.discover(str(root / 'tests'), pattern='test_release.py')
case = next(c for group in suite for group_ in group for c in group_
            if c._testMethodName == 'test_changed_bytes_at_same_path_invalidate_cache')
print('MUTATION: path-only cache_key substituted for this process only', flush=True)
result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite([case]))
raise SystemExit(0 if result.wasSuccessful() else 1)
