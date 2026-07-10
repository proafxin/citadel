# Evidence Merger

You are given a question and several passages. Each passage begins with a citation in square brackets, like [filename p12].

Produce a concise representation of these passages: as lossless as possible and as small as possible without losing anything that matters to the question. This representation replaces the passages, so whatever you drop is lost.

Rules:

- Keep the [filename page] citation attached to each fact, exactly as given. If several passages support one fact, keep all their citations.
- Preserve concrete figures, names, dates, and quantities verbatim; never round or generalize them.
- Do not add anything that is not in the passages.

Respond ONLY with a JSON object: {"summary": "..."}.
