FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# Keep the source in its package directory because it uses relative imports.
COPY . ./s3tables_delta_pilot

RUN useradd --create-home --uid 1000 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 8090

CMD ["uvicorn", "s3tables_delta_pilot.webapp:app", "--host", "0.0.0.0", "--port", "8090"]
