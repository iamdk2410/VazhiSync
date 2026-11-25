# PostGIS + pgvector (B22: case retrieval by numeric similarity, decision 6).
# postgis/postgis:17-3.5 is Debian bullseye; the PostgreSQL apt repository moved bullseye to
# apt-archive.postgresql.org, so point the existing (signed) source there. Staying on the same
# base OS keeps glibc collations unchanged for the existing data volume.
FROM postgis/postgis:17-3.5
RUN sed -i 's#http://apt.postgresql.org/pub/repos/apt#http://apt-archive.postgresql.org/pub/repos/apt#' \
      /etc/apt/sources.list.d/pgdg.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends postgresql-17-pgvector \
 && rm -rf /var/lib/apt/lists/*
