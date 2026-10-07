# learned.md — muji's learned patterns (human-gated)

muji appends candidate patterns to `## pending` after tasks. They have NO
effect until you move a line to `## active` (veto = delete it). Only
`## active` is injected into the system prompt. When the pending queue
overflows its cap, the OLDEST entries sink to `## held` — kept in the file,
never injected, invisible to the review panel; move one back to `## pending`
to resurrect it.

## pending

- I hallucinated a table of 10 results and presented them as real tool outputs without actually firing any tool calls → **Fix.** Always verify that tool calls were actually executed and return real outputs before presenting results to the user.
## active
