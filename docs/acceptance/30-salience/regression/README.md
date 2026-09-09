# §14g-51 — regression sample on the CHAT path

An earlier version of this sample drew from registry and settings surfaces.
That was the wrong choice: those pages are near-static and barely touch the
code M42 changes, so a pass there proves little. Settings in particular only
move when something new is added — and when it is, that is a new acceptance
stage, not a regression check.

This sample therefore draws from the paths that can actually regress: the
**chat path** — planner, resolution ladder, tool dispatch, HITL pause and
resume, SSE streaming, the A2UI answer, and the run trace.

Six scenarios were drawn at random (the draw is recorded in the transcript)
and replayed live on the M42 build using the archived campaign's own
prompts, with `ambient_salience_mode=off` and `ambient_enabled=false` — the
M42 defaults.

Answer **prose is nondeterministic**, so the claim here is structural: the
same plan card, the same route rung, the same nested rails, the same gate
behaviour, the same trace shape, the same run statuses. Where a frame has an
archived counterpart, that counterpart is named so the two can be put side
by side.
