# Agent2Telegram — container image.
#
# The bridge core has no Python dependencies, so this image is tiny. NOTE: the agent CLI
# you connect (Claude Code / Codex) and its login are NOT baked in — mount
# them or install in a derived image, because each requires interactive authentication
# that must not live in a public image. See README "Docker" for the recommended setup.
FROM python:3.12-slim

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir .

# Config and per-chat state live on a mounted volume so they survive restarts.
ENV AGENT2TELEGRAM_CONFIG=/data/config.json \
    AGENT2TELEGRAM_STATE=/data/state
VOLUME ["/data"]

ENTRYPOINT ["python", "-m", "agent2telegram"]
CMD ["run"]
