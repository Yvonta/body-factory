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
docker stop avatar-service
docker rm avatar-service
docker build -t local-avatar-api .
docker run -d -p 8090:8090 --name avatar-service local-avatar-api
