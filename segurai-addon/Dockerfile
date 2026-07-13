ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.20
FROM ${BUILD_FROM}

SHELL ["/bin/sh", "-o", "pipefail", "-c"]

RUN apk add --no-cache bash git openssh-client openssl tmux ttyd

WORKDIR /app
COPY rootfs /
RUN python3 -m pip install --no-cache-dir --upgrade pip \
    && python3 -m pip install --no-cache-dir -r /app/requirements.txt \
    && chmod +x /run.sh /app/segurai.py /usr/local/bin/segurai-terminal.sh

CMD ["/run.sh"]
