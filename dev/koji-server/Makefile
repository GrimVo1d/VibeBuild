.PHONY: setup stop logs clean certs init client regen-repo

COMPOSE = docker compose

.env:
	@cp .env.default .env
	@printf 'POSTGRES_PASSWORD=%s\nKOJIWEB_SECRET=%s\n' "$$(openssl rand -hex 16)" "$$(openssl rand -hex 32)" >> .env
	@printf 'VB_UID=%s\n' "$$(id -u)" >> .env
	@chmod 600 .env
	@echo "Created .env from .env.default with generated secrets"

# Generated in a Fedora container (OpenSSL 3, same as the hub) and handed back to the host user.
certs: .env
	@set -a; . ./.env; set +a; \
	docker run --rm -v "$$PWD":/stand -w /stand -e HOST_UID=$$(id -u) -e HOST_GID=$$(id -g) \
		fedora:$${FEDORA_VERSION:-44} bash -c \
		'dnf -q -y install openssl >/dev/null && bash scripts/generate-certs.sh && chown -R "$$HOST_UID:$$HOST_GID" ssl'

setup: .env certs
	@grep -q '^VB_UID=' .env || printf 'VB_UID=%s\n' "$$(id -u)" >> .env
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
	$(COMPOSE) up -d --build koji-builder vibebuild-dev
	@echo ""
	@echo "=== Setup complete ==="
	@echo "Web UI:  https://localhost:$${KOJI_HTTPS_PORT:-8443}/koji"
	@echo "Client:  docker compose exec vibebuild-dev bash"
	@echo "The first build repo (newRepo task) takes a few minutes: docker compose exec koji-hub koji list-tasks"

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

# Optional: writes ~/.koji/config on the host (needs the koji CLI on the host).
client:
	@bash scripts/setup-client.sh

regen-repo:
	@bash scripts/regen-repo.sh
