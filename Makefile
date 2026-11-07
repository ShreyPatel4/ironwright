PACK ?= all
export IRONWRIGHT_DATA ?= $(CURDIR)/data

.PHONY: install run test demo demo-check bundle serve mcp clean films intro media site deploy preview console cf-check

install:
	pip install -e ".[dev]"

run:
	ironwright run --pack $(PACK)

test:
	pytest -q

demo: run
	cd web && python -m http.server 8001 --bind 127.0.0.1

# The CI gate behind docs/DEMO-SCRIPT.md: the whole demo path, headless,
# bounded by wall clock. Fails loudly if the one-command narrative breaks.
# Locally: make demo-check IRONWRIGHT="uv run ironwright" PY="uv run python"
IRONWRIGHT ?= ironwright
PY ?= python
demo-check:
	@start=$$(date +%s); \
	$(IRONWRIGHT) run --pack all || exit 1; \
	for p in retail supply_chain banking_aml insurance_claims insurance_policy; do \
	  test -s web/data/$$p.json || { echo "missing export: $$p"; exit 1; }; \
	  $(PY) -c "import json,sys; d=json.load(open('web/data/$$p.json')); sys.exit(0 if d['evals']['passed'] else 1)" \
	    || { echo "evals not green in $$p export"; exit 1; }; \
	done; \
	test -s web/index.html || { echo "showcase missing"; exit 1; }; \
	IRONWRIGHT_DEV_OPEN=1 $(PY) scripts/demo_console_check.py || exit 1; \
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
	cd site && python -m http.server 8002 --bind 127.0.0.1

deploy: site
	npx wrangler pages deploy site --project-name ironwright --branch main

# Build the console bundle (requires node). dist/ is gitignored, so a checkout needs this
# once, and again after console/src changes. `ironwright platform` warns when it is stale.
console:
	cd console && npm install --no-fund --no-audit && npm run build

# The Cloudflare container, locally: the repo image, started the way the Worker starts it, under
# coconutlabs.org's prefix, on compose Postgres and versitygw. Then the hosted smoke walks it.
# CI runs this. Needs docker and node.
CF_PREFIX = /projects/ironwright/app
CF_COMPOSE = docker compose -f deploy/compose.yaml -f deploy/cloudflare/compose.cf.yaml
CF_START_JS = import { START } from './deploy/cloudflare/src/edge.js'; process.stdout.write(START)
cf-check:
	cd deploy/cloudflare && node test.mjs
	@export CF_START="$$(node --input-type=module -e "$(CF_START_JS)")"; \
	$(CF_COMPOSE) build && $(CF_COMPOSE) up -d || exit 1; \
	trap '$(CF_COMPOSE) logs --no-color --tail 80 api; $(CF_COMPOSE) down -v' EXIT; \
	n=0; until curl -sf http://127.0.0.1:8000$(CF_PREFIX)/readyz >/dev/null; do \
	  n=$$((n+1)); test $$n -le 150 || { echo "not ready under $(CF_PREFIX)"; exit 1; }; sleep 2; done; \
	curl -sf http://127.0.0.1:8000/readyz >/dev/null || { echo "the root no longer answers"; exit 1; }; \
	$(PY) deploy/hosted_smoke.py --base http://127.0.0.1:8000 --base-path $(CF_PREFIX) --wait 60 --packs-wait 600
