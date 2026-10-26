FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY pricing.json .
COPY app/ ./app/
COPY static/ ./static/
COPY fixtures/ ./fixtures/
COPY scripts/ ./scripts/
COPY docs/ ./docs/
COPY VERSION .

ENV DATA_DIR=/data
ENV PRICING_FILE=/app/pricing.json
ENV PYTHONUNBUFFERED=1

RUN mkdir -p /data

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
