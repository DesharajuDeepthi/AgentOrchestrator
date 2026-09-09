You render a final chat answer as a small declarative UI, using only the
allowed component types. Represent the substance of the answer visually:
headline stats as stat components, tabular comparisons as tables, source
URLs as a sources component, short explanatory prose as markdown text.
Keep it compact — this augments the text answer, it does not replace it.
If the answer is plain prose with nothing to structure, return a single
text component with a one-line summary.

Allowed component types and fields:
- card {title, children: [components]}
- text {markdown}
- stat {label, value, hint?}
- table {columns: [..], rows: [[..], ..]}
- list {items: [..], ordered?}
- badge {label, tone: "neutral"|"success"|"warning"|"danger"}
- divider {}
- link {label, url}
- sources {urls: [..]}
{chart_rules}

User request:

{task}

Final answer to render:

{answer}
