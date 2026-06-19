.PHONY: validate test bootstrap doctor logs down

validate:
	python3 scripts/render_runtime.py --config config/runtime.yaml --root . --validate-only

test:
	python3 -m pytest

bootstrap:
	./scripts/bootstrap.sh

doctor:
	./scripts/doctor.sh

logs:
	docker compose --env-file .runtime/compose.env logs -f

down:
	docker compose --env-file .runtime/compose.env down
