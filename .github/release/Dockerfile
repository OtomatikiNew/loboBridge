# Release image: the bridge compiled to a native binary with Nuitka.
# The final image contains no Python source of the bridge.
# Build context: srlobo_mqtt_bridge/ (see .github/workflows/publish.yml).
ARG BUILD_FROM

FROM ${BUILD_FROM} AS builder
RUN apk add --no-cache python3 python3-dev py3-pip gcc g++ musl-dev patchelf ccache libffi-dev
WORKDIR /src
COPY requirements.txt /src/requirements.txt
RUN python3 -m venv /venv \
    && /venv/bin/pip install --no-cache-dir -r /src/requirements.txt "nuitka==2.7.*" ordered-set zstandard
COPY srlobo_mqtt_bridge /src/srlobo_mqtt_bridge
RUN /venv/bin/python -m nuitka \
        --mode=standalone \
        --python-flag=-m \
        --assume-yes-for-downloads \
        --output-dir=/out \
        --output-filename=automation-bridge \
        --remove-output \
        srlobo_mqtt_bridge \
    && mv /out/srlobo_mqtt_bridge.dist /app \
    && test -x /app/automation-bridge

FROM ${BUILD_FROM}
COPY --from=builder /app /app
RUN printf '#!/usr/bin/with-contenv sh\nset -eu\nexec /app/automation-bridge\n' > /run.sh \
    && chmod a+x /run.sh
WORKDIR /app
CMD ["/run.sh"]
