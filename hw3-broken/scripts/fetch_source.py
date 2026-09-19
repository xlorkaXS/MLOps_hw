#!/usr/bin/env python3
"""Получить тот же снимок PSCompPars из официального TAP API архива."""

import csv
import hashlib
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from src.config import load_params  # noqa: E402

QUERY = (
    "select pl_name,hostname,disc_year,discoverymethod,pl_orbper,pl_rade,"
    "pl_bmasse,sy_dist,st_teff from pscomppars order by pl_name"
)
ENDPOINT = "https://exoplanetarchive.ipac.caltech.edu/TAP/sync"


def main() -> None:
    cfg = load_params()["collect"]
    target = Path(cfg["source"])
    target.parent.mkdir(parents=True, exist_ok=True)
    url = ENDPOINT + "?" + urlencode({"query": QUERY, "format": "csv"})
    request = Request(url, headers={"User-Agent": "MLOps-HW3-exoplanet-dataset/1.0"})
    with tempfile.NamedTemporaryFile(dir=target.parent, suffix=".csv", delete=False) as tmp:
        tmp_path = Path(tmp.name)
        with urlopen(request, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                tmp.write(chunk)
    try:
        digest = hashlib.sha256(tmp_path.read_bytes()).hexdigest()
        with tmp_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = sum(1 for _ in reader)
        if rows < cfg["rows_by_version"]["v2"]:
            raise SystemExit(f"архив вернул только {rows} строк")
        if digest != cfg["source_sha256"]:
            raise SystemExit(
                "архив обновился: SHA-256 снимка изменился на " + digest
                + "; для точного восстановления используйте DVC remote, "
                  "для обновления версии источника измените source_sha256 осознанно"
            )
        tmp_path.replace(target)
        print(f"источник восстановлен: {rows} строк, SHA-256 {digest}")
    finally:
        tmp_path.unlink(missing_ok=True)


if __name__ == "__main__":
    main()
