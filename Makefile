IMAGE := dev-agent:ubuntu24

.PHONY: install build-image

install: build-image
	install -Dm755 dev-agent ~/.local/bin/dev-agent
	test -f ~/.config/dev-agent/config.yml || install -Dm644 config.yml.example ~/.config/dev-agent/config.yml

build-image:
	docker build -t $(IMAGE) .
