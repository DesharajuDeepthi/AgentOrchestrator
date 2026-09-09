You convert raw text into a structured JSON summary.

Read the text below and produce a JSON object with exactly these fields:
- "title": a short descriptive title for the content
- "summary": a faithful 2-4 sentence summary
- "key_points": a list of the 3-7 most important points, each one sentence
- "entities": a list of the named people, organizations, and products mentioned

Respond with the JSON object only.

Text:

{text}

Strict format requirements: "key_points" and "entities" MUST be JSON arrays
of strings (never a single string, never null — use [] when nothing applies).
