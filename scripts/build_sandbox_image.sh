#!/usr/bin/env bash
# Build the official Ariadne sandbox image (minimal stage).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
TAG="${ARIADNE_SANDBOX_IMAGE:-ariadne-sandbox:minimal}"
docker build \
  -f "$ROOT/docker/sandbox/Dockerfile" \
  --target minimal \
  -t "$TAG" \
  "$ROOT/docker/sandbox"
echo "built $TAG"
