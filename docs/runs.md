# Research runs

A run stores the state of `gkai research` so the result can be inspected,
exported or continued after a failure. The ordinary cache is not enough for
this: a process can stop after an external request but before the successful
response is written to the cache. The quota or the money is already spent, and
a repeat run has no way to know it.

A run record holds the identifier, scenario and target, the market, the state,
the application and parser versions, a budget snapshot, a safe configuration
snapshot, timestamps, and the result or the error. It also holds the rest of the
request — the seed keyword and the result limit — because `resume` and `rerun`
rebuild the scenario from the record alone, and anything missing there would
quietly turn the repeat into a different question. Runs saved before schema v3
have neither and read back as unset. Ordered stages are stored
separately, with their states, attempt counts and checkpoints.

A stage fingerprint is the first 32 hexadecimal characters of the SHA-256 of
canonical JSON containing the stage name, target, market, budget and seed
keyword. On `resume`, a finished stage may be skipped only if it has a
checkpoint, its fingerprint matches, and neither the application nor the parser
version has changed.

Changing the application or parser version makes every checkpoint of a run
stale. Resuming then recomputes from scratch and adds an explanation to the
warnings: mixing results produced by different code or parsing versions is not
safe.

`gkai run resume <id>` continues the same run. Reuse is all-or-nothing: a
scenario is one coroutine rather than a chain of separately invocable stages, so
the run store can skip work only when every stage is reusable. A single stale
stage replays the scenario end to end. What softens that is the HTTP cache, not
the run record — and only for requests it actually holds: a call that was made
but whose response never reached the cache, the very case runs exist for, is
paid for again. The checkpoints still
earn their place — they hold the result of a finished run and the reason a stage
was skipped — but they are not a resume point in the middle of collection.

`gkai run rerun <id>` allocates a new identifier and performs the same research
again, leaving the original record untouched.

The configuration snapshot is produced by the regular settings masking. Token
values, client secrets and refresh tokens are never written to the database.
