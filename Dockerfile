# Container image for the agentsync MCP server.
#
# Exists so registries and sandboxes (e.g. Glama) can build and introspect this
# server rather than guessing at an image. Introspection only needs the server
# to start and answer tools/list, which is pure protocol and touches neither
# git nor the network.
#
# Actually USING the tools needs a board: set AGENTSYNC_BOARD_REPO to a clone
# holding the coordination branch, and AGENTSYNC_AGENT_ID to this agent's id.
# git is installed because every write is a git read-modify-write with push as
# the compare-and-swap.

FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, so a source-only change does not re-resolve them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Unbuffered: stdio IS the transport, so a buffered reply looks like a hang.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Required for the tools to do real work; unset is fine for introspection,
# where the server starts and serves tools/list without touching a board.
ENV AGENTSYNC_AGENT_ID="" \
    AGENTSYNC_BOARD_REPO="" \
    AGENTSYNC_BRANCH="agentsync"

CMD ["python", "agentsync_server.py"]
