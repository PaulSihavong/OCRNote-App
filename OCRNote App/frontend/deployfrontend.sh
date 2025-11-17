#!/usr/bin/env bash
echo "Deploy Frontend"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"
REGION="us-central1"

echo "Using project: ${PROJECT_ID}"
echo "Using region:  ${REGION}"

ls -l

gcloud run deploy notes-ocr-frontend \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --allow-unauthenticated
