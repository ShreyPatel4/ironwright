PACK ?= all
export IRONWRIGHT_DATA ?= $(CURDIR)/data

.PHONY: install run test demo bundle serve mcp clean films intro media site deploy preview

install:
	pip install -e ".[dev]"

run:
	ironwright run --pack $(PACK)

test:
	pytest -q

demo: run
	cd web && python -m http.server 8001

# The CI gate behind docs/DEMO-SCRIPT.md: the whole demo path, headless,
# bounded by wall clock. Fails loudly if the one-command narrative breaks.
# Locally: make demo-check IRONWRIGHT="uv run ironwright" PY="uv run python"
IRONWRIGHT ?= ironwright
PY ?= python
demo-check:
	@start=$$(date +%s); \
	$(IRONWRIGHT) run --pack all || exit 1; \
	for p in retail supply_chain banking_aml insurance_claims; do \
	  test -s web/data/$$p.json || { echo "missing export: $$p"; exit 1; }; \
	  $(PY) -c "import json,sys; d=json.load(open('web/data/$$p.json')); sys.exit(0 if d['evals']['passed'] else 1)" \
	    || { echo "evals not green in $$p export"; exit 1; }; \
	done; \
	test -s web/index.html || { echo "showcase missing"; exit 1; }; \
	end=$$(date +%s); took=$$((end-start)); echo "demo path: $$took s"; \
	test $$took -le 300 || { echo "demo path exceeded 300 s"; exit 1; }

bundle:
	ironwright export --pack all --web-dir web
	python scripts/bundle.py

serve:
	ironwright serve --pack $(if $(filter all,$(PACK)),retail,$(PACK))

mcp:
	ironwright mcp --pack $(if $(filter all,$(PACK)),retail,$(PACK))

clean:
	rm -rf data dist site web/data .pytest_cache

films:
	python scripts/film_specs.py && python scripts/build_films.py --video

intro:
	python scripts/build_intro.py --video

media: intro films
	python scripts/build_site.py --collect-media

site:
	ironwright export --pack all --web-dir web
	python scripts/film_specs.py && python scripts/build_films.py && python scripts/build_intro.py
	python scripts/build_site.py

preview: site
	cd site && python -m http.server 8002

deploy: site
	npx wrangler pages deploy site --project-name ironwright --branch main

# Rebuild the console bundle (requires node; the BUILT dist/ is committed so
# runtime needs uv only).
console:
	cd console && npm install --no-fund --no-audit && npm run build
