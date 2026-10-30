.PHONY: install test lint demo clean

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	ruff check .

demo:
	bash examples/demo.sh

clean:
	rm -rf demo-workspace .pytest_cache .ruff_cache
