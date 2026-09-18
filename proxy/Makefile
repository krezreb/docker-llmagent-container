# The proxy is installed separately from dev-agent itself: the root Makefile
# installs the wrapper script, this one installs everything --proxy needs.
#
#   make -C proxy install
#
IMAGE := dev-agent-proxy
CONFIG ?= $(HOME)/.config/dev-agent

DOCKER_BUILD_FLAGS ?=

.PHONY: install build test uninstall

# compose.yml carries no 'build:', so the image has to exist before 'up -d'
# runs; building it here is what makes the installed compose file usable from
# ~/.config/dev-agent, where ./proxy does not exist.
install: build
	install -Dm644 compose.yml $(CONFIG)/compose.proxy.yml
	install -Dm644 skills/egress-policy/SKILL.md \
	    $(CONFIG)/skills/egress-policy/SKILL.md

build:
	docker build $(DOCKER_BUILD_FLAGS) -t $(IMAGE) .

# The evaluation order and the pending queue, the two places where being wrong
# is silent. Needs the image, since pyyaml lives in it and not on the host.
test: build
	docker run --rm --entrypoint python3 $(IMAGE) /app/test_policy.py

# Leaves /state alone: it holds the CA and the rulesets, and regenerating the
# CA means re-trusting it everywhere.
uninstall:
	docker compose -p dev-agent -f $(CONFIG)/compose.proxy.yml down 2>/dev/null || true
	rm -f  $(CONFIG)/compose.proxy.yml
	rm -rf $(CONFIG)/skills/egress-policy
