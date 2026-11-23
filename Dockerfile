# Final_pipe Streamlit wizard (gui.py) for Linux hosts such as Render.
# Guide: docs/RENDER_DEPLOY.html. State lives on a disk at $DATA_DIR (/data);
# docker-entrypoint.sh links the state directories into it on every boot.
# The minor version matches the Windows dev venv (.venv: Python 3.14).
FROM python:3.14-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    STREAMLIT_BROWSER_GATHER_USAGE_STATS=false \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends tini ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# dependencies first, so a code-only change reuses this layer
# (playwright installs as a package; its browsers are NOT installed, so
# --js-fallback has no browser here - share mode drops that flag anyway)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chmod +x /app/docker-entrypoint.sh

EXPOSE 10000
ENTRYPOINT ["tini", "--", "/app/docker-entrypoint.sh"]
