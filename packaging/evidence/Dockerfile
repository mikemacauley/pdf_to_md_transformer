# Build time may fetch dependencies; execution always uses --network=none.
FROM python:3.13-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 OMP_THREAD_LIMIT=1 OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1
WORKDIR /opt/engine
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --no-cache-dir .
USER 65534:65534
WORKDIR /work
