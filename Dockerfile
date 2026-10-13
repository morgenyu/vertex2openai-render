# Match the mature console image currently running in the local Docker deployment.
FROM ghcr.io/bad-woman/vertex2openai@sha256:67c110e477d8d92da5e11034d9f3cdf6cedaec437afabcec64cef26710bea21e

WORKDIR /app
COPY render/render_retry.py /app/render_retry.py
COPY render/patch_runtime.py /tmp/render_patch_runtime.py
RUN python /tmp/render_patch_runtime.py /app && mkdir -p /app/data

ENV STATE_DIR=/app/data
EXPOSE 7860

# Keep the existing Render credentials and accept Render's injected PORT.
CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT:-7860}"]
