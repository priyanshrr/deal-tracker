VENV ?= .venv
PY   ?= $(VENV)/bin/python

.PHONY: install discover fetch filter extract run calibrate test clean

install:
	python3 -m venv $(VENV) && $(PY) -m pip install -q -r requirements.txt

discover:      ## probe every enabled source for a feed
	$(PY) -m dealtracker discover

discover-all:  ## include disabled (tier 4) sources
	$(PY) -m dealtracker discover --all --concurrency 2

fetch:
	$(PY) -m dealtracker fetch --tier 1 --limit 10

filter:
	$(PY) -m dealtracker filter --tier 1 --tier 2 --tier 3 --limit 10

extract:       ## needs ANTHROPIC_API_KEY
	$(PY) -m dealtracker extract --tier 1 --limit 20

extract-cached:
	$(PY) -m dealtracker extract --from-cache data/articles_cache.json --limit 20

run:
	$(PY) -m dealtracker run --limit 40

calibrate:     ## one row per record + neighbours, for picking the threshold
	$(PY) -m dealtracker run --limit 40 --no-dedup

test:
	$(PY) tests/test_dedupe.py && $(PY) tests/test_pipeline.py && $(PY) tests/test_failed_extraction.py && $(PY) tests/test_repair.py && $(PY) tests/test_structured.py

clean:
	rm -rf data/state.sqlite data/articles_cache.json __pycache__ dealtracker/__pycache__
