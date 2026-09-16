FROM dev-agent:ubuntu26

# Install the toolchain into an immutable system location.
#
# HOME is overridden for the install because the base image points it at
# /home/agent, which does not exist at build time; rustup refuses to run when
# HOME disagrees with the effective user's home directory.
ENV RUSTUP_HOME=/opt/rust
RUN curl -fsSL https://sh.rustup.rs -o /tmp/rustup.sh \
    && HOME=/root CARGO_HOME=/opt/rust sh /tmp/rustup.sh -y --no-modify-path --profile minimal \
    && rm /tmp/rustup.sh \
    && chmod -R a+rX /opt/rust \
    && /opt/rust/bin/cargo --version

# At runtime cargo needs a writable CARGO_HOME; /opt/rust is read-only.
ENV CARGO_HOME=/home/agent/.cargo \
    PATH=/opt/rust/bin:$PATH
