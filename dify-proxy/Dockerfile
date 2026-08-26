FROM python:3.12-slim-bookworm
# Install only the necessary security update
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    && echo "deb http://deb.debian.org/debian testing main" > /etc/apt/sources.list \
    && apt-get update \
    # For Security
    && apt-get install -y --no-install-recommends expat\
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY dify-server.py .
COPY model_usage.py .

RUN adduser --disabled-password --gecos "" appuser
USER appuser

EXPOSE 8080

CMD ["uvicorn", "dify-server:app", "--host", "0.0.0.0", "--port", "8080"]
