# Pinned to the Python version Odin runs; the DuckDB extension pins in
# docker/install_extensions.py are for linux_amd64 only.
FROM python:3.13.1-slim-bookworm@sha256:031ebf3cde9f3719d2db385233bcb18df5162038e9cda20e64e08f49f4b47a2f

ARG VERSION=unknown

# Turns off buffering for easier container logging
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
# Supress warning that pip running as root user, OK for container
ENV PIP_ROOT_USER_ACTION=ignore
# Supress pip new release warning
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV LANG=C.UTF-8
ENV VERSION=${VERSION}

# Install required project packages
WORKDIR /install/
COPY requirements.txt requirements.txt
RUN test "$(uname -m)" = x86_64 \
    && pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements.txt \
    && rm requirements.txt

# Install and verify the pinned DuckDB extensions
COPY docker/install_extensions.py install_extensions.py
RUN python install_extensions.py && rm install_extensions.py

# Install odout package; dependencies are already installed from the hashed requirements
COPY pyproject.toml pyproject.toml
COPY src/ src/
RUN pip install --no-cache-dir --no-deps . && rm -r build src pyproject.toml

# Create/Use non-root container USER
RUN groupadd --gid 10001 quack \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin quack
USER 10001:10001
ENV HOME=/home/quack
WORKDIR /home/quack

EXPOSE 9494

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9494/', timeout=3).read()"]
CMD ["start-odout"]
