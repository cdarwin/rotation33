# Rotation33

Rotation33 is a self-hosted web app that helps you decide which record to play.
Point it at your Discogs collection, tell it what kind of mood you're in, and it
suggests a few records that fit. It also keeps track of what you actually put
on, so it can avoid repeating things and get better at suggesting over time.

It grew out of a clunkier habit: exporting a Discogs collection to a CSV and
asking for suggestions in a chat window. Rotation33 does the same job, but as a
real tool that lives on your home network and remembers what you've been
listening to.

## What it does

You pick one of five moods (First Light, Heads Down, Peak, Golden Hour, or After
Dark) and get 3 to 5 records that suit it. Each mood has a rough time of day
attached to it, so when you open the app it guesses which one you probably want
and pre-selects it. You can always override that.

Whatever you've played recently won't come back for a while. By default that
window is three days, and it works at the album level, so a different pressing
of the same record won't slip through as a "new" suggestion.

When you play something, you log it into the current session from whatever
device is handy. That can be one of the suggestions or anything else you grab
off the shelf. Those plays are what the recommendations learn from.

The app pulls your collection from Discogs when you ask it to: artist, title,
styles, and cover art. Only vinyl is pulled in; CDs and cassettes in your
collection are ignored. Sync only ever reads from Discogs, never writes back.
You can also flag a copy as damaged or mark one as sold, and it'll drop out of
future suggestions.

## How a session works

A session starts when you pick a mood. The app hands you a handful of records
that fit that mood and haven't been played lately, each shown with its cover art
so it's easy to match the suggestion on screen to the spine on the shelf. If the
set doesn't grab you, ask for another one.

As you play records, you log them into the session. A session stays open until
you start the next one.

Under the hood, recommendations come down to two things: how well a record's
Discogs styles match the mood you picked, and how long it's been since you last
heard it. The longer something has sat unplayed, the more likely it is to come
up, but there's deliberate randomness in the draw so you don't see the same
ranked list every time.

## A few terms

The model draws a distinction between an album and the physical copies of it,
which matters for both play history and recommendations.

- **Release**: the album itself, regardless of which copy you own. Recency is
  tracked here.
- **Instance**: one specific physical copy. Condition and play history belong to
  the instance.
- **Mood**: one of the five moods you start a session with.
- **Session**: a single sitting. A mood, the records suggested for it, and the
  records you logged as played.
- **Play**: a specific copy, logged as played on a given day.

## Running it

Rotation33 is meant to run as a Docker container next to whatever else you
self-host at home. It's built for one person with no login, on the assumption
that your home network is the boundary that keeps it private. Everything is
stored locally, and nothing reaches out to the internet during normal use.
Discogs only gets contacted when you manually kick off a sync.

This is still an MVP. Real setup and deployment instructions will go here once
there's something to deploy.

## Docs

- [Product requirements](docs/prd/mvp.md) covers the scope, the moods, and the
  full set of requirements and user flows.
- [Technical architecture](docs/rfc/technical-architecture.md) covers the
  component design, storage, sync, and deployment.
- [Recommendation engine design](docs/rfc/recommendation-engine-design.md)
  covers how suggestions are actually generated.
- [Execution plan](docs/plan/execution-plan.md) covers the build order, what
  each phase has to prove, and how requirements map onto it.

## Status

Still in the design phase. The product requirements, the recommendation engine
design, and the technical architecture are drafted and pending review; no code
yet.
