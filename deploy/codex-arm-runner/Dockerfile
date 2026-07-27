ARG BASE_IMAGE=public.ecr.aws/docker/library/debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG CODEX_VERSION=0.144.4
ARG CODEX_SHA256=4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2
ARG BWRAP_SHA256=d463266e99d040914973ce082c0b3a6f7c9df39e60ed2440ac47331bfa352a16

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq libcap2 ripgrep \
    && curl -fsSL \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-aarch64-unknown-linux-musl.tar.gz" \
      -o /tmp/codex.tar.gz \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum --check - \
    && tar -xzf /tmp/codex.tar.gz -C /usr/local/bin \
    && mv /usr/local/bin/codex-aarch64-unknown-linux-musl /usr/local/bin/codex \
    && curl -fsSL \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/bwrap-aarch64-unknown-linux-musl.tar.gz" \
      -o /tmp/bwrap.tar.gz \
    && echo "${BWRAP_SHA256}  /tmp/bwrap.tar.gz" | sha256sum --check - \
    && mkdir -p /usr/local/bin/codex-resources \
    && tar -xzf /tmp/bwrap.tar.gz -C /usr/local/bin/codex-resources \
    && mv /usr/local/bin/codex-resources/bwrap-aarch64-unknown-linux-musl /usr/local/bin/codex-resources/bwrap \
    && ln -s /usr/local/bin/codex-resources/bwrap /usr/bin/bwrap \
    && chmod 0555 /usr/local/bin/codex /usr/local/bin/codex-resources/bwrap \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/codex.tar.gz /tmp/bwrap.tar.gz

COPY config.toml /etc/codex/config.toml
COPY codex-probe.sh /usr/local/bin/codex-probe

RUN chmod 0444 /etc/codex/config.toml \
    && chmod 0555 /usr/local/bin/codex-probe

RUN useradd --create-home --uid 10001 codex

USER codex
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/codex-probe"]
