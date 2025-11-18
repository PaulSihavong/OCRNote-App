import os
import uvicorn
import uuid
import io
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware

from google.cloud import vision
from google.cloud import storage
from google.cloud.sql.connector import Connector
import pg8000

from sentence_transformers import SentenceTransformer
from functools import lru_cache

GCS_BUCKET_NAME = os.environ.get("GCS_BUCKET_NAME")
INSTANCE_CONNECTION_NAME = os.environ.get("INSTANCE_CONNECTION_NAME")
DB_NAME = os.environ.get("DB_NAME")
DB_USER = os.environ.get("DB_USER")
DB_PASS = os.environ.get("DB_PASS")

if not all([GCS_BUCKET_NAME, INSTANCE_CONNECTION_NAME, DB_NAME, DB_USER, DB_PASS]):
    raise RuntimeError("Missing one or more required env vars for DB/GCS/DB creds.")

app = FastAPI()

origins = [
	"https://notes-ocr-frontend-1037491170130.us-central1.run.app"
	]

app.add_middleware(
	CORSMiddleware,
	allow_origins=origins,
	allow_credentials=True,
	allow_methods=["*"],
	allow_headers=["*"],
)

vision_client = vision.ImageAnnotatorClient()
storage_client = storage.Client()
bucket = storage_client.bucket(GCS_BUCKET_NAME)

connector = Connector()

def get_conn():
    conn = connector.connect(
        INSTANCE_CONNECTION_NAME,
        "pg8000",
        user=DB_USER,
        password=DB_PASS,
        db=DB_NAME,
    )
    return conn


def insert_document(job_id,
                    user_id,
                    class_name,
                    topic,
                    text_body,
                    embedding):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            INSERT INTO documents (job_id, user_id, class_name, topic, text_body, embedding)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (job_id) DO UPDATE SET
              user_id   = EXCLUDED.user_id,
              class_name= EXCLUDED.class_name,
              topic     = EXCLUDED.topic,
              text_body = EXCLUDED.text_body,
              embedding = EXCLUDED.embedding;
            """,
            (job_id, user_id, class_name, topic, text_body, embedding),
        )
        conn.commit()
    finally:
        conn.close()
        conn.close()


def search_documents(query: str, limit: int = 10):
    pattern = f"%{query}%"
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT job_id, class_name, topic, text_body, created_at
            FROM documents
            WHERE text_body ILIKE %s
               OR topic ILIKE %s
               OR class_name ILIKE %s
            ORDER BY created_at DESC
            LIMIT %s;
            """,
            (pattern, pattern, pattern, limit),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    results = []
    for row in rows:
        job_id, class_name, topic, text_body, created_at = row
        snippet = (text_body[:200] + "...") if len(text_body) > 200 else text_body
        results.append(
            {
                "job_id": job_id,
                "class_name": class_name,
                "topic": topic,
                "snippet": snippet,
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return results


def semantic_search_documents(query_embedding, limit: int = 10):
    conn = get_conn()
    cur = conn.cursor()
    try:
        cur.execute(
            """
            SELECT job_id, class_name, topic, text_body, created_at,
                   embedding <-> %s::vector AS distance
            FROM documents
            WHERE embedding IS NOT NULL
            ORDER BY embedding <-> %s::vector
            LIMIT %s;
            """,
            (query_embedding, query_embedding, limit),
        )
        rows = cur.fetchall()
    finally:
        cur.close()
        conn.close()

    results = []
    for row in rows:
        job_id, class_name, topic, text_body, created_at, distance = row
        snippet = (text_body[:200] + "...") if len(text_body) > 200 else text_body
        results.append(
            {
                "job_id": job_id,
                "class_name": class_name,
                "topic": topic,
                "snippet": snippet,
                "distance": float(distance),
                "created_at": created_at.isoformat() if created_at else None,
            }
        )
    return results

def upload_bytes_to_gcs(blob_name: str, data: bytes, content_type: str) -> str:
    blob = bucket.blob(blob_name)
    blob.upload_from_string(data, content_type=content_type)
    return f"gs://{GCS_BUCKET_NAME}/{blob_name}"


def download_bytes_from_gcs(blob_name: str) -> bytes:
    blob = bucket.blob(blob_name)
    if not blob.exists():
        raise FileNotFoundError(blob_name)
    return blob.download_as_bytes()

def ocr_image_bytes(image_bytes: bytes) -> str:
    image = vision.Image(content=image_bytes)
    response = vision_client.document_text_detection(image=image)

    if response.error.message:
        raise RuntimeError(f"Vision API error: {response.error.message}")

    text = response.full_text_annotation.text or ""
    return text


def basic_clean_text(raw: str) -> str:
    import re

    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip() for line in text.split("\n")]
    text = "\n".join(lines)

    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def guess_class_name(text: str) -> Optional[str]:
    import re

    patterns = [
        r"\b([A-Z]{2,4}\s?\d{3})\b",      # e.g., CS433, CS 433, MATH160
        r"\b([A-Z]{2,4}\s?\d{3}[A-Z]?)\b" # e.g., CS 433A
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1)
    return None


def guess_topic_from_title(text: str) -> Optional[str]:
    import re

    lines = text.splitlines()
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", s):
            continue
        if re.search(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\b", s):
            continue
        if guess_class_name(s):
            continue
        return s[:120]
    return None

@lru_cache(maxsize=1)
def get_embedding_model() -> SentenceTransformer:
    return SentenceTransformer("sentence-transformers/all-MiniLM-L6-v2")

def get_text_embedding(text: str) -> list[float]:
    model = get_embedding_model()
    emb = model.encode([text])[0]
    return emb.tolist()

@app.get("/")
def root():
    return {"status": "ok", "message": "notes-ocr-backend up"}

@app.post("/upload")
async def upload_document(file: UploadFile = File(...)):
    file_bytes = await file.read()

    job_id = str(uuid.uuid4())

    upload_blob_name = f"uploads/{job_id}_{file.filename}"
    upload_bytes_to_gcs(
        upload_blob_name,
        file_bytes,
        content_type=file.content_type or "application/octet-stream",
    )

    try:
        raw_text = ocr_image_bytes(file_bytes)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"OCR failed: {e}")

    if not raw_text.strip():
        raise HTTPException(status_code=400, detail="No text detected in document")

    clean_text = basic_clean_text(raw_text)

    class_name = guess_class_name(clean_text)
    topic = guess_topic_from_title(clean_text)

    try:
        embedding = get_text_embedding(clean_text)
    except Exception as e:
        embedding = None
        print(f"Warning: embedding failed for job_id={job_id}: {e}")

    try:
        insert_document(
            job_id=job_id,
            user_id=None,
            class_name=class_name,
            topic=topic,
            text_body=clean_text,
            embedding=embedding,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"DB insert failed: {e}")

    text_blob_name = f"texts/{job_id}.txt"
    upload_bytes_to_gcs(
        text_blob_name,
        clean_text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )

    return {
        "job_id": job_id,
        "text": clean_text,
        "class_name": class_name,
        "topic": topic,
    }

@app.get("/download/{job_id}")
def download_text(job_id: str):
    blob_name = f"texts/{job_id}.txt"
    try:
        data = download_bytes_from_gcs(blob_name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Text file not found")

    return StreamingResponse(
        io.BytesIO(data),
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{job_id}.txt"'},
    )

@app.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    try:
        results = search_documents(q, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Search error: {e}")

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }

@app.get("/semantic_search")
def semantic_search(q: str = Query(..., min_length=1), limit: int = Query(10, ge=1, le=50)):
    try:
        query_embedding = get_text_embedding(q)
        results = semantic_search_documents(query_embedding, limit=limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Semantic search error: {e}")

    return {
        "query": q,
        "count": len(results),
        "results": results,
    }

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run("main:app", host="0.0.0.0", port=port)

