# ============================================================
# Dockerfile for AquaFlow — built for Google Cloud Run
# ============================================================
FROM python:3.12-slim
 
# Prevent Python from writing .pyc files and buffering stdout/stderr
# (buffered logs would be invisible in Cloud Run's log viewer until
# the process exits, which makes debugging painful)
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
 
WORKDIR /app
 
# Install dependencies first (separate layer so Docker can cache this
# step and skip reinstalling everything just because you changed an
# HTML file later)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
 
# Now copy the rest of the project
COPY . .
 
# Cloud Run injects the PORT environment variable at runtime (usually
# 8080) — app.py already reads os.environ.get('PORT', 5000), so no
# code change is needed, just make sure gunicorn also binds to it.
ENV PORT=8080
EXPOSE 8080
 
# WORKDIR is /app, but app.py lives in /app/Backend and references
# templates as '../templates' (i.e. /app/templates) — so gunicorn
# must run from inside Backend/ for that relative path to resolve.
WORKDIR /app/Backend
 
CMD exec gunicorn --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 60 app:app
 





