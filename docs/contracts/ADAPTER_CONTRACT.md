# Bounded fictional document worker

Clock source: Python datetime system clock; 2026-09-11T20:31:31.413401+00:00 = 2026-09-11 15:31:31 CDT, America/Chicago.

This adapter accepts fictional document.count jobs in this worktree's .checks or .runtime directories only. It does not connect any assistant, model, account or live estate service. Task body text is input data, including anything resembling commands. Input artifact references are not opened as instructions or followed as URLs; the fixed job counts the body text itself.

## Execute the complete fictional workflow

From this worktree, run:

```sh
python -B -m common_room.fixture_run
```

The program creates a fresh ignored .runtime/adapter-* directory, measures empty-inbox and lounge behavior, delivers a fictional note, runs one worker child and has a separately registered fixture reviewer recompute the result. It prints JSON evidence and exits. No browser server or background loop is left running. The result files remain in that isolated runtime for inspection and are excluded from Git and release manifests. The existing browser demo remains a separately launched local preview; it does not schedule this worker.

MEASURED development demonstration: evidence/fixture-workflow.json records three words, a ready_for_review receipt followed by a done receipt under the reader identity, and a matching artifact digest. evidence/idle-measurement.json records one hundred explicit empty-inbox ticks and one hundred lounge-only ticks. Neither phase added events or started a subprocess, network connection or shell. These are short local measurements, not a long-running service efficiency claim. No model invocation exists in the fixed worker or count child; zero model calls is supported by that source boundary and the idle process/network audit, not by access to a live model service.

## Interface and authority

FixtureWorker(store, registered_worker, timeout=30).run_once() attempts at most one count process per explicit call. It acknowledges delivered work, claims only the assigned worker's allowed job, waits while another current lease exists and skips paused/revoked scopes or tasks needing a decision. Completed cards cannot hide later work or reviews. It reads the accepted store's finite task snapshot; no large-history performance bound is claimed.

The process uses the trusted Python runtime with isolated mode and a fixed count_job.py path. Only bounded JSON stdin is passed; shell=False. The source limits the process timeout to a positive finite value at most thirty seconds, input text to the accepted four-thousand-character body and accepted output to a small fixed JSON shape. The store's claim is three hundred seconds and its existing shared attempt ceiling is two. These are source-defined settings; current tests and independent receipts record their exercised behavior.

Output is exclusively created as fixture-count/<task-id>-f<fence>.json under the granted fixture root. Existing output is never overwritten. Linked or escaping roots/paths are refused through the accepted artifact guard. Authority and the current lease are checked before work, again before writing and by the store at submission. Operator-owned directories must not be concurrently replaced by another local administrator; this inherits the store's explicit filesystem assumption. A cancelled or interrupted job can leave an unsubmitted fixture file, which is never silently reused as an accepted result.

FixtureReviewer(store, separately_registered_reviewer).run_once() checks the expected relative output name, stored file hash, input hash, character count and a separately computed whitespace word count. It records acceptance or an owner decision. This separate fixture identity demonstrates routing and review behavior; it is distinct from the independent agent review required to accept this code build.

## Restart, retry and stop behavior

There is no automatic retry loop or daemon. A new invocation leaves a still-current claim alone. After an interrupted worker's lease expires, the store accounts for that failed attempt before allowing reclamation. A second expired or failed attempt exhausts the shared budget and prevents another child process. Pause, revocation and owner decisions do not erase that budget.

A finish response lost after commit is recognized from the persisted ready-for-review/done task, without creating another artifact. A response lost before submission leaves a claim to expire; the adapter does not guess that it owns a live lease from another runner. Separate runners competing under one worker identity are fenced by the store, not by an assertion in message text. Interrupted orphan files stay local until a later reviewed cleanup.

## Evidence and continuation

The first stub RED is retained in evidence/adapter-red.json, including its missing mock-import seam. evidence/adapter-behavior-red.json records the corrected test seam while worker behavior is still unimplemented; no implementation was added to make the baseline pass. The initial combined GREEN is adapter-green.json. A focused backlog RED in adapter-backlog-red.json records the accidental early card slice; adapter-final-green.json verifies its correction. No old receipt or failed-attempt history was overwritten.

Read evidence/ADAPTER_REVIEW_2026-09-11.md for the independent verdict and REPORT.md for the actual stopping gate. A full release remains separate. Existing build approval covers ordinary continuation to CR-RELEASE after this adapter is independently accepted; live installation or real assistant delivery is not included.
