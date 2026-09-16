# Beacon

A company uploads its handbooks and policies. Anyone can then ask it questions
in plain language and get an answer with the page it came from.

It runs on your own machine, with your own model. Nothing leaves the building
unless you point it somewhere else.

## Why

Every business answers the same questions all day. What time is breakfast. Do
you take dogs. How late is the sauna open. The answers already exist in a
handbook or a laminated notice at the front desk, but the person who can read
them out is busy.

You could paste the handbook into a chatbot and be done in an afternoon. That
works until the second location opens.

Now the group has a handbook that applies everywhere, and Ginza starts serving
breakfast an hour later than the rest. The same question has two correct
answers, and which one is right depends entirely on who is asking.

```
                     Sagar Hotels
                          │
        ┌─────────────────┼─────────────────┐
        │                 │                 │
      Ginza            Chiyoda            Meguro
   breakfast till    nothing special     allows pets
      11:00
```

The obvious fix is three handbooks. Then somebody changes the cancellation
policy and there are three places to change it. A year later they disagree and
nobody notices until a guest is told the wrong thing.

Beacon stores the group handbook once. A property adds a document only for what
is genuinely different about it. When a question arrives, both are searched, and
where they cover the same subject the property wins.

```
   Ginza guest asks                 Chiyoda guest asks
         │                                 │
   Ginza + group                    Chiyoda + group
         │                                 │
   Ginza says 11:00                 Chiyoda says nothing
   group says 10:00                 group says 10:00
         │                                 │
   same subject,                           │
   property wins                           │
         ▼                                 ▼
    "until 11:00"                    "until 10:00"
```

One stored copy, two correct answers. Everything Ginza did not override still
applies to Ginza.

## Who uses it

Three kinds of people, and only two of them have a password.

| | signs in | does |
|---|---|---|
| **Visitor** | no | browses companies, asks questions |
| **Org admin** | yes | uploads documents, adds branches and staff, watches processing |
| **Platform owner** | yes | creates organizations and their first admin |

A visitor never signs in. They open the site, pick a company, ask. That is the
whole experience.

## Architecture

```
   asking a question
   ═════════════════
   browser ──► Next.js ──► FastAPI ──► PostgreSQL     find the passages
                              │
                              └─────► Ollama          write the answer

   uploading a document
   ════════════════════
   FastAPI ──► Redis ──► worker ──► Tesseract         read pictures
               queue        │
                            ├─────► PostgreSQL        store the pieces
                            │
                            └─────► Redis ──► FastAPI ──► browser
                                    progress, while it happens
```

The browser only ever talks to Next.js. It calls the API from the server side
and keeps the token in a cookie the page cannot read, so an XSS bug cannot walk
off with a session.

The API serves requests and never parses an uploaded file. That happens in the
worker, a separate process, because an uploaded file is untrusted input and a
parser is a good place to find a bug. The worker is also the only image that
carries Tesseract, which keeps the API image about 150MB smaller.

Postgres does three jobs: ordinary rows, vector search through pgvector, and
full text search. One database, one backup, one transaction when a new version
of a document goes live.

Redis carries the ingestion queue, short term chat memory, rate limiting, and
the live progress feed the browser watches.

Every external thing sits behind an interface. The model, the embedder, OCR,
object storage, and the queue are all chosen by config. Changing from Ollama to
OpenAI is a line in a file, not a project.

## Getting a document in

This is the part worth watching, and the site lets you watch it happen.

```
   file
    │
    ▼  PARSING      pull out the text and how it was laid out
    │
    ▼  OCR          is this real text, or a picture of text?
    │               normal PDF: skipped
    │               scan or photo: read it
    │               mixed file: only the pages that need it
    │
    ▼  CLEANING     tidy it, keep a copy so this never repeats
    │
    ▼  CHUNKING     cut at its own headings, never mid subject
    │
    ▼  EMBEDDING    turn each piece into numbers a search can compare
    │
    ▼  INDEXING     write it all, marked invisible
    │
    ▼  VALIDATING   did every piece land? does a test search find it?
    │
    ▼  ACTIVATING   switch the new version on and the old one off, together
    │
   ready
```

Two things in there matter more than they look.

OCR only runs when it is needed. Running it on a normal PDF wastes minutes for a
worse result than the text already in the file. Deciding that is harder than
counting characters, because a PDF with a broken font table produces thousands
of characters of garbage, and a scanned contract stamped CONFIDENTIAL on every
page looks like it has text everywhere. The gate weighs several signals and
records why it chose what it chose.

Nothing is visible until it has passed. Every piece is written switched off, and
the last step flips the new version on and the old one off in one transaction.
Upload version four while version three is answering questions and version three
keeps answering until version four proves it works. If it fails, version three
never moved.

## Answering a question

```
   question
      │
      ▼  plan     what kind of question is this, what should we search for
      │
      ▼  search   two searches at once: meaning, and exact words
      │           branch results and group results merged, branch wins
      │           on subjects both cover
      │
      ▼  tools    if no document knows (today's date, a conversion), call
      │           something that does
      │
      ▼  answer   write it from what was found, cite the passages
```

Two searches run on purpose. Searching by meaning finds the paragraph about dogs
when you asked about pets. Searching by exact words finds the room number or the
price you typed. Either one alone misses what the other catches.

## Keeping companies apart

If one hotel's documents can ever reach another hotel's guest, nothing else
matters. The rule is that no request ever touches two organizations, and it is
enforced in four independent places because any one of them can have a bug.

```
   1  token         says which organization you are, and nothing in the
                    request can change that

   2  query         every statement filters on your organization

   3  database      row level security refuses rows from anywhere else,
                    even when 1 and 2 are both wrong

   4  connection    the app connects as a role that cannot bypass that
                    policy, and refuses to start if it can
```

Layer three is the one that saves you. A careless WHERE clause is an ordinary
mistake, and Postgres simply will not return the rows.

The public site is the one deliberate exception. A visitor has no token, so the
organization comes from the web address instead. The scope does not change: that
address resolves to exactly one company, and the visitor carries no branch, so
they see what is published for everyone and never one property's private
material.

## Tech stack

| | |
|---|---|
| API and worker | Python 3.12, FastAPI, SQLAlchemy 2.0 |
| Database | PostgreSQL 16, pgvector for meaning search, tsvector for exact words |
| Queue, cache, live updates | Redis 7 Streams |
| Model | Ollama by default. Anthropic, OpenAI, Gemini, vLLM and anything OpenAI compatible are one config entry away. |
| Embeddings | nomic-embed-text, 768 dimensions |
| OCR | Tesseract, behind an interface, so Document AI or Textract is a swap |
| Web | Next.js 15, TypeScript, no UI framework |
| Tests | pytest, 232 of them |

The backend never assumes the model is local and never looks at this machine's
memory or graphics card. It knows a web address and a model name. So the API can
run on a small server while the model runs on a machine with a real graphics
card, and moving to a different provider later is configuration rather than code.

## Running it

You need Docker, and Ollama somewhere.

```bash
cp .env.example .env
docker compose up -d --build
```

Wherever Ollama runs:

```bash
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
```

If that is a different machine, put its address in `.env` as `OLLAMA_BASE_URL`.
If it is this machine, the default already points at it. On Windows, Ollama only
listens to itself until told otherwise, so Docker cannot reach it:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Quit Ollama from the tray and start it again. Then:

```bash
make seed
make web-install
make web
```

Open http://localhost:3000.

### Try this

1. Pick Sagar Hotels and ask what time breakfast is. You get 7:00 to 10:00,
   cited to the group handbook.
2. Sign in at `/admin` as `admin@sagarhotels.example`, password `demo-password-12345`.
3. Open Knowledge, drop in a document or a photo of a notice, and watch it move
   through the stages live.
4. Ask the public site about what you just uploaded.

To see the property override, the demo has a front desk account per property.
Ask as Ginza and breakfast runs to 11:00; ask as Chiyoda and it runs to 10:00.
Same stored knowledge, both answers correct. Those accounts are for the API at
http://localhost:8000/docs, since the site itself has no visitor login.

### Platform owner

```bash
make owner email=you@example.com
```

That prints a password once. Sign in at `/owner` to create organizations, give
each one an administrator, and choose which ones appear publicly.

An owner cannot read any organization's documents, searches or conversations.
They create tenants, they do not look inside them. If they need to, they make
themselves an account in that organization, and that is recorded.

## Tests

```bash
make test               # nothing else needs to be running
make test-integration   # needs postgres and redis
make test-all
```

Two of them are named after what they protect: one hotel can never retrieve
another hotel's data, and a new version never replaces a working one until it
has proved itself.

## More detail

| | |
|---|---|
| [architecture.md](docs/architecture.md) | how the pieces fit |
| [tenant-isolation.md](docs/tenant-isolation.md) | the four layers and what each catches |
| [hybrid-search.md](docs/hybrid-search.md) | why two searches, and how they merge |
| [versioning.md](docs/versioning.md) | going live without a gap |
| [providers.md](docs/providers.md) | adding a model provider |
| [api.md](docs/api.md) | every endpoint, with real responses |
| [deployment.md](docs/deployment.md) | running it somewhere other than a laptop |
| [tradeoffs.md](docs/tradeoffs.md) | what was chosen, and what was given up |
