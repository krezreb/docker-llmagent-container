# Docs

## demo

Scripted version: `expect docs/demo.exp` (see the header of that file).

Or run these commands by hand

cd /tmp
rm -rf docker-llmagent-container

export PS1="dev$ "

asciinema rec demo.cast

docker --version
git clone https://github.com/krezreb/docker-llmagent-container.git
cd docker-llmagent-container
make build-image
make install
make aliases
dev-agent claude .


dev-agent bash --ro
touch scratch.txt
echo hacked >> README.md
touch /tmp/fine && echo '/tmp still writable'

convert to gif with agg


docker run --rm  -v "$PWD:/data"   ghcr.io/asciinema/agg:latest   demo.cast demo.gif
