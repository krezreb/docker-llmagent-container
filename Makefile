IMAGE := dev-agent:ubuntu24
VARIANTS := $(patsubst images/%.Dockerfile,%,$(wildcard images/*.Dockerfile))

.PHONY: install build-image images $(VARIANTS)

install: build-image
	install -Dm755 dev-agent ~/.local/bin/dev-agent
	test -f ~/.config/dev-agent/config.yml || install -Dm644 config.yml.example ~/.config/dev-agent/config.yml

build-image:
	docker build -t $(IMAGE) .

images: $(VARIANTS)

$(VARIANTS): %: build-image
	docker build -t dev-agent:$@ -f images/$@.Dockerfile .
