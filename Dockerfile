FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 archiver \
    && useradd --uid 1001 --gid archiver --create-home archiver

COPY requirements.txt pyproject.toml README.md ./
RUN python -m pip install --upgrade pip \
    && python -m pip install -r requirements.txt

COPY app ./app
RUN python -m pip install --no-deps .

RUN mkdir -p /app/data /app/downloads \
    && chown -R archiver:archiver /app

USER archiver

VOLUME ["/app/data", "/app/downloads"]
ENTRYPOINT ["python", "-m", "app"]
CMD ["listen"]
