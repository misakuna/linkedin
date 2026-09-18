# Deploy target: Railway or Render (persistent Python service, NOT serverless).
# The scheduler runs for the life of the container and SQLite lives on a
# mounted volume — see the Deploying section of README.md.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# SQLite lives here. Mount a persistent volume at /data on Railway/Render and
# set DATABASE_PATH=/data/david.db, or state is lost on every deploy.
#
# Deliberately no VOLUME instruction: Railway rejects it and fails the build
# ("docker VOLUME at Line N is not supported, use Railway Volumes"). The mount
# is declared in the service's Volumes settings instead. Nothing is lost — the
# directive only ever created an anonymous volume for bare `docker run`.
ENV DATABASE_PATH=/data/david.db

EXPOSE 8000

# Railway/Render inject $PORT; 8000 is the local fallback.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
