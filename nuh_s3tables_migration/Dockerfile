# One-time NUH Parquet -> S3 Tables migration runner.
FROM public.ecr.aws/lambda/python:3.12

COPY nuh_s3tables_migration/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt

COPY nuh_s3tables_migration/handler.py ${LAMBDA_TASK_ROOT}/handler.py

CMD ["handler.lambda_handler"]
