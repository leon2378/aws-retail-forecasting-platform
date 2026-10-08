FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS=1
WORKDIR /app
RUN apt-get update && apt-get install --no-install-recommends -y libgomp1 && rm -rf /var/lib/apt/lists/*
COPY aws/requirements-image.txt /app/requirements-image.txt
RUN pip install --no-cache-dir -r requirements-image.txt
COPY retail_forecast /app/retail_forecast
COPY aws /app/aws
EXPOSE 8080
ENTRYPOINT ["python", "-m", "aws.jobs"]
CMD ["train"]
