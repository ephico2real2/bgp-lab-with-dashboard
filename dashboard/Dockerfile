FROM python:3.12-slim

# What this image IS, and which build it is. A Dockerfile cannot know the
# commit it is being built from, so CI passes it — and it is passed ONCE, as a
# build argument that becomes both the OCI label and the value the app serves
# on /api/version. Two independent mechanisms for the same fact would be two
# things to keep in step, and the first time they disagreed the page would be
# confidently wrong about itself.
ARG REVISION=unknown
ARG BUILT=unknown

LABEL org.opencontainers.image.title="bgp-dashboard" \
      org.opencontainers.image.description="Live BGP topology, events and traffic for the four-router lab" \
      org.opencontainers.image.source="https://github.com/ephico2real2/bgp-lab-with-dashboard" \
      org.opencontainers.image.revision="$REVISION" \
      org.opencontainers.image.created="$BUILT"

ENV DASHBOARD_REVISION=$REVISION \
    DASHBOARD_BUILT=$BUILT

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
