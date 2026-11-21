# Same base image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1

# selector.py         the program the scorer runs
# blast_map.pkl.gz    per-test coverage, file reads and dialect/expression usage at the pinned commit
# sqlglot_src.tar.gz  the pinned commit's Python sources, which patches are applied to
COPY selector.py blast_map.pkl.gz sqlglot_src.tar.gz /app/

RUN python -c "import tarfile; tarfile.open('/app/sqlglot_src.tar.gz').extractall('/opt/sqlglot', filter='data')" \
 && rm /app/sqlglot_src.tar.gz \
 && chmod -R a+rX /opt/sqlglot /app

CMD ["python", "-S", "/app/selector.py"]
