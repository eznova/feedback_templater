FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ARG APP_COMMIT=unknown
ENV APP_COMMIT=${APP_COMMIT}

EXPOSE 18500

CMD ["python", "-m", "src.app"]
