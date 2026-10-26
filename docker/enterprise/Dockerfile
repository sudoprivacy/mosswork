# Build the application from this checkout first; never use a historical patch image.
ARG APP_IMAGE=moss-server-base:source
FROM ${APP_IMAGE}
LABEL org.opencontainers.image.title="moss-server"
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/moss-browser
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && python -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 1000 mosswork && useradd -u 1000 -g mosswork -d /data mosswork \
    && chown -R mosswork:mosswork /data
COPY docker/enterprise/verify_runtime.py /usr/local/libexec/mosswork/verify_runtime.py
USER 1000:1000
# Verify the actual non-root browser launch as well as embedding imports, offline.
RUN --network=none python /usr/local/libexec/mosswork/verify_runtime.py
# Bootstrap is an explicit one-off command, including on PostgreSQL installations.
ENTRYPOINT []
CMD ["moss-server", "run", "--host", "0.0.0.0", "--port", "8088"]
