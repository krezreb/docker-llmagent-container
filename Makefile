IMAGE := dev-agent:ubuntu24
VARIANTS := $(patsubst images/%.Dockerfile,%,$(wildcard images/*.Dockerfile))
BASHRC ?= $(HOME)/.bashrc

.PHONY: install build-image images aliases $(VARIANTS)

install: build-image
	install -Dm755 dev-agent ~/.local/bin/dev-agent
	test -f ~/.config/dev-agent/config.yml || install -Dm644 config.yml.example ~/.config/dev-agent/config.yml

build-image:
	docker build -t $(IMAGE) .

images: $(VARIANTS)

$(VARIANTS): %: build-image
	docker build -t dev-agent:$@ -f images/$@.Dockerfile .

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
