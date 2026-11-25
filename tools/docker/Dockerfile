# Linux / Colab-like test image for streamclipper.
# The repo is not copied in: mount it at /src and media at /data.
#   docker build -t streamclipper-test -f tools/docker/Dockerfile tools/docker
#   docker run --rm -v "<repo>:/src:ro" -v "<media>:/data" streamclipper-test bash -lc "cp -r /src /tmp/src && pip install '/tmp/src[asr,dev]' && streamclipper doctor"
FROM python:3.12-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUTF8=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_ROOT_USER_ACTION=ignore
WORKDIR /work
