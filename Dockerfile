FROM public.ecr.aws/docker/library/python:3.12-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0

COPY pyproject.toml README.md LICENSE ./
COPY src/ ./src/
RUN pip install . && \
    groupadd --system --gid 1001 matrix && \
    useradd --system --uid 1001 --gid matrix --no-create-home matrix && \
    chown -R matrix:matrix /app

USER matrix

EXPOSE 3300

CMD ["python", "-m", "matrix_mcp"]
