.PHONY: setup stop logs clean certs init client

COMPOSE = docker compose

.env:
	@cp .env.default .env
	@echo "Created .env from .env.default"

certs:
	@bash scripts/generate-certs.sh

setup: .env certs
	$(COMPOSE) up -d --build db koji-hub
	@echo "Waiting for koji-hub to be healthy..."
	@timeout=120; while [ $$timeout -gt 0 ]; do \
		if $(COMPOSE) exec -T koji-hub curl -sk https://localhost/kojihub >/dev/null 2>&1; then \
			break; \
		fi; \
		sleep 2; \
		timeout=$$((timeout - 2)); \
	done
	@bash scripts/koji-init.sh
	$(COMPOSE) up -d --build koji-builder
	@bash scripts/setup-client.sh
	@echo ""
	@echo "=== Setup complete ==="
	@echo "Web UI:  https://localhost:$${KOJI_HTTPS_PORT:-8443}/koji"
	@echo "Verify:  koji --noauth list-tags"

stop:
	$(COMPOSE) stop

logs:
	$(COMPOSE) logs -f

clean:
	$(COMPOSE) down -v
	rm -rf ssl/
	rm -f .env

init:
	@bash scripts/koji-init.sh

client:
	@bash scripts/setup-client.sh
