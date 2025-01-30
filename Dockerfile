FROM docker.io/python:3.10-alpine

WORKDIR /app
COPY aqualogic_mqtt aqualogic_mqtt
COPY requirements.txt requirements.txt

RUN pip install -r requirements.txt

ENTRYPOINT [ "python", "-m", "aqualogic_mqtt.client" ]
CMD ["--help"]

