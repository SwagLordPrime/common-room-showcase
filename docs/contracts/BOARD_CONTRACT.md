# Common Room local board contract

Clock source: Python datetime system clock; 2026-09-11T20:12:18.804685+00:00 = 2026-09-11 15:12:18 CDT, America/Chicago.

The board is a localhost fixture application. Its prepared Keeper, Scribe and Reader identities demonstrate store states; they are not connected assistant accounts. The automated fictional document worker is now provided separately by common_room/fixture_run.py; see ADAPTER_CONTRACT_2026-09-11.md. The board does not schedule it.

## Local boundary

VERIFIED IN LOCAL SOURCE: common_room/board.py:15, :39, :44, :54 and :85 define the fixed static routes, exact Host/Origin checks, registered-session identity, state filtering and bounded JSON transport. The listener binds to 127.0.0.1. The launcher prints its actual allocated port; use that exact address rather than substituting localhost, which the Host check refuses. Neither the key nor session is placed in a URL. Only a fresh fictional key is printed by the launcher. It is retained inside the ignored runtime folder for that run.

Login accepts the fixture access key in a JSON POST. The resulting opaque session uses an HttpOnly, SameSite=Strict cookie. POST requests require the exact local Origin and a single JSON Content-Length. Session settings are fifteen minutes, at most 256 sessions and 32 concurrent connections; the body limit is 16384 bytes. These are source-defined limits, not production load measurements. Plain HTTP is deliberate for this isolated loopback demonstration; the cookie is not marked Secure. Do not expose this server on a LAN, reverse proxy or live estate service under this checkpoint.

Identity is taken from the registered session, never from message text. Authority is created only in trusted fixture setup; no HTTP grant endpoint is available. Visible tasks, notes and participants are restricted to authorities containing that actor. The board shows relative artifact references and saved hashes as text, without arbitrary file browsing, downloading, URL fetching, shell commands or SQL endpoints. All displayed user content uses textContent. Static CSS and JavaScript are local, with a restrictive content policy and no external assets.

## Visible work and contact

Inbox includes draft, delivered and acknowledged. Working contains claimed tasks; Review contains ready_for_review; Done contains accepted completion; Owner decisions contains waiting_decision. Opening the demo seeds one prepared example per column through the accepted store lifecycle. Prepared results and review notes are labelled as such. This seeding is not the later bounded worker or evidence that a real assistant performed work.

Participants show No fixture contact, Fixture browser active or Fixture contact stale. The source defines contact as an authenticated local HTTP request and uses a sixty-second freshness window. Contact does not establish that an assistant is online. Existing claimed tasks may remain in their last committed lifecycle state after the lease deadline until a subsequent operation handles expiry; the store still refuses expired completion. No running-worker progress is claimed by the card alone.

A submitted draft is distinct from delivery. An assigned worker may acknowledge and claim. A separate reviewer may accept or return work for an owner decision. Pause and resume use the store's owner checks. Lounge posts append notes and create no task or worker. The board itself does not execute document.count jobs.

Request retries retain the same request key after an ambiguous failure within that browser session. This pending envelope is in memory only; it is not durable across reload or browser closure. The underlying store's committed receipts remain durable. Future offline queueing or cross-provider transports require separate work.

## Measured development evidence

- evidence/board-http-red.json and board-http-green.json: nine actual HTTP scenarios, failing before implementation and passing afterward, with source/test/output hash pairs.
- evidence/ui-red.json: retained browser-environment miss, NOT a valid behavior RED. No bundled browser was installed or downloaded as a repair.
- evidence/ui-native-red.json: installed Chrome reaches the real page, then observes the expected absent login controls. This is the valid browser behavior RED.
- evidence/ui-results.json: nine browser checks pass on the actual localhost application, including all columns, contact labels, task creation/delivery, literal hostile HTML, mobile width, session persistence and logout. The process is stopped afterward.
- evidence/board-complete-green.json: the combined protocol, store and HTTP suite passes sixty-two tests. This is builder evidence; separate review is recorded independently.

The builder visually inspected evidence/board-preview.png and evidence/ui-results-mobile.png: the desktop columns and stacked phone layout are readable, the page has no horizontal phone overflow, and the hostile lounge sample appears as literal text. Screenshot appearance does not substitute for behavioral checks. Browser fixtures and profiles stay under .checks, without using a personal Chrome profile. The saved screenshots contain fictional data only.

Review kind and refunds on the board (cr-upgrade, 2026-10-01): the review form has a paperwork checkbox, shown only when returning work; ticking it sends kind paperwork, leaving it clear sends no kind (a substance return, as before). The durable event receipts under a task show a review's kind and paperwork count, and any strike refund (who, strikes before and after, and the written reason). Those lines appear only for events that carried them; older receipts look exactly as they did. The board has no refund button yet: a refund is a resolve_decision with refund true, which the crew command line gains later.

The stop switch on the board (cr-upgrade, 2026-10-01): the state the board serves carries `stop`, {active: false} or {active: true, by, at, reason}, taken from the authorities the viewer can see. While stopped, every view shows a banner (THE ROOM IS STOPPED, who, when, why). Only the owner sees the buttons: Pause everything, which opens a form that stays disabled until STOP is typed exactly, then sends stop_all; and Resume everything, which sends resume_all. The public status page is unchanged.

## Needs Shane and the usage panel (turnons-2026-10-03, job panels)

**Needs Shane** is the first panel on the board, above everything else, for every viewer. It lists everything in the room that is waiting on Shane, oldest first within each kind. Each line says what is waiting in plain words and the exact thing to do: a line to type in the room folder (`crew-shane ...`) or a board click. When the list is empty it says "Nothing needs you." to the owner and "Nothing needs Shane." to everyone else. `crew needs` prints the same list (exit 0 when empty, 2 when not; `--json` prints the items), and the phone push (job notify) sends each new item once.

The list comes from one place, `common_room/needs.py`: `needs_shane(snapshot)` reads the viewer's own `/api/state` and nothing else (no clock, no files, no network). The board server adds the result to `/api/state` as `needs`. Each item is `{id, kind, since, summary, action}`:

| kind | When it is listed | What to do |
|---|---|---|
| `stop` | The room is stopped (Pause everything / `stop-all`). | `crew-shane resume-all`, or Resume everything. |
| `stopped` | A task has two strikes and is not done or cancelled. | `crew-shane reinstate <task> --reason "..."`, or give one strike back with `crew-shane decide <task> --refund --response "..."`. |
| `decision` | A task waits on a decision and its decider is Shane: a root task, or a helper the keeper sent. A review return reads as one. | `crew-shane decide <task> --response "..."` |
| `question` | A Lounge note starting `QUESTION` names Shane or keeper in its first line and is not answered yet. | Answer in the claude-room chat, or `crew-shane note <scope> "answering #<seq>: ..."` |
| `seat` | A seat other than the keeper has no active room key but still has open work (delivered, acknowledged or claimed). Contact that is merely stale is normal idling and is not listed. | Restart the crew (Stop, then Start Common Room Crew.cmd), or withdraw the work with `crew-shane cancel`. |
| `slow` | A decision on a helper claude-room sent, or any other `QUESTION` note, has waited more than 30 minutes (by the room's clock) for claude-room. | Ask claude-room in its chat, or answer it yourself with the line shown. |

A `QUESTION` note counts as answered when a later note from the snapshot owner or claude-room cites it as `#<seq>`, or a later visible `task_resolve_decision` or `task_reinstate` history event by either actor cites it in `detail`. Later means a greater event sequence; `#13` does not answer `#1377`, and `#13770` does not answer `#1377`. Other actors and event kinds do not count. Only the viewer's visible history and the last 100 notes returned by the room are considered. A task stopped at two strikes is still listed once, as `stopped`.

VERIFIED IN LOCAL SOURCE (accepted W1 component `2c7bce4`, binding refreshed 2026-10-09; not a deployment claim).
VERIFIED IN LOCAL SOURCE: `Artifacts/orchestra/intake-2026-10-05/results/room-next/build/worktrees/room-next-w1/common_room/needs.py:74-78`.
VERIFIED IN LOCAL SOURCE: `Artifacts/orchestra/intake-2026-10-05/results/room-next/build/worktrees/room-next-w1/common_room/needs.py:129-149`.

Ids stay the same while the condition holds and change for a new episode: `stop:<at>`, `stopped:<task>:f<fence>`, `decision:<task>:f<fence>`, `slow:<task>:f<fence>`, `question:<seq>`, `slow:note:<seq>`, `seat:<seat>:no-key`. The fence moves every time a task leaves or re-enters a decision. `since` is the time of the event that started the condition, or null when the view does not show one.

Nothing secret appears in any field. Summaries quote room text only in short excerpts. A word that looks like a path to a credential, `.runtime`, secrets, `_Private`, `.env` or the keeper store is shown as `[path hidden]`. A long random-looking token is shown as `[hidden]`; plain hyphenated names and hex hashes stay as written. The participants list also carries `key_active` (true while the seat holds a valid key), which is what the `seat` kind reads.

**Usage** is a panel only the owner sees, under the at-a-glance tiles. `GET /api/usage` (owner only; any other seat gets 403) reads the workers' usage logs, `usage/<seat>.jsonl` beside `room.sqlite` (see `USAGE_FORMAT.md`), read-only. Per seat it shows runs, seconds and tokens for today (from midnight Central) and for the last 7 days. Tokens appear only where the model reported them; headless agents report time only. A damaged line is counted as skipped and never shown, because its text could hold anything. The board asks at most once a minute, or when Refresh usage is pressed.

The old status filter and button called "Needs Shane" now read "Waiting on a decision". They show every task in `waiting_decision`, including helpers whose decider is claude-room, so they are no longer named like the panel.
