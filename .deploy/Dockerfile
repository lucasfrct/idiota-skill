FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g ask-cli

COPY lambda/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt ask-sdk-local-debug

ENV ASK_DEFAULT_DEVICE_LOCALE=pt-BR
WORKDIR /workspace

CMD ["bash"]
