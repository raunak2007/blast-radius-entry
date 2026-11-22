# Environment for regenerating maps/ (coverage and class facts) on Linux with the
# labeler's exact Python and packages. Build from the repository root:
#   docker build --platform linux/arm64 -f tools/trace_image/Dockerfile -t br-trace .
# (arm64 runs natively on Apple Silicon; the maps are platform independent.)
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
ENV PYTHONDONTWRITEBYTECODE=1 SKIP_INTEGRATION=0 PIP_DISABLE_PIP_VERSION_CHECK=1
COPY requirements.lock /app/requirements.lock
RUN pip install --no-cache-dir -r /app/requirements.lock
RUN python - <<'PY'
import io, tarfile, urllib.request, os
SHA = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
data = urllib.request.urlopen(f"https://github.com/tobymao/sqlglot/archive/{SHA}.tar.gz", timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tar:
    tar.extractall("/opt", filter="data")
os.rename(f"/opt/sqlglot-{SHA}", "/opt/sqlglot")
PY
RUN python -m compileall -q -j 0 /opt/sqlglot/sqlglot /opt/sqlglot/tests /opt/sqlglot/sqlglotc
COPY selector /app/selector
COPY tools /app/tools
WORKDIR /app
