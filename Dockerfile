FROM ubuntu:22.04
# Yvonta's Body Factory 
# ---------------------
# Author...: Dirk Jan Buter (Yvonta)
# Email....: hello@yvonta.com
# Date.....: 29-07-2026
# License..: CC0 1.0 Universal

ENV DEBIAN_FRONTEND=noninteractive
# 1. Install dependencies (added 'zip' for extension packaging, 'unzip' to
#    extract the MPFB asset packs below -- these are two separate packages
#    on Debian/Ubuntu, 'zip' alone does not provide 'unzip')
RUN apt-get update && apt-get install -y \
    libgl1-mesa-glx \
    libgles2-mesa \
    libglib2.0-0 \
    curl xz-utils python3 python3-pip libxrender1 libxi6 libgconf-2-4 \
    libxxf86vm1 libxfixes3 libgl1-mesa-glx libglu1-mesa libegl1 \
    libxkbcommon0 libsm6 libice6 git zip unzip && rm -rf /var/lib/apt/lists/*
# 2. Install Blender 5.1.2
WORKDIR /opt
RUN curl -L https://download.blender.org/release/Blender5.1/blender-5.1.2-linux-x64.tar.xz | tar -xJ \
    && mv blender-5.1.2-linux-x64 blender
ENV PATH="/opt/blender:${PATH}"
# FIX: server.py sets BLENDER_USER_CONFIG=/root/.config/blender at RUNTIME
# for the subprocess that runs generator_core.py. If the extension gets
# installed during this BUILD under a different config directory (Blender's
# default, unset), the running server's Blender process would look in a
# different place and never see it. Setting this here, before installing
# MPFB, makes the build-time and run-time config directories match.
ENV BLENDER_USER_CONFIG=/root/.config/blender

# 3. Install MPFB2 as a PROPERLY PACKAGED extension (not raw source files).
#
#    FIX (round 2): the previous approach (git clone the source + cp -r
#    into a folder + BLENDER_SYSTEM_EXTENSIONS env var) got MPFB's files
#    onto disk, but a real container test showed --mpfb-live still
#    couldn't find it. Blender's extension system needs a properly
#    packaged .zip (with a manifest) to be explicitly INSTALLED and
#    ENABLED -- raw source copying skips both steps.
#
#    A first fix attempt fetched the "latest" release asset via GitHub's
#    API -- this failed in a real build ("curl: no URL specified!").
#    Root cause, confirmed directly: makehumancommunity/mpfb2's GitHub
#    Releases have NO uploaded assets at all (only auto-generated source
#    tarballs, not a packaged extension) -- fetching via the GitHub API
#    was never going to work here, regardless of parsing method.
#
#    The real distribution channel is MakeHuman Community's own file
#    server, which publishes a permanently-stable "latest" nightly build
#    URL -- no API calls, no dated filenames to track, no rate limits:
RUN curl -L -o /tmp/mpfb2.zip https://files.makehumancommunity.org/plugins/mpfb2-latest.zip
COPY install_mpfb.py /tmp/install_mpfb.py
RUN blender -b --python /tmp/install_mpfb.py -- /tmp/mpfb2.zip
# 3b. Install the visemes02 (15 Meta/Oculus-style visemes) and faceunits01
#     (54 ARKit face units) functional asset packs.
#
#     CONFIDENCE NOTE: MPFB's own "Install asset pack" UI button handles
#     unzipping internally. Rather than guess the destination path (which
#     depends on where MPFB actually got installed by the step above --
#     no longer a fixed /opt/blender_extensions/mpfb location now that
#     it's a proper managed extension install), this locates the REAL
#     mpfb data directory dynamically via `find`, using the confirmed
#     real structure from a successful local run:
#         .../mpfb/data/targets/visemes/viseme_CH.target
#     i.e. look for a directory literally named "mpfb" containing a
#     "data" subdirectory, and extract into that.
RUN MPFB_DATA_DIR=$(find /root/.config/blender -type d -path "*/mpfb/data" | head -1) \
    && if [ -z "$MPFB_DATA_DIR" ]; then \
         echo "[ERROR] Could not locate an mpfb/data directory under /root/.config/blender -- MPFB install may have failed. Listing what IS there:"; \
         find /root/.config/blender -maxdepth 6 -iname "*mpfb*"; \
         exit 1; \
       fi \
    && echo "[INFO] Found MPFB data directory: $MPFB_DATA_DIR" \
    && curl -L -o /tmp/visemes02.zip https://files.makehumancommunity.org/functional/visemes02.zip \
    && curl -L -o /tmp/faceunits01.zip https://files.makehumancommunity.org/functional/faceunits01.zip \
    && unzip -o /tmp/visemes02.zip -d "$MPFB_DATA_DIR/" \
    && unzip -o /tmp/faceunits01.zip -d "$MPFB_DATA_DIR/" \
    && rm -f /tmp/visemes02.zip /tmp/faceunits01.zip /tmp/mpfb2.zip /tmp/install_mpfb.py \
    && find "$MPFB_DATA_DIR" -iname "viseme_*" | head -5
# 6. Python application setup
WORKDIR /app
COPY requirements.txt ./
RUN pip3 install --no-cache-dir -r requirements.txt
COPY extract_face_landmarks.py ./
RUN curl -L -o /app/selfie_segmenter.tflite \
    https://storage.googleapis.com/mediapipe-models/image_segmenter/selfie_segmenter/float16/latest/selfie_segmenter.tflite
COPY remove_background.py ./

# CACHE-BUSTING: everything above this line (Ubuntu packages, Blender
# download, MPFB2 + asset pack install, pip dependencies) is expensive
# and legitimately doesn't change often -- keeping it cached is correct
# and desired. server.py/generator_core.py, on the other hand, change
# constantly during active development, and a real build showed Docker's
# legacy builder reusing a stale cached layer for their COPY step even
# after the files on disk had genuinely changed (confirmed via a
# [STARTUP] log line printing the running file's actual modification
# time, which kept showing an old timestamp after multiple "successful"
# rebuilds). ARG CACHEBUST, given a fresh value on every build (see
# run.sh), invalidates Docker's cache from THIS line onward every time --
# forcing server.py/generator_core.py to always be freshly copied --
# without paying the cost of redoing everything above it (which
# --no-cache would do wholesale, and was confirmed to take "ages").
ARG CACHEBUST=1
COPY server.py generator_core.py ./

# Bake the CC0 clothing asset packs into the image at build time 
RUN mkdir -p /opt/mpfb-assets/clothes
COPY mpfb-assets/clothes/ /opt/mpfb-assets/clothes/
# (or download+unzip the asset pack zips here instead of COPY, if you'd
# rather not vendor them in your repo)
RUN mkdir -p /opt/mpfb-assets/hair
COPY mpfb-assets/hair/ /opt/mpfb-assets/hair/
EXPOSE 8090
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8090"]