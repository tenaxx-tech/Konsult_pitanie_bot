FROM python:3.12-slim
WORKDIR /app
COPY nutrition /app/nutrition
COPY main.py /app/main.py
RUN mkdir -p /app/data
ENV API_HOST=0.0.0.0 PORT=8081 CHANNEL_DB=/app/data/channels.sqlite3 NUTRITION_TIMEZONE=Asia/Yekaterinburg PYTHONUNBUFFERED=1
EXPOSE 8081
CMD ["python", "/app/main.py"]
