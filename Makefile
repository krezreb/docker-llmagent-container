IMAGE := dev-agent:ubuntu24
VARIANTS := $(patsubst images/%.Dockerfile,%,$(wildcard images/*.Dockerfile))

# Aliases belong in the rc file of the shell you actually use: zsh is the
# default on macOS, bash the usual one on Linux. make passes the invoking
# environment's SHELL to sub-shells, so this reads your login shell.
LOGIN_SHELL := $(notdir $(shell echo $$SHELL))
ifeq ($(findstring zsh,$(LOGIN_SHELL)),zsh)
SHELL_RC ?= $(HOME)/.zshrc
else
SHELL_RC ?= $(HOME)/.bashrc
endif

.PHONY: install build-image images aliases $(VARIANTS)

install: build-image
	install -d ~/.local/bin ~/.config/dev-agent
	install -m 755 dev-agent ~/.local/bin/dev-agent
	test -f ~/.config/dev-agent/config.yml || install -m 644 config.yml.example ~/.config/dev-agent/config.yml

build-image:
	docker build -t $(IMAGE) .

images: $(VARIANTS)

$(VARIANTS): %: build-image
	docker build -t dev-agent:$@ -f images/$@.Dockerfile .

# One "dev-agent-<variant>" alias per images/*.Dockerfile, written to $(SHELL_RC)
# between markers so re-running replaces the block instead of appending again.
# The alias syntax is the same in bash and zsh.
aliases:
	@begin='# >>> dev-agent aliases >>>'; \
	end='# <<< dev-agent aliases <<<'; \
	tmp='$(SHELL_RC).dev-agent.tmp'; \
	touch '$(SHELL_RC)'; \
	awk -v b="$$begin" -v e="$$end" \
	    'index($$0, b) { skip = 1 } !skip { print } index($$0, e) { skip = 0 }' \
	    '$(SHELL_RC)' > "$$tmp"; \
	{ \
	  echo "$$begin"; \
	  for v in $(VARIANTS); do \
	    echo "alias dev-agent-$$v=\"DEV_AGENT_IMAGE=dev-agent:$$v dev-agent\""; \
	  done; \
	  echo "$$end"; \
	} >> "$$tmp"; \
	mv "$$tmp" '$(SHELL_RC)'; \
	echo "wrote aliases to $(SHELL_RC): $(addprefix dev-agent-,$(VARIANTS))"; \
	echo "run 'source $(SHELL_RC)' or start a new shell to pick them up"
