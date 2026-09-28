"""S3 bucket (skills + reports), SQS intake queue + DLQ, and a check that the reused DynamoDB table exists."""
from __future__ import annotations

import json

from botocore.exceptions import ClientError

from ..aws_clients import client
from ..config import Settings


def ensure_bucket(s: Settings) -> str:
    s3 = client("s3", s.harness_region)
    name = s.bucket
    try:
        s3.head_bucket(Bucket=name)
    except ClientError:
        kwargs = {"Bucket": name}
        if s.harness_region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": s.harness_region}
        s3.create_bucket(**kwargs)
        s3.put_public_access_block(Bucket=name, PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True, "BlockPublicPolicy": True, "RestrictPublicBuckets": True})
        s3.put_bucket_encryption(Bucket=name, ServerSideEncryptionConfiguration={
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]})
        s3.put_bucket_lifecycle_configuration(Bucket=name, LifecycleConfiguration={"Rules": [
            {"ID": "expire-reports", "Status": "Enabled", "Filter": {"Prefix": s["storage"]["reports_prefix"]},
             "Expiration": {"Days": 180}}]})
    return name


def ensure_queues(s: Settings) -> dict[str, str]:
    sqs = client("sqs", s.queue_region)
    q = s["queue"]
    dlq_url = sqs.create_queue(QueueName=q["dlq_name"], Attributes={"MessageRetentionPeriod": "1209600"})["QueueUrl"]
    dlq_arn = sqs.get_queue_attributes(QueueUrl=dlq_url, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
    attrs = {
        "VisibilityTimeout": str(q["visibility_timeout_seconds"]),
        "MessageRetentionPeriod": "1209600",
        "ReceiveMessageWaitTimeSeconds": "10",
        "RedrivePolicy": json.dumps({"deadLetterTargetArn": dlq_arn, "maxReceiveCount": q["max_receive_count"]}),
    }
    try:  # existing queue: CreateQueue rejects changed attributes, so update them in place
        url = sqs.get_queue_url(QueueName=q["name"])["QueueUrl"]
    except sqs.exceptions.QueueDoesNotExist:
        url = sqs.create_queue(QueueName=q["name"], Attributes=attrs)["QueueUrl"]
    sqs.set_queue_attributes(QueueUrl=url, Attributes=attrs)
    return {"queue_url": url, "dlq_url": dlq_url}


def verify_table(s: Settings) -> dict:
    ddb = client("dynamodb", s.table_region)
    t = ddb.describe_table(TableName=s.table_name)["Table"]
    keys = {k["AttributeName"]: k["KeyType"] for k in t["KeySchema"]}
    assert keys == {"pk": "HASH", "sk": "RANGE"}, f"unexpected key schema on {s.table_name}: {keys}"
    ttl = ddb.describe_time_to_live(TableName=s.table_name)["TimeToLiveDescription"]
    return {"table": s.table_name, "ttl": ttl.get("TimeToLiveStatus"), "ttl_attr": ttl.get("AttributeName")}


def ensure_notifications(s: Settings) -> dict[str, str]:
    """SNS topic for task completion events + HMAC secret for webhooks (stored in Secrets Manager)."""
    import secrets as _secrets
    sns = client("sns", s.queue_region)
    topic = sns.create_topic(Name=s["notifications"]["sns_topic_name"], Tags=[{"Key": "Project", "Value": s.project}])["TopicArn"]
    sm = client("secretsmanager", s.region)
    sid = s["notifications"]["webhook_secret_id"]
    try:
        secret = json.loads(sm.get_secret_value(SecretId=sid)["SecretString"])["secret"]
    except ClientError:
        secret = _secrets.token_hex(32)
        sm.create_secret(Name=sid, Description="HMAC-SHA256 key for deep-research completion webhooks",
                         SecretString=json.dumps({"secret": secret}))
    return {"sns_topic_arn": topic, "webhook_secret": secret, "webhook_secret_id": sid}
