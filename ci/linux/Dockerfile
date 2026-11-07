# Linux CI image for Stira-Strands, on the same OS as GitHub's ubuntu-latest (24.04):
# git, Node 22, PowerShell and Python (3.11 or 3.12 via uv, like actions/setup-python).
# Build: docker build --build-arg PY=3.11 -t stira-ci:py3.11 .
FROM ubuntu:24.04
ARG PY=3.11
ENV DEBIAN_FRONTEND=noninteractive PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONUNBUFFERED=1 \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python UV_LINK_MODE=copy PATH=/opt/uvbin:$PATH
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl ca-certificates gnupg jq xz-utils unzip procps lsof build-essential \
 && curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && curl -fsSL -o /tmp/msprod.deb https://packages.microsoft.com/config/ubuntu/24.04/packages-microsoft-prod.deb \
 && dpkg -i /tmp/msprod.deb && rm /tmp/msprod.deb \
 && apt-get update && apt-get install -y --no-install-recommends powershell \
 && rm -rf /var/lib/apt/lists/*
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/opt/uvbin sh \
 && uv python install ${PY} && ln -s "$(uv python find ${PY})" /opt/uvbin/python && python --version
RUN userdel -r ubuntu 2>/dev/null; useradd -m -u 1000 ci && chmod -R a+rX /opt/uv-python
USER ci
WORKDIR /work
