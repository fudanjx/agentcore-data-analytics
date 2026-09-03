import boto3
from botocore.exceptions import ClientError


class S3TableManager:
    def __init__(self, region_name=None):
        self.client = boto3.client("s3tables", region_name=region_name)

    def delete_table(self, bucket_arn, namespace, table_name):
        print(f"Deleting table: {namespace}.{table_name}")

        self.client.delete_table(
            tableBucketARN=bucket_arn,
            namespace=namespace,
            name=table_name,
        )

    def delete_all_tables(self, bucket_arn):
        paginator = self.client.get_paginator("list_tables")

        count = 0

        for page in paginator.paginate(tableBucketARN=bucket_arn):
            for table in page.get("tables", []):
                namespace = table["namespace"][0]
                name = table["name"]

                self.delete_table(bucket_arn, namespace, name)
                count += 1

        print(f"Deleted {count} tables")

    def delete_namespace(self, bucket_arn, namespace):
        print(f"Deleting namespace: {namespace}")

        self.client.delete_namespace(
            tableBucketARN=bucket_arn,
            namespace=namespace,
        )

    def delete_all_namespaces(self, bucket_arn):
        paginator = self.client.get_paginator("list_namespaces")

        count = 0

        for page in paginator.paginate(tableBucketARN=bucket_arn):
            for ns in page.get("namespaces", []):
                namespace = ns["namespace"][0]

                self.delete_namespace(bucket_arn, namespace)
                count += 1

        print(f"Deleted {count} namespaces")

    def delete_bucket(self, bucket_arn, force=False):
        """
        force=False:
            Delete bucket only.

        force=True:
            Delete all tables, namespaces, then bucket.
        """

        if force:
            print("Force cleanup enabled")

            self.delete_all_tables(bucket_arn)
            self.delete_all_namespaces(bucket_arn)

        print(f"Deleting table bucket: {bucket_arn}")

        self.client.delete_table_bucket(
            tableBucketARN=bucket_arn
        )

        print("Completed")


if __name__ == "__main__":

    manager = S3TableManager(region_name="ap-southeast-1")

    bucket_arn = (
        "arn:aws:s3tables:ap-southeast-1:964340114883:bucket/jg-testing2"
    )

    # Example 1
    # Delete a single table
    # manager.delete_table(
    #     bucket_arn=bucket_arn,
    #     namespace="nuh",
    #     table_name="soc",
    # )

    # Example 2
    # Delete all tables only
    # manager.delete_all_tables(bucket_arn)

    # Example 3
    # Delete bucket directly (must already be empty)
    # manager.delete_bucket(bucket_arn)

    # Example 4
    # Delete everything in bucket then delete bucket
    manager.delete_bucket(bucket_arn, force=True)