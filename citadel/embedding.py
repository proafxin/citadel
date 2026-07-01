import functools

from sentence_transformers import SentenceTransformer

from config import get_settings

EMBED_BATCH = 32


@functools.lru_cache
def get_embedder() -> SentenceTransformer:
    settings = get_settings()
    return SentenceTransformer(settings.embed_model, device=settings.embed_device)


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = get_embedder().encode(texts, batch_size=EMBED_BATCH, normalize_embeddings=True)
    return [vector.tolist() for vector in vectors]
