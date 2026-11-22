# Same base image the organizers labeled every commit in.
FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90

ENV PYTHONDONTWRITEBYTECODE=1

# selector.py         the program the scorer runs
# blast_map.pkl.gz    per-test coverage, file reads and dialect/expression usage at the pinned commit
# sqlglot_src.tar.gz  the pinned commit's Python sources, which patches are applied to
# requirements.lock   the labeling environment, so cheap selections can be checked by running them
# tpcds.pkl.gz        per-query coverage and pristine outputs of test_execute_tpcds, for replaying it
# operands.pkl.gz     per test, the operands each comparison saw (tools/operand_plugin.py)
# dictreads.pkl.gz    per test, the keys each module/class-level dict literal gave out (same run)
# defaults.pkl.gz     per test, the defaulted parameters some call left at their default
# attr_owners.pkl.gz  per test, the class constants it read, by the class that holds them
COPY requirements.lock selector.py blast_map.pkl.gz sqlglot_src.tar.gz tpcds.pkl.gz globals_refs.pkl.gz genmem.pkl.gz dictkeys.pkl.gz cmp.pkl.gz argreq.pkl.gz import_users.pkl.gz operands.pkl.gz dictreads.pkl.gz defaults.pkl.gz attr_owners.pkl.gz import_lambdas.pkl.gz tpcds_env.pkl.gz /app/

RUN pip install --no-cache-dir --disable-pip-version-check -q -r /app/requirements.lock

# sqlglot's repository at the pinned commit (for its test fixtures), then the
# committed sources on top; both are the same commit. Bytecode is compiled here
# so each runtime check only compiles the files a patch changes.
RUN python - <<'EOF'
import io, tarfile, urllib.request
sha = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
url = f"https://github.com/tobymao/sqlglot/archive/{sha}.tar.gz"
data = urllib.request.urlopen(url, timeout=120).read()
with tarfile.open(fileobj=io.BytesIO(data)) as tf:
    prefix = f"sqlglot-{sha}/"
    members, top = [], []
    for m in tf.getmembers():
        if m.name.startswith(prefix + "tests/"):
            m.name = m.name[len(prefix):]
            members.append(m)
        elif m.name[len(prefix):] in ("pyproject.toml", "setup.cfg", "setup.py"):
            m.name = m.name[len(prefix):]
            top.append(m)
    tf.extractall("/opt/sqlglot", members=members, filter="data")
    # the top-level config files, apart, so nothing run from /opt/sqlglot picks them up
    tf.extractall("/opt/sqlglot_root", members=top, filter="data")
tarfile.open("/app/sqlglot_src.tar.gz").extractall("/opt/sqlglot", filter="data")
EOF
# static_src.pkl: every import statement and test method source of the pinned sources,
# which the import-failure narrowing would otherwise parse again in every run;
# tpcds_ready.pkl / tpcds_rows.pkl: tpcds.pkl.gz as load_tpcds() uses it, the rows apart. selector.py
# is compiled too and imported as a module, so no run compiles it again.
RUN rm /app/sqlglot_src.tar.gz \
 && python -m compileall -q -j 0 /usr/local/lib/python3.13 /opt/sqlglot/sqlglot /opt/sqlglot/tests > /dev/null \
 && python -S /app/selector.py --static /app/static_src.pkl /app/tpcds_ready.pkl /app/tpcds_rows.pkl \
 && python -m compileall -q /app/selector.py \
 && chmod -R a+rX /opt/sqlglot /opt/sqlglot_root /app

CMD ["python", "-S", "-c", "import sys; sys.path.insert(0, '/app'); import selector; selector.cli(sys.argv[1:])"]
