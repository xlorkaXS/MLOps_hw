.PHONY: install source repro v1 v2 diff dag diversity contamination check clean

install:
	uv sync

source:
	uv run python scripts/fetch_source.py

repro:
	uv run dvc repro

v1:
	uv run python scripts/set_version.py v1
	uv run dvc repro

v2:
	uv run python scripts/set_version.py v2
	uv run dvc repro

diff:
	uv run dvc metrics diff

dag:
	uv run dvc dag

diversity:
	uv run python -m src.diversity

contamination:
	uv run python scripts/check_contamination.py

check:
	uv run python -m unittest discover -s tests -p 'test_*.py'
	bash tests/check.sh

clean:
	rm -rf data metrics/*.json params.yaml.bak params.yaml.orig
