FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .
COPY knowledge ./knowledge
CMD ["sh", "-c", "gunicorn -b 0.0.0.0:${PORT:-8000} app:app --workers 2 --timeout 60"]
