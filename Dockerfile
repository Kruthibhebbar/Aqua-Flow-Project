FROM python:3.12-slim

WORKDIR /app

# System deps (bcrypt/pymongo build cleanly without extras, but keep this
# in case you add packages later that need compiling)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Install Python deps first for better layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the whole project (Backend/, templates/, static/, etc.)
COPY . .

# Fly sets PORT itself; 8080 is just the default fallback used in fly.toml
ENV PORT=8080
EXPOSE 8080

WORKDIR /app/Backend

# gunicorn is already in requirements.txt — use it instead of app.run()
# app.py defines "app = Flask(...)" so the module:variable is app:app
CMD ["sh", "-c", "gunicorn --bind 0.0.0.0:$PORT --workers 2 --threads 4 --timeout 60 app:app"]
