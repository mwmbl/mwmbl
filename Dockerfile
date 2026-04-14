FROM node:hydrogen-bullseye as front-end

COPY front-end /front-end
WORKDIR /front-end
RUN npm install && npm run build


FROM python:3.11.12-bookworm as base

ENV PYTHONFAULTHANDLER=1 \
    PYTHONHASHSEED=random \
    PYTHONUNBUFFERED=1

WORKDIR /app

FROM base as builder

# Install uv
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install Rust toolchain (required by maturin to compile the mwmbl_rank extension)
# Install clang/libclang-dev for bindgen (used by xgboost_lib-sys)
# Install patchelf so maturin can bundle libxgboost.so into the wheel RPATH
RUN apt-get update && \
    apt-get install -y clang libclang-dev patchelf && \
    rm -rf /var/lib/apt/lists/*

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
    | sh -s -- -y --default-toolchain stable
ENV PATH="/root/.cargo/bin:${PATH}"

# Create a /venv directory & environment.
# This directory will be copied into the final stage of docker build.
RUN uv venv /venv

# Copy only the necessary files to build/install the python package.
# mwmbl/resources is needed because idf.rs and wiki.rs embed JSON files at
# compile time via include_str!("../../mwmbl/resources/...").
COPY pyproject.toml uv.lock /app/
COPY mwmbl /app/mwmbl
COPY mwmbl_rank /app/mwmbl_rank

# Working directory is /app
# Use uv to install the mwmbl python package including the mwmbl_rank Rust
# extension. PEP 517/518 allows uv to use maturin as the build backend.
RUN uv pip install --python /venv/bin/python .

# Copy libxgboost.so (prebuilt by the xgb crate) into the mwmbl_rank package
# directory and patch the extension's RPATH so it finds the library at runtime.
RUN SODIR=/venv/lib/python3.11/site-packages/mwmbl_rank && \
    cp /app/mwmbl_rank/target/release/deps/libxgboost.so "$SODIR/" && \
    patchelf --set-rpath '$ORIGIN' "$SODIR/mwmbl_rank.cpython-311-x86_64-linux-gnu.so"

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
