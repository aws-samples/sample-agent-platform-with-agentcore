# Sandbox kernel: a one-shot code sandbox for Amazon Bedrock AgentCore Runtime.
# AgentCore Runtime executes linux/arm64 images. IMAGE_VERSION is what a
# sandbox reports in build_info, so a caller can prove which image it runs:
#   docker buildx build --platform linux/arm64 --build-arg IMAGE_VERSION=v1 -t sandbox-kernel .
#
# This is the platform's reference image. A customer image replaces it by
# implementing the same /invocations contract, claiming included (see
# src/control_server.py); the platform pins it through a runtime endpoint.
# nosemgrep: dockerfile-source-not-pinned
FROM python:3.13-slim
# checkov:skip=CKV_DOCKER_2: AgentCore Runtime health-checks via the /ping HTTP contract, not Docker HEALTHCHECK.

# The numeric stack sandboxed jobs commonly need, as native arm64 wheels.
RUN pip install --no-cache-dir numpy==2.3.4 pandas==2.3.3 pyarrow==21.0.0

COPY src/control_server.py /opt/control/control_server.py

ARG IMAGE_VERSION=dev
RUN python -c "import json, sys; json.dump({'image_version': sys.argv[1], 'kernel': 'sandbox-kernel'}, open('/opt/VERSION.json', 'w'))" "${IMAGE_VERSION}" \
    && useradd --create-home --uid 10001 sandbox \
    && mkdir -p /workspace && chown sandbox:sandbox /workspace

# CONTROL_PRELOAD: what the forkserver imports before the snapshot. It only
# runs when the runtime sets CONTROL_FORKSERVER=1, which Terraform does for a
# Fast Runtime (AgentCore platform V2); a Standard Runtime ignores it.
ENV WORKSPACE_ROOT=/workspace \
    PYTHONUNBUFFERED=1 \
    OMP_NUM_THREADS=1 \
    OPENBLAS_NUM_THREADS=1 \
    CONTROL_PRELOAD=numpy,pandas,pyarrow

USER sandbox
WORKDIR /workspace
EXPOSE 8080
CMD ["python", "/opt/control/control_server.py"]
