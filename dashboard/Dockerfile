FROM python:3.12-slim

# What this image IS. The revision and the build time are passed at build time
# by CI (`--label org.opencontainers.image.revision=...`), because a Dockerfile
# cannot know the commit it is being built from — and without it nothing on a
# running host can say which build it is holding. Measured on the published
# image before this: `docker image inspect --format '{{json .Config.Labels}}'`
# answered `null`, so the only way to identify a container was to compare its
# served files with a checkout, byte for byte.
LABEL org.opencontainers.image.title="bgp-dashboard" \
      org.opencontainers.image.description="Live BGP topology, events and traffic for the four-router lab" \
      org.opencontainers.image.source="https://github.com/ephico2real2/bgp-lab-with-dashboard"

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./

EXPOSE 8080

# 0.0.0.0 is the CONTAINER's namespace, not the host's: what decides who can
# reach this page is the port publish, and both the compose file and
# simple.clab.yml bind it to 127.0.0.1. Narrowing this to one address waits on
# the management-network design (#14, #16) — until the dashboard has an
# interface of its own there is no address here worth binding to.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
