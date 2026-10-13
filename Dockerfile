# Keep the proxy binary and application image pinned for reproducible builds.
FROM metacubex/mihomo@sha256:baf38d282b785d7037337676714a69e3fdd1f2d9bf748dfd25fce681a624ea74 AS mihomo
FROM ghcr.io/bad-woman/vertex2openai@sha256:67c110e477d8d92da5e11034d9f3cdf6cedaec437afabcec64cef26710bea21e

WORKDIR /app
COPY --from=mihomo /mihomo /usr/local/bin/mihomo
COPY proxy/proxy_pool.py /app/proxy_pool.py
COPY render/entrypoint.py /app/render_entrypoint.py
COPY render/egress.py /app/render_egress.py
COPY render/render_retry.py /app/render_retry.py
COPY render/patch_runtime.py /tmp/render_patch_runtime.py
RUN python /tmp/render_patch_runtime.py /app && mkdir -p /app/data

ENV STATE_DIR=/app/data
ENV PYTHONUNBUFFERED=1
EXPOSE 7860

# Keep the existing Render credentials and accept Render's injected PORT.
CMD ["python", "/app/render_entrypoint.py"]
