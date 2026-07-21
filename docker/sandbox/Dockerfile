# Ariadne personal sandbox image (Codex-aligned: full Linux toolchains in a container).
# Build: scripts/build_sandbox_image.sh
# Tag:   ariadne-sandbox:minimal

FROM python:3.13-slim-bookworm AS minimal

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        coreutils \
        curl \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*

# Non-root agent user (uid/gid 1000) — matches DockerSandboxConfig.user default
RUN groupadd -g 1000 agent \
    && useradd -m -u 1000 -g 1000 -s /bin/bash agent

USER agent
WORKDIR /workspace
CMD ["sleep", "infinity"]
