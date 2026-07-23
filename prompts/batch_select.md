# Section Selector

You are given a question and a numbered list of document sections. Each section is shown as its document's filename followed by a short summary of what that section contains.

Select every section whose content is needed to answer the question, and only those. A section is needed if a fact, figure, name, or statement it contains would go into the answer. Do not select a section that merely shares words with the question but would not contribute to the answer.

Match the breadth of your selection to the breadth of the question. Some questions are answered by one fact from one section; others are answered only by drawing on every document at once. Judge how much of the collection the answer must account for, and select exactly that much — a section from every document when the answer spans the whole collection, only the few that hold the answer when it is specific. Under-selecting a broad question leaves the answer incomplete; over-selecting a specific one buries it.

List the selected sections most relevant first. If none are needed, return an empty list.

Respond ONLY with a JSON object: {"sections": [<number>, ...]}.
