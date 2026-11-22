# Blast Radius selector for sqlglot @ eb11aec4be8cb2122afe713a4816662c3f0fb2fa.
# Same base image (by digest) the commits were labeled in, so decision-time
# test runs behave like the labeler's.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SKIP_INTEGRATION=0 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    BR_ROOT=/opt/sqlglot

# The labeler's exact package set (from the starter kit), from PyPI.
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock

# sqlglot's own repository at the pinned commit (GitHub archive of that commit).
RUN python - <<'EOF'
import io, tarfile, urllib.request
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/opt", filter="data")
import os
os.rename(f"/opt/sqlglot-{SHA}", "/opt/sqlglot")
EOF

# Precompile so decision-time imports of unchanged modules read bytecode, and
# make everything readable by the unprivileged runtime user.
RUN python -m compileall -q -j 0 /opt/sqlglot/sqlglot /opt/sqlglot/tests /opt/sqlglot/sqlglotc \
    && chmod -R a+rX /opt/sqlglot

COPY selector /app/selector
COPY maps /app/maps
# State fingerprint of the pinned tree under two hash seeds, used to see which dict
# entries a patch changes at import time (selector/keyrefine.py).
RUN python -c "import sys; sys.path.insert(0, '/app'); from selector import keyrefine; keyrefine.build_pinned('/opt/sqlglot', '/app/maps/pinned_state.json.gz')" \
    && python -m compileall -q /app/selector && chmod -R a+rX /app

CMD ["python", "/app/selector/main.py"]
