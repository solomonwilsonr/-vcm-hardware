.PHONY: install test lint demo ui clean

install:
	python3 -m pip install -e ".[dev]"

test:
	python3 -m pytest -q

lint:
	ruff check .

demo:
	bash examples/demo.sh

# One command to a seeded dashboard, already signed in. Needs `pip install -e ".[api]"`.
ui:
	python3 -m vcm_api --demo --open

clean:
	rm -rf demo-workspace .pytest_cache .ruff_cache
