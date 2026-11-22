-- Runs once, on first start of an empty Postgres volume.
-- Langfuse (observability profile) keeps its data in its own database on the same server.
CREATE DATABASE langfuse;
