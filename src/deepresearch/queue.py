"""SQS intake queue wrapper (the only entry point; no public endpoint is exposed)."""
from __future__ import annotations

import json

from .aws_clients import client
from .config import Settings


class IntakeQueue:
    def __init__(self, settings: Settings, queue_url: str | None = None):
        self.s = settings
        self.sqs = client("sqs", settings.queue_region)
        self.url = queue_url or settings.state("queue_url")
        if not self.url:
            self.url = self.sqs.get_queue_url(QueueName=settings["queue"]["name"])["QueueUrl"]

    def send(self, task: dict) -> str:
        body = json.dumps({"task_id": task["task_id"], "query": task["query"], "depth": task.get("depth", "standard"),
                           "actor_id": task.get("actor_id", "anonymous")})
        return self.sqs.send_message(QueueUrl=self.url, MessageBody=body,
                                     MessageAttributes={"depth": {"DataType": "String", "StringValue": task.get("depth", "standard")}})["MessageId"]

    def receive(self, max_messages: int = 1, wait: int = 5) -> list[dict]:
        r = self.sqs.receive_message(QueueUrl=self.url, MaxNumberOfMessages=max_messages, WaitTimeSeconds=wait,
                                     AttributeNames=["ApproximateReceiveCount"])
        out = []
        for m in r.get("Messages", []):
            body = json.loads(m["Body"])
            body["_receipt"] = m["ReceiptHandle"]
            body["_receive_count"] = int(m.get("Attributes", {}).get("ApproximateReceiveCount", 1))
            out.append(body)
        return out

    def delete(self, receipt: str) -> None:
        self.sqs.delete_message(QueueUrl=self.url, ReceiptHandle=receipt)

    def extend(self, receipt: str, seconds: int) -> None:
        self.sqs.change_message_visibility(QueueUrl=self.url, ReceiptHandle=receipt, VisibilityTimeout=seconds)

    def release(self, receipt: str, delay: int = 0) -> None:
        """Make the message visible again soon (used when admission is denied)."""
        self.sqs.change_message_visibility(QueueUrl=self.url, ReceiptHandle=receipt, VisibilityTimeout=delay)

    def depth(self) -> dict[str, int]:
        a = self.sqs.get_queue_attributes(QueueUrl=self.url, AttributeNames=[
            "ApproximateNumberOfMessages", "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return {"visible": int(a["ApproximateNumberOfMessages"]), "inflight": int(a["ApproximateNumberOfMessagesNotVisible"])}
