ARG PYTHON_BASE_IMAGE=python:3.12-slim@sha256:09f7da3bc104798d0afb40bc08d23ab2da20a76130cec1f2ef170848f5d85217
FROM ${PYTHON_BASE_IMAGE}

ARG PIP_INDEX_URL=https://pypi.org/simple

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY packages ./packages
COPY apps/api ./apps/api
RUN PIP_INDEX_URL="$PIP_INDEX_URL" python -m pip install --no-cache-dir \
    --constraint ./apps/api/constraints.txt \
    "./packages/providers[all]" \
    ./packages/monitor \
    ./packages/engine \
    ./apps/api

RUN useradd --create-home --uid 10001 quantsieve \
    && mkdir -p /app/data \
    && chown -R quantsieve:quantsieve /app
USER quantsieve

EXPOSE 8000
CMD ["uvicorn", "quantsieve_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
