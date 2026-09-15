FROM dev-agent:ubuntu24

# Install the toolchain into an immutable system location.
ENV RUSTUP_HOME=/opt/rust
RUN CARGO_HOME=/opt/rust curl -fsSL https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal \
    && chmod -R a+rX /opt/rust

# At runtime cargo needs a writable CARGO_HOME; /opt/rust is read-only.
ENV CARGO_HOME=/home/agent/.cargo \
    PATH=/opt/rust/bin:$PATH
