# labrat buy rig: live/buyrig_runner.py, which runs live/buyrig.py (headless Chromium + the rat's two networks) once
# per buyback batch and streams the masked pons page to the relay's pons channel. DRY by default: the image holds no
# wallet key and no .env, and the rig signs nothing. LIVE (live/buyrig_live.py) is switched on only by the service's
# variables BUYRIG_LIVE=1, BUYRIG_CONFIRM=LABRAT and BUYBACK_RH_KEY (see live/BUYBACK.md "Going live").
# The build context is the repo root (on Railway: RAILWAY_DOCKERFILE_PATH=buyrig/Dockerfile). buyrig/Dockerfile.dockerignore
# (BuildKit's per-Dockerfile ignore file) lets only the files copied below through.
FROM python:3.13.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# tini is PID 1: it hands SIGTERM to the runner and reaps Chromium's children
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY buyrig/requirements.txt buyrig/requirements.txt
RUN pip install -r buyrig/requirements.txt \
 && python -m playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

# the brain session (its code files are part of the brain commit: copied unchanged), the scene and the two networks
COPY env.py cursor_env.py steer_env.py session.py ptload.py replay_session.py launcher.py ./
COPY assets/ assets/
# (in the image, not on the volume: buyrig.load_brain refuses networks outside the repo folder, symlinks resolved)
COPY runs/final/policy.pt runs/final/steer.pt runs/final/
# the rig: brainrig (the rat's session, lighting, click forwarding), ponsbot (the injected wallet), buyback (the
# pinned pool facts and the read-only RPC), the buy rig and its runner
COPY live/brainrig.py live/ponsbot.py live/buyback.py live/buyrig.py live/buyrig_live.py live/buyrig_runner.py live/
COPY buyrig/ buyrig/

# fail the build, not a session: the imports, the mask and page scripts, both networks
RUN python -c "import sys; sys.path[:0] = ['/app', '/app/live']; import buyrig, buyrig_runner; \
print('buy rig', buyrig.EXECUTE, len(buyrig.MASK_JS))" \
 && python -c "import sys; sys.path[:0] = ['/app', '/app/live']; import buyrig; \
buyrig.load_brain('runs/final/steer.pt', 'runs/final/policy.pt'); print('networks ok')"

# /data (a volume): runs/ (every session's recording and its proof, --out-root), buyrig/runner.json (which batches
# ran, so a redeploy never runs a batch twice) and, in LIVE, buyrig/live_journal.jsonl + buyrig/windows/ (one
# transaction per window, ever; LIVE refuses to run unless /data is a mounted volume)
# BURN_STATUS_URL (the burn engine's /status, live/burn.py) makes the runner execute that engine's booked burns too,
# only with BURN_LIVE=1, BURN_CONFIRM=LABRAT and the key (see live/buyrig_runner.py "BURNS"); BURN_METHOD forces
# burn | transfer (default auto: the token bytecode decides)
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "mkdir -p /data/runs /data/buyrig && exec python live/buyrig_runner.py --state /data/buyrig/runner.json --out-root /data/runs ${BUYRIG_RELAY:+--relay $BUYRIG_RELAY} ${BURN_STATUS_URL:+--burn-status-url $BURN_STATUS_URL} ${BURN_METHOD:+--burn-method $BURN_METHOD}"]
