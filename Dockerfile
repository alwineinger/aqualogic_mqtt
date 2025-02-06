FROM docker.io/python:3.11-alpine AS builder
RUN apk add --update alpine-sdk
RUN pip wheel --wheel-dir /wheels aiohttp

FROM docker.io/python:3.11-alpine

LABEL org.opencontainers.image.description "MQTT adapter for pool controllers"

WORKDIR /app
COPY aqualogic_mqtt aqualogic_mqtt
COPY requirements.txt requirements.txt
COPY --from=builder /wheels /wheels

RUN pip install -r requirements.txt --only-binary aiohttp --find-links /wheels

ENTRYPOINT [ "python", "-m", "aqualogic_mqtt.client" ]
CMD ["--help"]

