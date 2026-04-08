FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV VIRTUAL_ENV=/opt/jaster-venv
ENV PATH="/opt/jaster-venv/bin:${PATH}"

# Pin Kali to the official rolling mirror and enable apt retries for flaky networks.
RUN printf '%s\n' \
    'deb http://kali.download/kali kali-rolling main contrib non-free non-free-firmware' \
    > /etc/apt/sources.list && \
    printf '%s\n' \
    'Acquire::Retries "5";' \
    'Acquire::http::Timeout "30";' \
    'Acquire::https::Timeout "30";' \
    > /etc/apt/apt.conf.d/80-retries

# Base system, Python toolchain, and the security tools referenced by skills/*.json.
RUN apt-get update && apt-get install -y --fix-missing --no-install-recommends \
    bash \
    zsh \
    ca-certificates \
    curl \
    wget \
    git \
    jq \
    ripgrep \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    ruby \
    ruby-dev \
    libffi-dev \
    libssl-dev \
    nmap \
    ffuf \
    whatweb \
    dnsrecon \
    exploitdb \
    netexec \
    ldap-utils \
    sqlmap \
    sslscan \
    smbclient \
    netcat-openbsd \
    hydra \
    wpscan \
    && rm -rf /var/lib/apt/lists/*

# Python runtime and CLI tools live in an isolated venv to avoid conflicts with Kali's system packages.
RUN python3 -m venv "${VIRTUAL_ENV}" && \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir --upgrade pip setuptools wheel hatchling && \
    "${VIRTUAL_ENV}/bin/pip" install --no-cache-dir \
    httpx \
    pydantic \
    typer \
    pytest \
    git-dumper \
    impacket

WORKDIR /app

# The repository is expected to be bind-mounted to /app at runtime.
# Typical first-run steps inside the container:
#   pip install -e .
#   jaster contest run
CMD ["bash"]
