FROM python:3.13-alpine

WORKDIR /app
COPY pyproject.toml README.md ./
COPY dvrbridge ./dvrbridge
RUN pip install --no-cache-dir .

# drop root: the daemon needs no privileges and binds only 8554 (unprivileged)
RUN adduser -D -H -u 10001 dvrbridge
USER dvrbridge

# config is mounted read-only at /config/dvrbridge.toml (holds the DVR password)
VOLUME /config
EXPOSE 8554

ENTRYPOINT ["dvrbridge"]
CMD ["serve", "-c", "/config/dvrbridge.toml"]
