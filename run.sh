#!/bin/bash
# Yvonta's Body Factory 
# ---------------------
# Author...: Dirk Jan Buter (Yvonta)
# Email....: hello@yvonta.com
# Date.....: 29-07-2026
# License..: CC0 1.0 Universal
#
# (Re)building and running the docker conainer for the Yvonta's Body Factory web API
# 
mkdir -p ./mpfb-assets/clothes/
mkdir -p ./mpfb-assets/hair/
docker stop avatar-service
docker rm avatar-service
# CHANGED: --build-arg CACHEBUST=$(date +%s) gives the Dockerfile's
# "ARG CACHEBUST" a fresh, different value every single build. This
# invalidates Docker's cache starting from that line in the Dockerfile
# (right before COPY server.py generator_core.py ./) onward, forcing
# those two files to always be freshly copied into the image -- fixes a
# real, confirmed bug where the legacy Docker builder reused a stale
# cached layer for that COPY step even after the files had genuinely
# changed on disk. Everything ABOVE that line in the Dockerfile (the
# slow Blender/MPFB2/pip installs) is unaffected and stays cached, so
# this is fast -- unlike a full --no-cache build, which redoes
# everything and was confirmed to take "ages".
docker build --build-arg CACHEBUST=$(date +%s) -t local-avatar-api .
docker run -d -p 8090:8090 --name avatar-service local-avatar-api
