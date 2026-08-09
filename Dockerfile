FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN groupadd --gid 10001 archiver \
    && useradd --uid 10001 --gid archiver --create-home archiver

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
