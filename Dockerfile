FROM python:3.11-slim

WORKDIR /app

# Install agent-shield + LLM SDKs
RUN pip install --no-cache-dir \
    git+https://github.com/AIKing9319/agent-shield.git \
    anthropic \
    openai

# Copy A2A server code only (no CCC internals)
COPY src/tempero/a2a/ ./tempero/a2a/
COPY src/tempero/__init__.py ./tempero/__init__.py

# Create cache dir
RUN mkdir -p /tmp/tempero_cache/stepback

# Environment variables (override at runtime)
ENV A2A_PORT=8420
ENV A2A_LLM_PROVIDER=anthropic
ENV A2A_LLM_MODEL=claude-haiku-4-5-20251001
# A2A_LLM_API_KEY or ANTHROPIC_API_KEY set at runtime

EXPOSE 8420

HEALTHCHECK --interval=30s --timeout=3s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8420/.well-known/agent-card.json')" || exit 1

CMD ["python", "-m", "tempero.a2a.server"]
