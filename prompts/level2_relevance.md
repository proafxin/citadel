# Text Relevance

You are given a question and a numbered list of text excerpts drawn from documents already known to bear on it. The excerpts were found by a search over the question's wording — a match in the search does not mean the excerpt actually bears on the question, only that it looked similar.

List only the excerpts the answer cannot do without — one belongs in the list if the answer would be incomplete or wrong without what it holds. An excerpt that merely shares words or subject matter with the question, without actually answering or bearing on it, belongs in neither. It is normal and expected for most or all excerpts in a list to not belong.

## Examples

excerpts:
[0] The lease begins on the first of the month following signing and runs for twelve months.
[1] Either party may end the lease by giving thirty days' written notice.
[2] The tenant is responsible for minor repairs under fifty dollars.

question: what notice does the tenant have to give to end the lease
{"relevant": [1]}

question: what does this document cover
{"relevant": [0, 1, 2]}

question: who pays for a broken window
{"relevant": []}

Respond ONLY with a JSON object: {"relevant": [...]}.
