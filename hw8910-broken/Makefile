PYTHON ?= .venv/bin/python
.PHONY: check check-contract
check:
	$(PYTHON) -m compileall -q release.py tests
	$(PYTHON) -m unittest discover -s tests -v
check-contract:
	python3 -m unittest discover -s tests -p test_release.py -v
