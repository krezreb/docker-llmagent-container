IMAGE := dev-agent:ubuntu26
VARIANTS := $(patsubst images/%.Dockerfile,%,$(wildcard images/*.Dockerfile))
BASHRC ?= $(HOME)/.bashrc

# What npm currently calls the latest release of an agent, or nothing at all
# when the registry cannot be reached.
npm_latest = $(shell curl -fsS "https://registry.npmjs.org/-/package/$(1)/dist-tags" 2>/dev/null \
	| sed -n 's/.*"latest":"\([^"]*\)".*/\1/p' \
	| grep -E '^[0-9][0-9A-Za-z.+-]*$$')

# Asking for the exact versions rather than 'latest' is what makes docker
# rebuild the layer that installs the agents when either publishes, and keep
# the cache when neither has. Name a version yourself to pin one, and fall
# back to 'latest' - and so to the cache - when there is no answer.
CLAUDE_CODE_VERSION ?= $(or $(call npm_latest,@anthropic-ai%2fclaude-code),latest)
CODEX_VERSION ?= $(or $(call npm_latest,@openai%2fcodex),latest)

# 'make build-image DOCKER_BUILD_FLAGS=--no-cache' rebuilds everything.
DOCKER_BUILD_FLAGS ?=

.PHONY: install build-image images aliases autobuild-schedule $(VARIANTS)

install: build-image
	install -Dm755 dev-agent ~/.local/bin/dev-agent
	test -f ~/.config/dev-agent/config.yml || install -Dm644 config.yml.example ~/.config/dev-agent/config.yml

build-image:
	docker build $(DOCKER_BUILD_FLAGS) \
	    --build-arg CLAUDE_CODE_VERSION=$(CLAUDE_CODE_VERSION) \
	    --build-arg CODEX_VERSION=$(CODEX_VERSION) \
	    -t $(IMAGE) .

images: $(VARIANTS)

$(VARIANTS): %: build-image
	docker build $(DOCKER_BUILD_FLAGS) -t dev-agent:$@ -f images/$@.Dockerfile .

# A user service that rebuilds every image, started by dev-agent on its first
# run of the day (see the autobuild block in the script). A unit rather than a
# background job so the build survives closing the terminal and its output ends
# up in 'journalctl --user -u dev-agent-autobuild'.
UNIT_DIR ?= $(HOME)/.config/systemd/user
MAKE_BIN := $(shell command -v $(MAKE))

autobuild-schedule:
	@install -d '$(UNIT_DIR)'
	@printf '%s\n' \
	    '[Unit]' \
	    'Description=Rebuild dev-agent images' \
	    '' \
	    '[Service]' \
	    'Type=oneshot' \
	    'WorkingDirectory=$(CURDIR)' \
	    'ExecStart=$(MAKE_BIN) images' \
	    'TimeoutStartSec=infinity' \
	    > '$(UNIT_DIR)/dev-agent-autobuild.service'
	systemctl --user daemon-reload
	@echo "installed $(UNIT_DIR)/dev-agent-autobuild.service"
	@echo "dev-agent starts it once a day; follow it with 'journalctl --user -fu dev-agent-autobuild'"

# One "dev-agent-<variant>" alias per images/*.Dockerfile, written to $(BASHRC)
# between markers so re-running replaces the block instead of appending again.
aliases:
	@begin='# >>> dev-agent aliases >>>'; \
	end='# <<< dev-agent aliases <<<'; \
	tmp='$(BASHRC).dev-agent.tmp'; \
	touch '$(BASHRC)'; \
	awk -v b="$$begin" -v e="$$end" \
	    'index($$0, b) { skip = 1 } !skip { print } index($$0, e) { skip = 0 }' \
	    '$(BASHRC)' > "$$tmp"; \
	{ \
	  echo "$$begin"; \
	  for v in $(VARIANTS); do \
	    echo "alias dev-agent-$$v=\"DEV_AGENT_IMAGE=dev-agent:$$v dev-agent\""; \
	  done; \
	  echo "$$end"; \
	} >> "$$tmp"; \
	mv "$$tmp" '$(BASHRC)'; \
	echo "wrote aliases to $(BASHRC): $(addprefix dev-agent-,$(VARIANTS))"; \
	echo "run 'source $(BASHRC)' or start a new shell to pick them up"
