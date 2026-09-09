# Anticipation briefing

The platform is idle. Based on the user's recent activity and standing watches, predict what they are most likely to ask for next, and pre-compute a short briefing item for each prediction.

## Recent activity (newest first)

{recent_activity}

## Active standing watches

{watches}

## Output

Return up to 3 items. Each item is one likely next ask: `title` names it in a few words; `note` is one or two sentences of pre-computed substance — the key fact, the obvious next step, or the thing worth knowing before they ask. Only include items with a genuine basis in the activity above. If nothing can be predicted with confidence, return an empty list — an empty briefing is better than a guessed one.
