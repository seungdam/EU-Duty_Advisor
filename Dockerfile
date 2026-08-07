  # syntax=docker/dockerfile:1

  FROM node:22.23.1-bookworm-slim AS web-build
  WORKDIR /build/webapp

  COPY webapp/package.json webapp/package-lock.json ./
  RUN npm ci

  COPY webapp/ ./
  RUN npm run build

  FROM continuumio/miniconda3:26.1.1-1 AS runtime
  WORKDIR /app
  ENV PIP_NO_CACHE_DIR=1

  COPY environments.yml /tmp/environments.yml
  RUN sed -i 's/^[[:space:]]*- defaults[[:space:]]*$/  - nodefaults/' /tmp/environments.yml \
      && grep -Eq '^[[:space:]]*- nodefaults[[:space:]]*$' /tmp/environments.yml \
      && conda env create --yes -f /tmp/environments.yml \
      && conda clean --all --yes

  RUN conda run -n asap_pw python -m playwright install --with-deps chromium

  COPY . /app
  COPY --from=web-build /build/webapp/dist /app/webapp/dist

  RUN timeout 60s conda run -n asap_pw python -c "import importlib.metadata as m, asap_app, paddle, torch; cuda = sorted(d.metadata['Name'] for d in m.distributions() if (d.metadata['Name'] or '').lower().startswith('nvidia-')); assert torch.version.cuda is None and not paddle.device.is_compiled_with_cuda() and not cuda, (torch.version.cuda, cuda)"

  ENV PYTHONUNBUFFERED=1
  EXPOSE 8060

  CMD ["/opt/conda/envs/asap_pw/bin/gunicorn", \
      "--bind=0.0.0.0:8060", \
      "--workers=1", \
      "--worker-class=gthread", \
      "--threads=4", \
      "--timeout=120", \
      "--access-logfile=-", \
      "asap_app:app"]
