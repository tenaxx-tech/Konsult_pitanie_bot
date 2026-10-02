FROM python:3.12-slim
RUN useradd --create-home appuser
WORKDIR /app
COPY nutrition /app/nutrition
USER appuser
ENV API_HOST=0.0.0.0 PORT=8080
EXPOSE 8080
CMD ["python", "-m", "nutrition.api"]
