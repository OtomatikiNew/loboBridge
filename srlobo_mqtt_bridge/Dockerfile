ARG BUILD_FROM=alpine:3.19
FROM $BUILD_FROM

RUN apk add --no-cache python3 py3-pip py3-requests

WORKDIR /app
COPY requirements.txt /app/requirements.txt
RUN pip3 install --break-system-packages --no-cache-dir -r /app/requirements.txt

COPY run.sh /run.sh
COPY srlobo_mqtt_bridge /app/srlobo_mqtt_bridge
RUN chmod a+x /run.sh

CMD ["/run.sh"]
