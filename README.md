# Beacon

Every business already knows the answers to the questions it gets asked all day.
What time is breakfast. Do you take dogs. How late is the sauna open. The answers
sit in a handbook, a rate card, a laminated notice at the front desk.

The trouble is that they sit there in a filing cabinet, and the person who can
read them out is busy.

Beacon takes those documents and lets anyone ask them questions, in plain
language, and get an answer that cites the page it came from.

```
        a visitor                          the front desk binder
            |                                       |
   "what time is breakfast?"          "Breakfast is served 7:00 to 10:00
            |                          in the main dining room, daily."
            +---------> Beacon <-------------------+
                          |
              "7:00 AM to 10:00 AM  [Guest Handbook p.1]"
```

## The problem that makes this interesting

One hotel group. Three properties. The group publishes a handbook that applies
everywhere, and then Ginza starts serving breakfast an hour later than everyone
else.

Now the same question has two correct answers, and which one is right depends
entirely on who is asking.

```
                    Sagar Hotels
                         |
      +------------------+------------------+
      |                  |                  |
    Ginza             Chiyoda            Meguro
   breakfast          (nothing            allows
   until 11:00         special)            pets
```

The naive fix is to write three handbooks. Then somebody changes the cancellation
policy and you have three places to change it, and a year later they disagree
with each other and nobody notices until a guest is told the wrong thing.

Beacon stores the group handbook once. A property adds a document only for what
is genuinely different about it. When somebody asks about breakfast:

```
  Ginza guest asks               Chiyoda guest asks
        |                              |
  search Ginza + group           search Chiyoda + group
        |                              |
  Ginza says 11:00               Chiyoda says nothing about breakfast
  group says 10:00                     |
        |                        group says 10:00
  same subject, so                     |
  the branch wins                      |
        v                              v
     "until 11:00"                "until 10:00"
```

One stored copy. Two different correct answers. The group policy on everything
Ginza did not override still applies, untouched.

## Who uses it

There are three kinds of people here, and only two of them have a password.

```
   visitor              org admin             platform owner
  (no login)           (runs one org)        (runs the deployment)
       |                     |                       |
  browse companies      upload documents      create organizations
  ask questions         add branches          create their first admin
                        manage people         publish or hide them
                        watch processing
```

A visitor never signs in. They open the site, pick a company, and ask. That is
the whole experience.

## The bit that had to be got right

If one hotel's documents can ever be seen by another hotel's guest, nothing else
about the system matters.

So the rule is: **no request ever touches two organizations.**

It is enforced in four independent places, because any one of them can have a bug
in it.

```
  1. the token         says which organization you are, and nothing you
                       send in the request can change that

  2. the query         every SQL statement filters on your organization

  3. the database      Row Level Security refuses rows from anywhere else,
                       even if layers 1 and 2 are both wrong

  4. the connection    the app connects as a role that cannot bypass that
                       policy, and refuses to start if it can
```

Layer 3 is the one that actually saves you. A careless `WHERE` clause is an
ordinary mistake; PostgreSQL simply will not return the rows.

The public site is the one deliberate exception, and it is worth being exact
about it. A visitor has no token, so the organization comes from the web address
instead. What does not change is the scope: that address resolves to exactly one
organization, and the visitor carries no branch, which means they see what is
published for everyone and never one property's private material.

## What happens to a document you upload

This is the part worth watching, and the site lets you watch it.

```
  you drop a file
        |
        v
  +-------------+
  |   PARSING   |  pull out the text and how it was laid out
  +-------------+
        |
        v
  +-------------+  is there real text in here, or is it a picture of text?
  |     OCR     |  a normal PDF: skipped entirely
  +-------------+  a scan, or a photo: read it with OCR
        |          a mixed file: OCR only the pages that need it
        v
  +-------------+
  |  CLEANING   |  tidy the text, keep a copy so this never repeats
  +-------------+
        |
        v
  +-------------+  cut it at its own headings, never mid subject
  |  CHUNKING   |  "Pet Policy" and "Smoking Policy" stay separate
  +-------------+
        |
        v
  +-------------+
  |  EMBEDDING  |  turn each piece into numbers a search can compare
  +-------------+
        |
        v
  +-------------+  write everything, but marked invisible
  |  INDEXING   |
  +-------------+
        |
        v
  +-------------+  did every piece land? does a test search find it?
  | VALIDATING  |  if not, stop here and keep the old version live
  +-------------+
        |
        v
  +-------------+  make the new version visible and the old one not,
  | ACTIVATING  |  in one step that cannot half happen
  +-------------+
        |
        v
      ready
```

Two things in there matter more than they look.

**OCR only runs when it is needed.** Running it on a normal PDF wastes minutes
per document for a worse result than the text already in the file. Deciding
that is harder than counting characters: a PDF with a broken font table happily
produces thousands of characters of garbage, and a scanned contract with a
"CONFIDENTIAL" watermark on every page looks like it has text on every page. The
gate weighs several signals and writes down why it chose what it chose, so you
can always see the reason.

**Nothing is visible until it has passed.** Every piece is written switched off,
and only the last step turns the new version on and the old one off, together.
Upload version four while version three is answering questions, and version three
keeps answering until version four has proved it works. If it fails, version
three never moved.

## Asking a question

```
  question
     |
     v
  plan      what kind of question is this, and what should we search for
     |
     v
  search    two searches at once: meaning, and exact words
     |      the branch result and the group result are merged, and where
     |      they cover the same subject, the branch wins
     v
  tools     if the answer is not in any document (today's date, a rate
     |      conversion) call something that knows
     v
  answer    write it from the passages that were found, and cite them
```

The search runs two ways at once on purpose. Searching by meaning finds the
paragraph about dogs when you asked about pets. Searching by exact words finds
the room number or the price you typed. Either alone misses things the other
catches, so both run and the results are merged.

## Running it

You need Docker. You also need Ollama, which is what actually writes the answers,
and it does not have to be on this machine.

```bash
cp .env.example .env
docker compose up -d --build     # postgres, redis, api, worker
```

On whichever machine runs Ollama:

```bash
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
```

If that is a different machine, put its address in `.env` as `OLLAMA_BASE_URL`.
If it is the same machine, the default already points at it. On Windows, Ollama
listens only to itself until you tell it otherwise, so Docker cannot reach it:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Then quit Ollama from the tray and start it again.

Load the demo hotel group and start the website:

```bash
make seed
make web-install
make web
```

Open **http://localhost:3000**.

### Try this, in order

1. Pick Sagar Hotels and ask *what time is breakfast*. You get 7:00 to 10:00,
   cited to the group handbook.
2. Sign in at `/admin` as `admin@sagarhotels.example` with `demo-password-12345`.
3. Go to Knowledge, add a document, and watch it move through the stages live.
4. Ask the public site about what you just uploaded. It answers, and cites your file.

To see the branch override, the demo has front desk accounts for each property.
Ask as Ginza and you get 11:00; ask as Chiyoda and you get 10:00. Both answers
come from the same stored knowledge. Those accounts are for the API, at
http://localhost:8000/docs, since the website itself has no visitor login.

### The platform owner

```bash
make owner email=you@example.com     # prints a password, once
```

Sign in at `/owner`. From there you create organizations, give each one its first
administrator, and decide which ones appear on the public site.

An owner deliberately **cannot** read any organization's documents, searches or
conversations. They create tenants; they do not look inside them. If they need
to, they make themselves an account in that organization, which is recorded.

## What it is built from

| | |
|---|---|
| **API and worker** | Python, FastAPI, SQLAlchemy |
| **Storage** | PostgreSQL, with pgvector for meaning search and its own full text search for exact words |
| **Queue and live updates** | Redis Streams |
| **Model** | Ollama by default, on any machine. Anthropic, OpenAI, Gemini, vLLM and anything OpenAI compatible are one config entry away. |
| **Reading pictures** | Tesseract, behind an interface, so Google Document AI or AWS Textract is a swap rather than a rewrite |
| **Website** | Next.js and TypeScript |

Nothing in the business logic knows which of those it is talking to. Every one of
them sits behind an interface, which is what makes the model choice a line in a
config file rather than a project.

## Why the model can live somewhere else

The backend never assumes the model is local and never looks at this machine's
memory or graphics card to decide anything. It knows a web address and a model
name. That means the API can run on a small server while the model runs on a
machine with a proper graphics card, and moving from Ollama to something else
later changes configuration rather than code.

## Reading further

| | |
|---|---|
| [architecture.md](docs/architecture.md) | how the pieces fit |
| [tenant-isolation.md](docs/tenant-isolation.md) | the four layers, and what each one catches |
| [hybrid-search.md](docs/hybrid-search.md) | why two searches, and how they are merged |
| [versioning.md](docs/versioning.md) | how a new version goes live without a gap |
| [providers.md](docs/providers.md) | adding a model provider |
| [api.md](docs/api.md) | every endpoint, with real responses |
| [deployment.md](docs/deployment.md) | running it somewhere other than a laptop |
| [tradeoffs.md](docs/tradeoffs.md) | what was chosen, and what was given up |

## Tests

```bash
make test               # no services needed
make test-integration   # needs postgres and redis
make test-all
```

The two that matter most are named after what they protect: one hotel can never
retrieve another hotel's data, and a new version never replaces a working one
until it has proved itself.
