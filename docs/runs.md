# Research runs

A run stores the state of `gkai research` so the result can be inspected,
exported or continued after a failure. The ordinary cache is not enough for
this: a process can stop after an external request but before the successful
response is written to the cache. The quota or the money is already spent, and
a repeat run has no way to know it.

A run record holds the identifier, scenario and target, the market, the state,
the application and parser versions, a budget snapshot, a safe configuration
snapshot, timestamps, and the result or the error. Ordered stages are stored
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

`gkai run resume <id>` continues the same run and reuses the checkpoints that
are still valid. `gkai run rerun <id>` allocates a new identifier and performs
the same research again, leaving the original record untouched.

The configuration snapshot is produced by the regular settings masking. Token
values, client secrets and refresh tokens are never written to the database.
