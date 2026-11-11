FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -g 10001 mailagent \
    && useradd -u 10001 -g mailagent --no-create-home --shell /usr/sbin/nologin mailagent

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY mailcore.py providers.py main.py ./
RUN mkdir -p /app/data && chown -R 10001:10001 /app/data
USER 10001:10001
CMD ["python", "main.py", "--loop"]
