.PHONY: install test lint demo demo-fleet clean

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	ruff check .

demo:
	bash examples/demo.sh

demo-fleet:
	bash examples/demo-fleet.sh

clean:
	rm -rf demo-workspace demo-workspace-fleet .pytest_cache .ruff_cache
