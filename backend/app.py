"""Owner: backend member. Health works; chat awaits team integration."""
from uuid import uuid4

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.config import FRONTEND_ORIGIN

app = FastAPI(title="GreenPrompt", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN],
    allow_methods=["GET", "POST"],
    allow_headers=["Content-Type"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "GreenPrompt", "stage": "scaffold"}


@app.post("/chat")
async def chat():
    # Implement schemas, orchestration and normalized errors before use.
    return JSONResponse(status_code=501, content={
        "request_id": str(uuid4()),
        "error": {
            "code": "NOT_IMPLEMENTED",
            "message": "Chat integration is not implemented yet.",
            "retryable": False,
        },
    })
