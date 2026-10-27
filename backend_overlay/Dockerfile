# Build from the actual locally patched installed image, not unpatched ARM 19.1.0.
ARG ARM_BASE=arm-vision-arm:local-beta-8091dbf
FROM ${ARM_BASE}
USER root
COPY backend_overlay /opt/recognition-helper-overlay
RUN python3 /opt/recognition-helper-overlay/apply.py /opt/arm /usr/bin/abcde \
    && python3 -m compileall -q /opt/arm/arm/ripper /opt/arm/arm/api/v1/jobs.py \
    && bash -n /opt/arm/recognition-helper-abcde \
    && command -v ffprobe && command -v flac && command -v cdparanoia
