FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 KASM_USE_TRANSPORT=http KASM_USE_HOST=0.0.0.0
ARG INSTALL_GUAC=0
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
COPY . /src
RUN if [ "$INSTALL_GUAC" = 1 ]; then \
        apt-get update && apt-get install -y --no-install-recommends libatomic1 && \
        pip install --no-cache-dir '/src[guac]' && python -m playwright install --with-deps chromium && \
        rm -rf /var/lib/apt/lists/*; \
    else \
        pip install --no-cache-dir /src; \
    fi && rm -rf /src && useradd --uid 1000 --create-home app
USER app
EXPOSE 8000
CMD ["kasm-use"]
