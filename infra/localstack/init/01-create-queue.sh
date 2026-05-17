#!/bin/sh
set -eu

awslocal sqs create-queue --queue-name dass-tasks >/dev/null 2>&1 || true
awslocal sqs create-queue --queue-name dass-tasks-normal >/dev/null 2>&1 || true
awslocal sqs create-queue --queue-name dass-tasks-retry >/dev/null 2>&1 || true
