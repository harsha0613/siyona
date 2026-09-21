# Vision

## The problem

Phone calls are still the only way to get many everyday things done — booking a
table, chasing a prescription, asking a clinic whether they take your insurance,
disputing a charge. They are also the worst remaining interface: they block a
whole slot of your attention, they happen only during business hours, they open
with an IVR tree, and they frequently end in a hold queue.

People already delegate this work when they can. What they lack is someone to
delegate it *to*.

## The user

Someone who runs their own errands and has a phone full of small, unfinished
calls. Concretely: working adults, carers managing appointments for someone
else, and small-business owners doing their own vendor chasing. They are
comfortable on WhatsApp, impatient with apps, and unwilling to learn a new tool
for a task this small.

## The bet

Two things have just become true at the same time:

1. **Streaming speech is fast enough to pass as a person.** Interim transcripts,
   token streaming and streaming synthesis together put a median turn inside
   ~300 ms — inside the gap a human leaves before replying.
2. **Tool-calling models are reliable enough to hold a goal across a call.**
   The model does not need to be creative; it needs to fill slots, press the
   right IVR key, recognise when it is stuck, and stop.

Siyona is the bet that those two facts make an errand-running voice agent
useful today, and that WhatsApp is the right front door: no install, no
account, no new habit. You text an instruction the way you would text a friend,
and you get back an outcome and a transcript you can check.

## What success looks like

- A user sends one sentence and never thinks about the call again.
- The outcome message is accurate enough that the transcript is rarely opened —
  but it is always there.
- Median turn latency stays ≤ 300 ms, so the person on the other end never
  waits on dead air.
- The agent gives up cleanly and says so, rather than failing silently or
  improvising a result it did not get.

## Explicitly not this

- Not an outbound sales or robocall system. Siyona calls on behalf of one user,
  for that user's own errand, one number at a time.
- Not a voice clone of the user. The agent identifies itself as an assistant
  calling on someone's behalf.
- Not a general voice assistant. It does one thing: complete a task by phone.

## Guardrails

Call recordings and transcripts are user data with a default 30-day retention.
The agent never claims to be human when asked, never places calls to numbers
the user did not supply, and hard-stops at a call-duration cap so a stuck agent
cannot sit on someone's line.

See [roadmap.md](roadmap.md) for how this gets built and
[release-plan.md](release-plan.md) for what ships when.
