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
