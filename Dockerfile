FROM docker.io/python:3.10-alpine

LABEL org.opencontainers.image.description "MQTT adapter for pool controllers"

WORKDIR /app
COPY aqualogic_mqtt aqualogic_mqtt
COPY requirements.txt requirements.txt

RUN pip install -r requirements.txt

ENTRYPOINT [ "python", "-m", "aqualogic_mqtt.client" ]
CMD ["--help"]

