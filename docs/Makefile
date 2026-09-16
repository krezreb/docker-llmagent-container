# Recording the demo. Run from this directory.

.PHONY: all rec convert

all: rec convert

# Drive demo.exp under asciinema. Needs expect and a working docker.
rec:
	asciinema rec -c "expect demo.exp" demo.cast --overwrite

# Cast to gif, with agg from its own container so nothing extra is installed.
convert:
	docker run --rm -v "$(CURDIR):/data" ghcr.io/asciinema/agg:latest demo.cast demo.gif
