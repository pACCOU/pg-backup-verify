FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends postgresql-client \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY pgbv ./pgbv
RUN pip install --no-cache-dir .

VOLUME ["/backups"]
ENTRYPOINT ["pgbv"]
CMD ["--help"]
