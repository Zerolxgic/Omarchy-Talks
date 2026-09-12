"""Deterministic, dependency-free semantic segmentation for speech."""
import re

MAX_CHUNK_CHARS = 100
MIN_TRAILING_CHUNK_CHARS = 40

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_BOUNDARY = re.compile(r"[,;:\u2014\u2013-](?=\s)")


def normalize_text(text: str) -> str:
    """Strip edges and collapse every whitespace run to one ASCII space."""
    return " ".join(text.split())


def _split_long(unit: str, limit: int) -> list[str]:
    chunks = []
    remaining = unit
    while len(remaining) > limit:
        window = remaining[:limit]
        clause_ends = [match.end() for match in _CLAUSE_BOUNDARY.finditer(window)]
        if clause_ends:
            cut = clause_ends[-1]
        else:
            whitespace = window.rfind(" ")
            cut = whitespace if whitespace > 0 else limit

        chunks.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        chunks.append(remaining)
    return chunks


def segment(text: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """Split normalized text, preferring sentences, clauses, then whitespace.

    Sentence punctuation is retained on the preceding chunk. Long sentences use
    the best clause boundary available inside ``limit``, then whitespace, and
    finally a hard cut for an oversized unbroken token.
    """
    if limit <= 0:
        raise ValueError("chunk limit must be positive")
    normalized = normalize_text(text)
    if not normalized:
        return []
    sentence_chunks = [_split_long(sentence, limit)
                       for sentence in _SENTENCE_BOUNDARY.split(normalized)]
    # Only a fragment introduced by splitting an oversized sentence is
    # eligible. Ordinary short sentences remain independent semantic units.
    for index, chunks in enumerate(sentence_chunks[:-1]):
        fragment = chunks[-1]
        following = sentence_chunks[index + 1][0]
        if (len(chunks) > 1 and len(fragment) < MIN_TRAILING_CHUNK_CHARS and
                len(fragment) + 1 + len(following) <= limit):
            chunks.pop()
            sentence_chunks[index + 1][0] = f"{fragment} {following}"
    return [chunk for chunks in sentence_chunks for chunk in chunks]
