"""
Embedding service using HuggingFace transformers + all-MiniLM-L6-v2.
Real semantic search — understands meaning, not just keywords.

Model: all-MiniLM-L6-v2 (22M params, 384-dim, runs fast on CPU)
First run downloads ~80MB from HuggingFace, then cached locally.
"""
import numpy as np
from transformers import AutoTokenizer, AutoModel
import torch
from app.database import store_embedding, get_all_embeddings, get_item, get_db

MODEL_NAME = "sentence-transformers/all-MiniLM-L6-v2"
EMBEDDING_DIM = 384

_tokenizer = None
_model = None


def _load_model():
    """Lazy-load the model on first use."""
    global _tokenizer, _model
    if _tokenizer is None:
        print(f"🧠 Loading embedding model: {MODEL_NAME}...")
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_NAME)
        _model = AutoModel.from_pretrained(MODEL_NAME)
        _model.eval()
        print(f"✅ Model loaded ({EMBEDDING_DIM}-dim embeddings)")
    return _tokenizer, _model


def _mean_pooling(model_output, attention_mask):
    """Mean pooling — take attention mask into account for correct averaging."""
    token_embeddings = model_output.last_hidden_state
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(
        input_mask_expanded.sum(1), min=1e-9
    )


def embed_text(text: str) -> list[float]:
    """Convert text to a 384-dim vector embedding."""
    tokenizer, model = _load_model()

    encoded = tokenizer(
        text,
        padding=True,
        truncation=True,
        max_length=512,
        return_tensors="pt",
    )

    with torch.no_grad():
        output = model(**encoded)

    embedding = _mean_pooling(output, encoded["attention_mask"])

    # L2 normalize
    embedding = torch.nn.functional.normalize(embedding, p=2, dim=1)

    return embedding.squeeze().tolist()


def embed_and_store(item_id: int) -> bool:
    """Embed an item's content and store the vector."""
    item = get_item(item_id)
    if not item:
        return False

    text = f"{item['title']} {item['content']}"
    vector = embed_text(text)
    store_embedding(item_id, vector, MODEL_NAME)
    return True


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors."""
    a_np = np.array(a, dtype=np.float32)
    b_np = np.array(b, dtype=np.float32)

    # Handle dimension mismatch (model upgrade scenario)
    min_len = min(len(a_np), len(b_np))
    a_np = a_np[:min_len]
    b_np = b_np[:min_len]

    norm_a = np.linalg.norm(a_np)
    norm_b = np.linalg.norm(b_np)
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return float(np.dot(a_np, b_np) / (norm_a * norm_b))


def semantic_search(query: str, limit: int = 20) -> list[dict]:
    """Find items similar to the query using vector similarity."""
    all_embeddings = get_all_embeddings()
    if not all_embeddings:
        return []

    query_vector = embed_text(query)

    scored = []
    for emb in all_embeddings:
        score = cosine_similarity(query_vector, emb["vector"])
        item = get_item(emb["item_id"])
        if item:
            scored.append({"item": item, "score": score, "match_type": "semantic"})

    scored.sort(key=lambda x: x["score"], reverse=True)
    return scored[:limit]


def reindex_all() -> int:
    """Re-embed all items in the database."""
    with get_db() as conn:
        rows = conn.execute("SELECT id, title, content FROM items").fetchall()

    count = 0
    for row in rows:
        text = f"{row['title']} {row['content']}"
        vector = embed_text(text)
        store_embedding(row["id"], vector, MODEL_NAME)
        count += 1

    return count
