FROM denoland/deno:bin-2 AS deno
FROM python:3.12-slim
COPY --from=deno /deno /usr/local/bin/deno
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN useradd --create-home bot && chown bot:bot /app
COPY --chown=bot:bot . .
USER bot
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
