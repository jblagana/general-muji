# tool_notes.md — muji's tool-quirk log (auto-active)

muji appends verified tool quirks straight to `## active` after hitting them
live — these are verifiable external facts (API behavior + fix + recheck
rule), not self-judgments, so no human gate. Only `## active` is injected
into the system prompt; the Boss deactivates stale notes from the 🧠 panel.
`## pending` is kept for manual drafts only (same cap + `## held` overflow
as learned.md).

Format: **Tool / endpoint → quirk → fix.** [verified YYYY-MM-DD] Recheck: <the
signal that tells you the tool changed and this note is stale>.

## pending

## active
- local_search → throws `KeyError: 'method'` when the method parameter is omitted → must explicitly provide a method argument. [verified 2024-05-22] Recheck: tool accepts missing method or returns a clear error message.
- local_search → KeyError: 'method' → Agent reproduced the bug but did not fix it (as it was a demonstration task). [verified 2024-05-22] Recheck: If local_search is called again and succeeds, the bug is fixed.
