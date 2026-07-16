# AWS Lambda container image — Python 3.12, x86_64
# S3-event-triggered loader for AH parquet → S3 Tables (Iceberg).
FROM public.ecr.aws/lambda/python:3.12

COPY lambda_s3tables_loader/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Shared transforms module
COPY infra/ah_transforms.py ${LAMBDA_TASK_ROOT}/ah_transforms.py

# Handler
COPY lambda_s3tables_loader/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]
