FROM python:3.12-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY orderflow ./orderflow
COPY frontend ./frontend
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 appuser && mkdir -p /app/data /app/build && chown appuser /app/data /app/build
USER appuser
CMD ["python", "-m", "orderflow", "drill", "--database", "/app/data/orderflow.sqlite3"]
