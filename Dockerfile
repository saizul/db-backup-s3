FROM python:3.12-alpine3.21

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 WORK_DIR=/tmp

# Database clients (pg_dump 17 can dump PostgreSQL 9.2-17; mariadb-dump works with MariaDB and MySQL),
# age for public-key encryption and tzdata for SCHEDULE_TZ.
RUN apk add --no-cache postgresql17-client mariadb-client age tzdata

WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install . \
    && rm -rf /app/build \
    && adduser -D -H -u 10001 backup

USER backup
WORKDIR /tmp
ENTRYPOINT ["db-backup-s3"]
CMD ["run"]

LABEL org.opencontainers.image.source="https://github.com/saizul/db-backup-s3" \
      org.opencontainers.image.licenses="MIT"
