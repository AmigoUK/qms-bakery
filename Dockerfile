FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Runtime dependencies only:
#   - libpango / libharfbuzz / fontconfig / shared-mime-info: WeasyPrint
#     PDF rendering (HACCP monthly, FSA traceability, training certs)
#   - curl: kept for healthcheck-style ad-hoc probes; no compilation toolchain
#
# The project relies on binary wheels (psycopg[binary], bcrypt manylinux,
# weasyprint cffi bundles), so build-essential and libpq-dev are not
# needed -- a meaningful attack-surface reduction vs. the previous image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    libpango-1.0-0 \
    libpangoft2-1.0-0 \
    libharfbuzz0b \
    fontconfig \
    shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

# Create the unprivileged runtime user up-front. UID/GID are overridable
# at build time so a host-bind-mount deployment can match the host owner
# without rebuilding.
ARG QMS_UID=1000
ARG QMS_GID=1000
RUN groupadd --system --gid ${QMS_GID} qms \
    && useradd --system --uid ${QMS_UID} --gid ${QMS_GID} \
        --create-home --home-dir /home/qms --shell /usr/sbin/nologin qms

WORKDIR /app
RUN chown ${QMS_UID}:${QMS_GID} /app

COPY --chown=${QMS_UID}:${QMS_GID} pyproject.toml ./
# `tool.uv.package = false` makes this an effective deps-only install
# against the system site-packages -- the editable-install marker just
# tells uv to read pyproject as a manifest. Site-packages are root-owned
# but world-readable; the qms user reads them fine at runtime.
RUN uv pip install --system -e .

COPY --chown=${QMS_UID}:${QMS_GID} . .

ENV GUNICORN_WORKERS=4 \
    GUNICORN_BIND=0.0.0.0:8000

USER qms

EXPOSE 8000

# Self-check on `docker run` (compose has its own healthcheck that
# overrides this one in compose deployments). Mirrors the compose probe
# so both surfaces stay in sync.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz', timeout=2).status == 200 else 1)"

# Shell form so $GUNICORN_WORKERS / $GUNICORN_BIND substitute. Override
# either at run-time (e.g. for unix-socket binding or worker tuning).
CMD ["sh", "-c", "exec gunicorn -w ${GUNICORN_WORKERS} -b ${GUNICORN_BIND} 'app:create_app()'"]
