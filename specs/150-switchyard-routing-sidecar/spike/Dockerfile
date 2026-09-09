# Spec 150 (#204) spike: switchyard-server needs rustc >= 1.96.1, newer than the
# host toolchain. Building here keeps the host untouched and produces the image
# the sidecar design needs regardless.
FROM rust:1.96-slim AS build
RUN apt-get update && apt-get install -y --no-install-recommends pkg-config libssl-dev ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN cargo install --locked switchyard-server --version 0.2.0 --root /out

# Trixie, not bookworm: the rust:1.96-slim build stage is trixie-based and the
# binary links GLIBC 2.38, which bookworm (2.36) does not have. The image builds
# either way and only fails when you run it.
FROM debian:trixie-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /out/bin/switchyard-server /usr/local/bin/switchyard-server
ENTRYPOINT ["switchyard-server"]
