FROM node:hydrogen-bullseye as front-end

COPY front-end /front-end
WORKDIR /front-end
RUN npm install && npm run build


FROM python:3.10.2-bullseye as base

ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

WORKDIR /app

FROM base as builder

ENV PIP_DEFAULT_TIMEOUT=100 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

# Create a /venv directory & environment.
# This directory will be copied into the final stage of docker build.
RUN python -m venv /venv

# Copy only the necessary files to build/install the python package
COPY pyproject.toml uv.lock /app/
COPY mwmbl /app/mwmbl

# Working directory is /app
# Use pip to install the mwmbl python package
# PEP 518, PEP 517 and others have allowed for a standardized python packaging API, which allows
# pip to be able to install uv packages.
RUN /venv/bin/pip install pip wheel --upgrade && \
    /venv/bin/pip install .

FROM base as final

RUN apt-get update && apt-get install -y postgresql-client && rm -rf /var/lib/apt/lists/*

# Copy only the required /venv directory from the builder image that contains mwmbl and its dependencies
COPY --from=builder /venv /venv

# Copy the front end build
COPY --from=front-end /front-end/dist /front-end-build

ADD nginx.conf.sigil /app
# ADD app.json /app

# Set up a volume where the data will live
VOLUME ["/data"]

EXPOSE 5000

CMD ["/venv/bin/mwmbl-tinysearchengine"]
