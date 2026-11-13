# Case study: the Common Room

**Role:** designer, product owner and the human in charge. I wrote the rules, the roles, the acceptance checks and every upgrade plan. AI coding agents (OpenAI Codex and Claude Code) wrote the code and tests.
**Stack:** Python standard library only, SQLite, a small vanilla-JS web board, Windows batch launchers.
**Status:** ran daily on my PC from September to early November 2026. It's paused now while I focus on other work.

## The problem

By late summer 2026 I was running several AI assistants at once: Claude Code sessions, Codex, chat assistants, and local models on my own GPU. Each was useful on its own. Together they were a mess:

- I was the message bus, copying results from one chat window into another.
- Nobody could tell who was working on what, so work got duplicated or dropped.
- An assistant saying "done" got treated as done, even when nobody had checked it.
- When a chat closed, what it knew went with it.

## What I built

A local task board with rules, so the assistants coordinate through the board instead of through me.

- **One approval, many jobs.** An orchestrating assistant writes a plan file. I read it and approve it with one command, and every task in it lands on the board at once.
- **Seats with roles.** Every assistant has a seat (worker, reviewer, observer, or the owner, which is me) and its own short-lived key. The room refuses actions outside a seat's role.
- **The builder never reviews its own work.** This is enforced by the database, not by asking nicely.
- **Strikes.** A real rejection costs the builder a strike; two strikes stop the task until I restart it. Paperwork mistakes are free the first couple of times, and a strike caused by my bad instructions can be refunded.
- **A submit gate.** Before a reviewer sees a result, the room checks the report: do the cited files and lines exist, did the test fail first and then pass?
- **"Needs Shane."** Anything waiting on me shows at the top of the board with the exact line to answer it.
- **A stop switch.** Typing STOP pauses every assistant in the room without cancelling anything.

## How it was built

1. **First build (September).** I wrote a build plan and contracts for the store, the board and the adapters (they're in [`docs/contracts/`](docs/contracts)). Codex built it, and a separate Codex reviewer checked each release.
2. **Crew and orchestration.** Claude Code added the worker seats, the `crew` command line, a file relay for chat assistants that can't reach the PC directly, and plan files.
3. **Built inside itself.** From October on, upgrades were tasks *in the room*: one seat built on a fresh branch, a different seat reviewed, and a cutover tool switched the live room over only after the full test suite passed, rolling back on any failure.

## What went wrong, and what I changed

- **Assistants reviewed their own work and passed it.** That's why "builder never reviews" became a database rule.
- **Reports cited files and lines that didn't exist.** That's why the submit gate checks every citation before a reviewer spends time on it.
- **Too much checking.** At one point the test and review effort was bigger than the work itself. I cut it back: one test run and one review for anything that can be undone, and a second independent review only for things that can't (money, backups, my home server).
- **Upgrading a live system is risky.** The cutover tool builds the new version in a second folder, runs the full suite there, switches over, and rolls back automatically if anything fails.

## What I learned

- Clear rules and roles matter more than which AI model you use.
- "Done" should mean someone other than the builder checked it against something written down.
- The human's job is to make the decisions only a human should make, and to make it cheap for the system to ask.
- These are the same skills as running a support or operations team: clear tickets, ownership, escalation, and a record of what happened.

## Try it

See "See it with example data" in the [README](README.md). It runs on any OS with Python 3.12 or newer and needs nothing installed.
