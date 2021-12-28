FROM python:3.9-slim-bullseye as base

ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

WORKDIR /app

FROM base as builder

ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    POETRY_VERSION=1.1.12

# RUN apk add --no-cache gcc libffi-dev musl-dev postgresql-dev
RUN pip install "poetry==$POETRY_VERSION"
RUN python -m venv /venv

COPY pyproject.toml poetry.lock ./
RUN poetry export -f requirements.txt | /venv/bin/pip install -r /dev/stdin

COPY . .
RUN poetry build && /venv/bin/pip install dist/*.whl

FROM base as final

#RUN apk add --no-cache libffi libpq
COPY --from=builder /venv /venv
COPY data /data
#COPY docker-entrypoint.sh wsgi.py ./
#CMD ["./docker-entrypoint.sh"]

CMD ["/venv/bin/python", "-m", "mwmbl.tinysearchengine.app", "/data/index.tinysearch"]
