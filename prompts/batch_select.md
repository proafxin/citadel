# Section Selector

You are given a question and a numbered list of document sections. Each section is shown as its document's filename followed by a short summary of what that section contains.

Select every section whose content is needed to answer the question, and only those. A section is needed if a fact, figure, name, or statement it contains would go into the answer. Do not select a section that merely shares words with the question but would not contribute to the answer.

For a broad question about the documents themselves, the sections that describe those documents are the ones needed. For a specific question, only the few sections that hold the answer are needed.

List the selected sections most relevant first. If none are needed, return an empty list.

Respond ONLY with a JSON object: {"sections": [<number>, ...]}.
