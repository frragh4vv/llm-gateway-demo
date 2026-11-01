# Image for the mock provider and the ops dashboard (not the gateway itself;
# that is the official LiteLLM image, pinned by digest in docker-compose.yml).
FROM python:3.11-slim@sha256:673719e071ecf27434fd2635bbd8d00f49f4fdd81f243143438a42ced8bedf60
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements-app.lock .
RUN pip install --no-cache-dir --require-hashes -r requirements-app.lock
COPY mock_provider ./mock_provider
COPY dashboard ./dashboard
RUN useradd --create-home app && mkdir -p /app/logs && chown app /app/logs
USER app
