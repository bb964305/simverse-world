ARG BASE_IMAGE=public.ecr.aws/docker/library/debian:bookworm-slim
FROM ${BASE_IMAGE}

ARG CODEX_VERSION=0.144.4
ARG CODEX_SHA256=4d07243ef4ae6786b8b321d7aea3f9be4e1d2c597ae5407e7c1b9873334082b2

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git jq ripgrep \
    && curl -fsSL \
      "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/codex-aarch64-unknown-linux-musl.tar.gz" \
      -o /tmp/codex.tar.gz \
    && echo "${CODEX_SHA256}  /tmp/codex.tar.gz" | sha256sum --check - \
    && tar -xzf /tmp/codex.tar.gz -C /usr/local/bin \
    && mv /usr/local/bin/codex-aarch64-unknown-linux-musl /usr/local/bin/codex \
    && chmod 0555 /usr/local/bin/codex \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* /tmp/codex.tar.gz

COPY config.toml /etc/codex/config.toml
COPY codex-probe.sh /usr/local/bin/codex-probe

RUN chmod 0444 /etc/codex/config.toml \
    && chmod 0555 /usr/local/bin/codex-probe

RUN useradd --create-home --uid 10001 codex

USER codex
WORKDIR /workspace

ENTRYPOINT ["/usr/local/bin/codex-probe"]
