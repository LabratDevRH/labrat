# labrat trainer: train.py and live/publish_training.py in one container, CPU only (trainer/README.md).
# The build context is the repo root (on Railway: RAILWAY_DOCKERFILE_PATH=trainer/Dockerfile). The root .dockerignore
# lets only the files copied below through, so .env, launch_journal.json and old runs never reach the build.
FROM python:3.13.15-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# tini is PID 1: it hands SIGTERM to entry.py and reaps orphaned processes (e.g. a killed train.py's workers)
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY trainer/requirements.txt trainer/requirements.txt
RUN pip install -r trainer/requirements.txt

COPY train.py policy.py env.py cursor_env.py steer_env.py tiles_env.py ptload.py ./
# the scene, and assets/songs.json (Rat Tiles' public-domain melodies, identical to site/assets/songs.json)
COPY assets/ assets/
COPY live/publish_training.py live/labrat_frame.py live/
COPY live/assets/rat.json live/assets/rat.json
# the final networks; entry.py copies them to the volume's runs/final (steer_env loads runs/final/policy.pt, and
# TRAIN_RESUME=runs/final/steer.pt resumes the steering network). /app/runs becomes a link to /data/runs at start.
COPY runs/final/policy.pt runs/final/steer.pt seed/final/
COPY trainer/ trainer/

# fail the build, not the job: imports, the CPU torch build, both networks, all four envs, the frame packer
RUN python trainer/entry.py --selftest

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "/app/trainer/entry.py"]
