FROM python:3.13.18-slim-bookworm@sha256:a5a7017efc5969e00ae2b91c8eb0631049a569652aa4ffc4501769049ae45c72 AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements.txt \
    && groupadd --system aimvoto \
    && useradd --system --gid aimvoto --no-create-home aimvoto \
    && install -d -o aimvoto -g aimvoto /app/staticfiles

FROM base AS test
COPY requirements-dev.txt .
RUN python -m pip install --no-cache-dir --require-hashes -r requirements-dev.txt
COPY --chown=aimvoto:aimvoto . .
USER aimvoto

FROM base AS runtime
COPY --chown=aimvoto:aimvoto . .
USER aimvoto
EXPOSE 8000
CMD ["gunicorn", "--bind=0.0.0.0:8000", "--workers=2", "--worker-class=gthread", "--threads=4", "--no-control-socket", "config.wsgi:application"]
