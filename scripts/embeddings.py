"""Shared config for the llama-server embedding pipeline (pipeline-owned).

Used by scripts/build_db.py and scripts/embed_only.py so the server URL,
model, batch size, timeout and instruction prefix stay in one place.
"""

# Start llama-server first with:
LLAMA_SERVER = (
    "llama-server -m llm/bge-base-en-v1.5-q8_0.gguf "
    "--embeddings --pooling cls --embd-normalize 2 --gpu-layers all --port 8080"
)

# bge-base-en-v1.5 via llama-server — keep these in sync with any
# llama-server flags you change.
EMBED_URL = "http://localhost:8080/v1/embeddings"
DIM = 768
BATCH = 256
MODEL = "bge-base-en-v1.5"
# Generous timeout — big batches are slow (a 256-text batch through
# llama-server can take ~a minute even on Metal).
TIMEOUT = 600

# BGE instruction prefix — matches the format bge-base-en-v1.5 was trained
# with, so retrieval queries and these stored documents embed consistently.
BGE_PREFIX = "Represent this sentence for searching relevant passages: "
