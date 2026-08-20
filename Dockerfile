ARG PYTHON_IMAGE=python:3.14-slim@sha256:ce40764625a4ff50df3548277632e7f96c4e77fe75fa848aae9885476e7df5a4
FROM ${PYTHON_IMAGE}

ARG VERSION=0.1.0
LABEL org.opencontainers.image.source="https://github.com/pomponchik/autofission" \
      org.opencontainers.image.description="Capacity-aware Fission autoscaling controller" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.version="${VERSION}"

COPY dist/*.whl /wheels/
RUN python -m pip install --no-cache-dir /wheels/autofission-*.whl && rm -rf /wheels

USER 65532:65532
ENTRYPOINT ["autofission"]
