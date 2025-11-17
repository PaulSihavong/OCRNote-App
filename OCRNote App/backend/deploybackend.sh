#!/usr/bin/env bash

echo "Deploy Backend"

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project)}"

REGION="us-central1"

SA_NAME="${SA_NAME:-ocr-backend-sa}"
INSTANCE_NAME="${INSTANCE_NAME:-notes-ocr-sql}"
BUCKET_NAME="${BUCKET_NAME:-notes-ocr-bucket}"
DB_NAME="${DB_NAME:-notesdb}"
DB_USER="${DB_USER:-user}"
DB_PASS="${DB_PASS:-password}"

INSTANCE_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${INSTANCE_NAME}"

echo "Using project:         ${PROJECT_ID}"
echo "Using region:          ${REGION}"
echo "Using service account: ${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
echo "Using bucket:          ${BUCKET_NAME}"
echo "Using SQL instance:    ${INSTANCE_CONNECTION_NAME}"

ls -l

gcloud run deploy notes-ocr-backend \
  --source . \
  --region "${REGION}" \
  --platform managed \
  --service-account "${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "GCS_BUCKET_NAME=${BUCKET_NAME}" \
  --set-env-vars "INSTANCE_CONNECTION_NAME=${INSTANCE_CONNECTION_NAME}" \
  --set-env-vars "DB_NAME=${DB_NAME},DB_USER=${DB_USER},DB_PASS=${DB_PASS}" \
  --add-cloudsql-instances "${INSTANCE_CONNECTION_NAME}" \
  --allow-unauthenticated
