# Multi-arch (linux/arm64 for the fleet, linux/amd64 for CI and local runs).
#
# Two-stage so the wheel build never ships: PyYAML and pydantic both compile
# native extensions, and the toolchain to do that is larger than the whole
# runtime image.

FROM python:3.11-slim AS build

# --no-install-recommends and a cleaned apt list, because this layer is only
# here to produce wheels and its size is pure waste otherwise.
RUN apt-get update \
    && apt-get install --no-install-recommends -y build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY retina_telemetry ./retina_telemetry
RUN pip wheel --no-cache-dir --wheel-dir /wheels .


FROM python:3.11-slim AS runtime

LABEL org.opencontainers.image.title="retina-telemetry" \
      org.opencontainers.image.description="Node-side telemetry uplink for the RETINA passive radar fleet" \
      org.opencontainers.image.source="https://github.com/offworldlabs/retina-telemetry"

# Runs as root, deliberately. It reads /proc for host-wide CPU, and /data/mender
# is 0600 root-owned — the node_id and device_type live there. A non-root user
# would need those relaxed, which is a worse trade than root in a container that
# binds no ports and mounts everything else read-only.

COPY --from=build /wheels /wheels
RUN pip install --no-cache-dir --no-index --find-links=/wheels retina-telemetry \
    && rm -rf /wheels

# Unbuffered so `docker logs` shows a crash-looping node's output immediately
# rather than after a 4 KB buffer fills — which for a service that logs a line
# a minute could be hours.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# No HEALTHCHECK. Liveness here is a payload, not a container state: a wedged
# node and a working one look identical to Docker, which is the same reason
# blah2's health is derived from the detection poll rather than the socket.

ENTRYPOINT ["python", "-m", "retina_telemetry"]
