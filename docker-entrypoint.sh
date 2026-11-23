#!/bin/sh
# Container entrypoint (Render / any Docker host). Runs under tini.
#
# All state lives on ONE persistent disk at $DATA_DIR (default /data). Each
# state directory the scripts own under /app becomes a symlink into the disk,
# so the code (anchored on Path(__file__).parent) writes there unchanged.
# Safe to run on every boot: every step is idempotent.
set -eu

DATA_DIR=${DATA_DIR:-/data}
APP_DIR=/app
mkdir -p "$DATA_DIR"

# --- plain state directories: the disk is the only copy --------------------
for name in runs .cache product_images_for_upload checkpoints url_index \
            batches backups inputs; do
    target="$DATA_DIR/$name"
    link="$APP_DIR/$name"
    mkdir -p "$target"
    if [ -d "$link" ] && [ ! -L "$link" ]; then
        # a real dir baked into the image: keep its files, never overwrite
        # what the disk already holds
        cp -a -n "$link/." "$target/" 2>/dev/null || true
        rm -rf "$link"
    fi
    if [ -L "$link" ] || [ -e "$link" ]; then
        rm -f "$link"
    fi
    ln -s "$target" "$link"
done

# --- clients: config from the image, state from the disk -------------------
# The image copy (git) is the source of truth for client.toml and the tracked
# .xlsx files: the hosted GUI runs in share mode, where taxonomy edits are
# off. The disk keeps the gitignored state (credits.json, backups/, .env).
if [ -d "$APP_DIR/clients" ] && [ ! -L "$APP_DIR/clients" ]; then
    rm -rf "$APP_DIR/clients_seed"
    mv "$APP_DIR/clients" "$APP_DIR/clients_seed"
fi
mkdir -p "$DATA_DIR/clients"
if [ -d "$APP_DIR/clients_seed" ]; then
    cp -a "$APP_DIR/clients_seed/." "$DATA_DIR/clients/"
fi
if [ -L "$APP_DIR/clients" ] || [ -e "$APP_DIR/clients" ]; then
    rm -f "$APP_DIR/clients"
fi
ln -s "$DATA_DIR/clients" "$APP_DIR/clients"

cd "$APP_DIR"
exec python -m streamlit run gui.py \
    --server.port "${PORT:-10000}" \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.fileWatcherType none \
    --browser.gatherUsageStats false
