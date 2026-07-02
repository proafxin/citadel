import functools
import logging

from sentence_transformers import SentenceTransformer

from config import get_settings

logger = logging.getLogger(__name__)

EMBED_BATCH = 32


@functools.lru_cache
def get_embedder() -> SentenceTransformer:
    settings = get_settings()
    logger.info("loading embedder model=%s device=%s", settings.embed_model, settings.embed_device)
    model = SentenceTransformer(settings.embed_model, device=settings.embed_device)
    logger.info("embedder loaded model=%s", settings.embed_model)
    return model


def embed_texts(texts: list[str]) -> list[list[float]]:
    if not texts:
        return []
    vectors = get_embedder().encode(
        texts, batch_size=EMBED_BATCH, normalize_embeddings=True, show_progress_bar=False
    )
    return [vector.tolist() for vector in vectors]
