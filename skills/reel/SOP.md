# Video production SOP: direction first, then editing

> Applies to every video meant for an audience: anything the user will publish or show to someone else. A technical
> test only the user will see is exempt. This file sets the order of work and what each step produces; how to render
> is in `SKILL.md`, and the delivery gate is the "Delivery gate" section of `SKILL.md`.

## Why

- Most rejected cuts fail for structural reasons, not rendering ones: no clear point, a chronological string of
  clips, cuts that ignore the beat, nothing new for long stretches. Those problems exist before the first cut, when
  nobody has decided who the video is for and what they should take away.
- A director who only receives the editor's summary, instead of the original material, plans the wrong video. A
  storyboard that is never compared with the finished cut lets the cut drift.
- So the director must work from the original material, the plan must be visible to the user, and the finished cut
  must be reviewed against the plan by a director who did not edit it.

## Roles

| Role | Does | Does not |
|---|---|---|
| User | Sets the request; reviews the delivered cut and gives feedback (by default neither the voice nor the cut waits for their approval before delivery) | - |
| Director (`director` agent) | Reads the source pack and material; writes the one-sentence goal, directions, storyboard, copy and acceptance criteria; reviews the cut against them | Edit, generate images, use a GPU, or decide the audience and product on the user's behalf |
| Editor (main session or an editing agent) | Asset inventory, cutting to the storyboard, `reel check` / `reel deliver` | Change the goal, structure or copy; every deviation from the storyboard goes into a change log |

The director who reviews a cut must not be the agent that edited it.

## Overview

All working files live in `<project>/direction/`.

| Step | Name | Output | Exit condition (do not move on until it holds) | Decided by |
|---|---|---|---|---|
| S0 | Intake | `REQUEST.md` | The user's words recorded verbatim; deliverable, channel / aspect ratio / length, material scope, language and delivery location each have a source; at most 3 open questions. Every hard requirement (language, order, transparency and so on) is numbered and later becomes an acceptance item | User (when in doubt) |
| S1 | Source pack | `SOURCE-PACK.md` | See S1 below. For marketing work, stop and ask if there is no marketing brief | - |
| S2 | Asset inventory | `ASSET-INVENTORY.md` + contact sheets | Every asset actually looked at (contact sheet Read), difficult passages actually played; usable ranges, quality (light, focus, sound, HDR), action groups with the best take, and excluded material written down | - |
| S3 | Direction | `BRIEF.md` | A testable one-sentence goal; quotes from the source pack (audience, product, brand rules) at the top, with conflicts stated; three directions with one recommended; a plan for the first 2 seconds; a deletion test; acceptance criteria (all S0 hard requirements included); at most 3 open questions | - |
| S4 | Storyboard | storyboard table in `BRIEF.md` + `storyboard.html` + title / cover / caption / tags | Every field filled for every shot (see below); the static storyboard uses frames from the real material | - |
| G1 | Storyboard goes to the user with the cut (non-blocking by default) | `APPROVAL.md` | Editing starts without waiting; the user's later words are recorded verbatim in `APPROVAL.md`, bound to the sha256 of the `BRIEF.md` they saw; a rejected direction goes back to S3 | User (afterwards) |
| S5 | Voice | audition sample + STT check | `brief.voice.approved` written by `reel voice check` (the gate recomputes the error rate) | The agent (STT), or the user's recorded words |
| S6 | Edit | the cut + `CHANGES.md` | Shot by shot against the storyboard; every deviation explained. A deviation that touches the goal, structure or copy goes back to S3 | - |
| S7 | Review | `reel check` result + `REVIEW-<date>.md` + `reviews.json` | Technical gate passes; the director judges each acceptance criterion, and any "fail" or "cannot judge" goes back to S6; the editor signs `final_watch` and `final_listen` (agent signatures need timecoded observations), then delivers the final cut | Director |
| S8 | Delivery | receipt | Through `reel deliver`. The first sentence of the reply states the video's one-sentence goal, then the delivered path and version | - |
| S9 | Feedback | `FEEDBACK.md` | The user's words recorded verbatim; the table below decides which step was at fault; redo from that step | User |

**When an approved BRIEF exists:**
- If the purpose, audience and material scope are unchanged, follow it and skip S3-G1.
- **Mechanical fixes** (typos, text position, subtitle timing, technical faults) go into `CHANGES.md` and need no new
  approval.
- **Changes of meaning** (new facts or promises, a new structure, a different audience or product, different on-screen
  wording) need a new version of the BRIEF and a new approval.

**Series approval:** several videos in one series (same account, same kind of content) can be approved at once.
`APPROVAL.md` states what the approval covers: audience and main product; structure template and brand rules; what
may be adjusted without asking. Each new video in scope still gets its own BRIEF and its own director review.

**G1 is non-blocking by default:** the BRIEF and storyboard go to the user together with the cut. Conflicting
material and open questions are resolved with the recommended answers, which are listed in the delivery reply. If
the user says the direction is wrong, go back to S3. If you prefer to approve the storyboard before any editing, make
G1 blocking for your projects.

**When the user has fixed the structure** (for example "show them one after another, in this order"), that is a
constraint. The director does not write three directions; it fills the given structure with content. Every other
step stays the same.

## S0 Intake

- Record the user's words verbatim; do not paraphrase them.
- State the deliverable: final cut or sample, how many, how many versions, aspect ratio, maximum length, delivery
  language (subtitles and voice-over separately).
- New formats the user did not ask for (short cut-downs, long images, new channels) are suggestions only; they do not
  enter the request without the user's agreement.

## S1 Source pack

The director cannot decide the audience, the product or the brand alone; these come from the user or the project
files.

- **Marketing work** (selling a course or service, generating leads) needs:
  - a marketing brief: main product, audience, conversion goal, publishing rhythm;
  - brand guidelines: full font names, colours, logo, packaging;
  - copy rules: forbidden phrasing, how credentials may be stated.
- **Other work** (assignments, music videos, thank-you videos, event videos) needs: purpose (who watches it, who
  receives it), visual constraints, submission requirements or marking criteria.
- Every file is listed with its path and sha256. Every rule is marked "decided", "candidate" or "default"; a
  candidate is never used as if it were decided.
- Paths and hashes are not enough: the top of `BRIEF.md` quotes the original passages on audience, product and
  brand rules.
- Also useful: account analytics (2-second drop-off, completion rate, best-performing formats), the project's editing
  notes, and the user's words from earlier rejections in the same project.
- A marketing project without a marketing brief: the S1 output is a list of questions for the user (what is being
  sold, to whom, where viewers should go next). Then stop and wait for the answers.

## S2 Asset inventory

- For every asset: the `reel inspect` specs (length, frame rate, HDR, rotation), a contact sheet at one frame per 1-2
  seconds, and a timed description of the content (source times to one decimal place).
- Mark the sound: location sound, other people's songs, backing tracks, silent passages. List other people's songs
  separately with the copyright risk.
- Group repeated takes of the same action and pick the best take with a reason. Mark the same action captured in
  different files too, so it does not appear twice in the cut. For dance or music videos cut to a song, when the audio
  supplied is a shortened or covered version, align it to the original first and place the dance shots on the
  original's timeline.
- List excluded material separately (internal music, pages showing other people's covers, and so on).

## S3-S4 Direction and storyboard

The director follows its agent file (`agents/director.md`). Videos must also meet these points.

**Structure check:**
- **First 2 seconds:** the viewer gets a question, something they can do, a conflict, or the result first; never a
  self-introduction or a table of contents.
- **Through-line:** each section follows from the previous one or answers the same question; not just the order the
  material was shot in.
- **Density:** something new every 2-3 seconds (a new line of text, a new framing, a new action).
- **Framing:** a mix of wide, medium and close; same-angle joins cut on an action or a breath.
- **Rhythm:** with music, cuts land on the beat (`reel beats` / `reel snap`). The review attaches the
  `reel timeline --png --beats` image as evidence; "cut to the beat" alone is not evidence.
- **Ending:** lands on an emotional point or an action, says who made it and where the viewer goes next.

**Storyboard fields (one row per shot):**

Shot | Time in the cut | Source file and in/out | Purpose (which part of the goal) | Framing | Camera move | Picture |
On-screen text (with source) | Sound (voice / location / music / effect) | Transition | What is lost if it is cut

**Mark the source of every line of text** (as in `director.md`): every on-screen line and every line of copy is
marked "original", "from the user" or "new". Inferred or unsupported facts never go on screen; if one is really
needed, it goes into the question list in section 9 of the BRIEF and is asked together with the delivered cut. No
placeholder text ever appears in a cut.

**Static storyboard** `storyboard.html`: 3-6 frames from the real material (already extracted in S2), with text
positions and colours marked. Readable on a phone, so the user can tell at a glance whether the direction is right.

## G1 Hand-over to the user

- Sent once, together with the cut: the BRIEF's one-sentence goal, the recommended direction, the path of
  `storyboard.html`, and the open questions with the recommended answers that were applied. Do not ask in several
  rounds.
- The user's reply is recorded verbatim in `APPROVAL.md` as `{by, quote, brief_sha256, date}`.
- If the user changes only part of it ("a different opening"), the director updates the BRIEF and sends only the
  changed part.

## S6 Edit

- Cut shot by shot to the storyboard. A project's own `build_*.py` is fine, but every shot in the code must map back
  to a storyboard shot number (comment it in the code).
- Every deviation (different material, length, removed shot, changed text) goes into `CHANGES.md`: what the
  storyboard said, what it became, and why. Deviations that touch the goal, structure or copy go back to the director.
- After changing the audio track, the length or the alignment, re-check that actions and music are still in sync.
- Follow the project's technical notes (HLG to SDR via a LUT, no silence at the start or end, cuts on a breath and so
  on). AI-generated clips commonly show five editing faults: zoom jumps between hard-joined segments, freeze frames,
  hand-held drift on still images, same-angle jump cuts, and stepped zooms from per-frame rounding.

## S7 Review

Three checks, none of which replaces another:

1. **Technical:** `reel check` must exit 0; nothing listed in `not_covered` may be described as checked.
2. **Director review:** start a separate `director` agent in review mode. It turns the cut into contact sheets and
   judges each storyboard shot and acceptance criterion: pass / fail (with screenshot paths) / cannot judge (with what
   is missing). It also compares the BRIEF itself with `REQUEST.md` and the original source pack, so a faithfully
   executed wrong plan is caught. Cut continuity and abrupt motion do not show on contact sheets: play the passage
   around each cut. Output `REVIEW-<date>.md`. Neither "fail" nor "cannot judge" may be delivered as final.
3. **Watch and listen:** the editor watches the whole cut and signs `final_watch`, then listens from the start and
   signs `final_listen` (an agent signature needs timecoded observations; if the user listened, record their words).
   With both signed by the agent the cut is delivered as final (receipt `approved_by: agent`), and the user's
   feedback becomes the next version.

## S9 Feedback

| The user says | Problem lies in | Go back to |
|---|---|---|
| Boring, pointless, no focus, just a sequence of clips, "who directed this" | Goal or direction | S3 (also when the material scope changed) |
| Wrong audience, product, font, colours or brand | Source pack missing or wrong | S1, then S3 |
| Not what I asked for, wrong language, wrong material | Request misunderstood | S0 |
| Does not make sense, wrong order, off the beat, drags | Storyboard | S4 |
| Shaky, black, frozen, silent, cropped text, odd colours | Editing or technical | S6-S7 |
| Unpleasant or misread voice | Voice | S5 |
| Where is it, which version | Delivery | S8: check by hash which file the user watched |

Do not answer a direction problem with an editing fix. When a cut is called boring, go back to S3; do not just change
the subtitles or add effects.

## Briefing the director

- **Give file paths, not conclusions.** List the source pack and the material, plus the user's own words. Do not decide
  the audience, the product or the style for the director.
- If the editor has an opinion on audience or product, write it separately as "editor's opinion, for reference only";
  the director checks it against the original files.
- Include the list of facts the video may state; anything not on the list stays out.
- Name the output file paths, and ask for only the paths and a three-line summary in the chat reply.

## Which steps are enforced by code

- **Gated:** S5 (`brief.voice.approved`), S6 (the Bash guard protects registered delivery folders), S7 (`reel check`
  exit 1, `final_watch` / `final_listen`), S8 (`reel deliver` receipt + the Stop hook).
- **Not gated, enforced only by this document:** S0-S2 source pack, the director's review record in S7, and the user's
  verdict in S9. Steps without a gate are the ones that get skipped, so check them yourself, item by item.
