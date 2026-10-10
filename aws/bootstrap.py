"""Explicitly seed reviewed synthetic catalog; existing work is never replaced."""
import argparse
import os

from aws.repository import DynamoRepository


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--table", default=os.environ.get("ORDERFLOW_TABLE"))
    parser.add_argument("--region", default="ap-southeast-2")
    args = parser.parse_args()
    if not args.table:
        parser.error("Provide --table with the deployed OrderFlow table name.")
    import boto3
    from orderflow.engine import initial_state
    created = DynamoRepository(args.table, boto3.client("dynamodb", region_name=args.region)).initialize(initial_state())
    print("Synthetic catalog initialized." if created else "Existing workspace preserved.")


if __name__ == "__main__":
    main()
