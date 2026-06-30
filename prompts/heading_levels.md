You are given the headings of ONE document, in reading order, so you can assign each a hierarchy level.

Each heading is a numbered line:
index | size=<font size, or ?> | p<page> | heading text
followed by an "intro:" line showing the start of the text directly under that heading, or "(no text directly under it)" when a sub-heading or nothing follows immediately.

Assign each heading a level: 1 = top-level section, 2 = subsection under a level-1, 3 = deeper, and so on.

How to decide:
- Font size is RELATIVE to this document — there is no fixed convention. Look at the sizes that actually appear and rank them into tiers: the largest tier is level 1, the next tier down is level 2, and so on. Headings sharing a tier are usually the same level.
- When the sizes do not separate the headings (they are uniform or inconsistent), use meaning and structure instead: a heading whose topic is narrower than, and belongs under, a preceding heading is one level deeper.
- Parent-child is NOT decided by adjacency. A section can have its own introductory text, or several subsections, before its first child heading, and a parent and child need not be next to each other. Infer nesting from scope, numbering, and font tiers across the whole list — a heading's children are the later headings that are narrower and belong under it, up to the next heading of the same or higher level.
- Treat any numbering or lettering scheme (1 / 1.1, or I / A / i) as an extra hint only; never assume one exists.
- Hard rule: a heading can never be a shallower level than the heading it sits under.

Respond ONLY with a JSON object mapping each heading's index (as a string key) to its level, e.g. {"0": 1, "1": 2}. Include an entry for every index.

Example input:
0 | size=16 | p1 | Introduction
    intro: This report examines the effects of...
1 | size=16 | p2 | Methods
    intro: (no text directly under it)
2 | size=13 | p2 | Data Collection
    intro: We gathered samples from...
3 | size=13 | p3 | Analysis
    intro: Each sample was scored by...
4 | size=16 | p4 | Results
    intro: The model achieved...

Example output:
{"0": 1, "1": 1, "2": 2, "3": 2, "4": 1}

Now assign levels to these headings:
