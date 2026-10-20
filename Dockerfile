FROM python:3.12-slim
WORKDIR /app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && curl --fail --show-error --location --proto '=https' --tlsv1.2 \
       https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
       --output /usr/local/share/ca-certificates/russian_trusted_root_ca.crt \
    && update-ca-certificates \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "google-auth[requests]>=2.38,<3" tzdata
COPY nutrition /app/nutrition
COPY main.py /app/main.py
RUN mkdir -p /app/data
ENV API_HOST=0.0.0.0 PORT=8081 CHANNEL_DB=/app/data/channels.sqlite3 NUTRITION_TIMEZONE=Asia/Yekaterinburg PYTHONUNBUFFERED=1
EXPOSE 8081
CMD ["python", "/app/main.py"]
