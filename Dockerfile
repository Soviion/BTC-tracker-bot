# Лёгкий образ для запуска бота в контейнере (альтернатива systemd, см. deploy/).
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot/ ./bot/

RUN useradd --system --create-home --shell /usr/sbin/nologin botuser
USER botuser

ENV PYTHONUNBUFFERED=1

CMD ["python", "-m", "bot"]
