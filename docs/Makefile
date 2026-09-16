# Recording the demo. Run from this directory.

.PHONY: all rec convert

all: rec convert

# Drive demo.exp under asciinema. Needs expect and a working docker.
#
# MAKELEVEL and friends are dropped: inherited, the `make install` inside the
# recording takes itself for a sub-make and prints "Entering directory" /
# "Leaving directory" banners into the demo.
rec:
	env -u MAKELEVEL -u MAKEFLAGS -u MFLAGS asciinema rec -c "expect demo.exp" demo.cast --overwrite

# Cast to gif, with agg from its own container so nothing extra is installed.
convert:
	docker run --rm -v "$(CURDIR):/data" ghcr.io/asciinema/agg:latest demo.cast demo.gif
