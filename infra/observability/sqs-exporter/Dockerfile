FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir \
    boto3==1.34.131 \
    botocore==1.34.131 \
    prometheus-client==0.20.0 \
    docker==7.1.0
COPY app.py .
EXPOSE 9100
CMD ["python", "-u", "app.py"]
