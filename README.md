# Beacon

Any place that people ask questions about can put its own information in here.
Visitors then ask in plain language and get an answer with the page it came
from.

A hotel group. A shopping mall. A cinema. A convenience store chain. A corporate
office. A tourist attraction. A railway station.

It runs on your own machines with your own model, so the documents you upload
stay where you put them.

## Why

Walk into any building and somebody there is answering the same questions all
day. Which exit. What time do you close. Do you take dogs. Where is the lift.

The answers already exist. They are in a staff handbook, a laminated notice, a
PDF nobody opens, a sign somebody photographed once. The information is not
missing. It is just not reachable by the person standing there with the
question, and the one person who knows is busy with somebody else.

Nobody reads the manual. Everybody asks the guard.

Beacon is where that information goes so it can answer for itself.

## Two examples

Worth reading before the architecture, because the shape of the problem explains
most of the decisions later.

### Shibuya Station

Eight railway lines, four operators, more than twenty exits, and a famous dog
statue nobody can find.

You upload what already exists: the line map, the exit guide, locker locations,
accessibility notes, opening hours for the shops inside. A photo of a platform
notice works too, because pictures of text get read.

```
   visitor with a phone                  what is in Beacon
           │                                    │
   "which exit for                      exit guide, page 2
    the Hachiko statue?"                "Hachiko Exit, west side,
           │                             follow the signs from the
           │                             JR Yamanote concourse"
           └────────► Beacon ◄─────────────────┘
                         │
      "Take the Hachiko Exit on the west side, following the
       signs from the JR Yamanote concourse. [Exit Guide p.2]"
```

The same upload answers all of these:

```
   "How do I get to Yokohama from here?"
   "Is there a lift down to the Ginza line?"
   "Where are the coin lockers and how much are they?"
   "What time does the last Toyoko line train leave?"
   "Is there somewhere to eat inside the gates?"
```

Nobody wrote a chatbot script for any of that. It needed the documents the
station already has, in one place, searchable.

### A hotel group with a hundred branches

Harder, and the reason the system is built the way it is.

The group publishes a handbook that applies everywhere: cancellation terms,
check in times, the smoking policy. Then each property has things that are only
true there. Breakfast runs later at Ginza. Meguro takes dogs. Chiyoda has no
parking.

```
                    Sagar Hotels
                         │
        ┌────────────────┼────────────────┐
        │                │                │
      Ginza          Chiyoda           Meguro         ... and 97 more
   breakfast to     no parking       allows dogs
      11:00
```

The obvious approach is a hundred handbooks. That works for about a month. Then
somebody updates the cancellation policy and there are a hundred places to
change it. Ninety of them get changed. A year later a guest is told something
wrong and nobody can say when it started being wrong.

Beacon keeps the group handbook once. A property uploads a document only for
what is genuinely different about it. When a question arrives both are searched,
and where they cover the same subject the property wins.

```
   Ginza guest asks                  Chiyoda guest asks
   "what time is breakfast?"         "what time is breakfast?"
          │                                  │
   search Ginza + group              search Chiyoda + group
          │                                  │
   Ginza says 11:00                  Chiyoda says nothing
   group says 10:00                  group says 10:00
          │                                  │
   same subject,                             │
   the property wins                         │
          ▼                                  ▼
     "until 11:00"                      "until 10:00"
```

One stored copy of the policy, two correct answers.

The last part is the whole trick: Ginza overriding breakfast must not quietly
drop the group's cancellation policy along with it. Only the subjects that
actually collide get overridden.

## Who uses it

Three kinds of people, and only two of them have a password.

| | signs in | does |
|---|---|---|
| Visitor | no | browses the directory, picks a place, asks |
| Organization admin | yes | uploads documents, adds branches and staff, watches processing |
| Platform owner | yes | creates organizations and their first admin |

A visitor never signs in. They open the site, pick a place, ask. Anything that
asks a stranger to make an account before telling them the opening hours has
already lost.

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

The browser only talks to Next.js. It calls the API from the server side and
keeps the token in a cookie the page cannot read, so a scripting bug in the
frontend cannot walk off with somebody's session.

The API serves requests and never opens an uploaded file. That happens in the
worker, a separate process, because an uploaded file is untrusted input and a
document parser is a good place to find a bug. The worker is also the only image
carrying Tesseract, which keeps the API image about 150MB smaller.

Postgres does three jobs at once: ordinary rows, vector search through pgvector,
and full text search. One database means one backup, and one transaction when a
new version of a document goes live.

Redis carries the ingestion queue, short term chat memory, rate limiting, and
the progress feed the browser watches while a document is being read.

## Design choices

### Everything external is a provider

The model, the embedder, OCR, object storage and the queue are each reached
through an interface. Nothing in the business logic knows which one it is
talking to.

That cost something to build, and it was worth it for two reasons.

**You will change your mind.** Today the model is Ollama on a laptop. Next month
it is Ollama on a machine with a graphics card. After that it might be Claude,
or GPT, or vLLM on your own hardware. Each of those is one entry in a config
file. The agent code, the retrieval code and the API never learn that anything
moved.

```yaml
llm:
  - name: local      type: ollama              base_url: ${OLLAMA_BASE_URL}
  - name: claude     type: anthropic           api_key: ${ANTHROPIC_API_KEY}
  - name: gpu-box    type: openai_compatible   base_url: http://10.0.0.5:8000/v1
```

**You will want several at once.** One question makes more than one model call,
and they do not all deserve the same model. Working out what to search for is a
small, cheap job. Writing the final answer is not. A tenant handling medical
records may need everything local while the rest are happy on a cloud model.

So the router decides per call, on what a model declares it can do rather than
on its name:

```
   classify the question   ──►  smallest model that can do the job
   write the answer        ──►  best one available
   this org is private     ──►  local only, whatever it costs
   that provider is down   ──►  next one in the list
```

Adding a provider is one new file and one config entry. The router picks it up
on its own, because it reasons about capability and cost rather than about which
vendor it is.

The same holds below the model. OCR is Tesseract today; Google Document AI or
AWS Textract is a swap, and the ingestion pipeline never finds out. Storage is
the local disk today and S3 by changing one word.

### The model does not have to be here

The backend never assumes the model is local and never inspects this machine's
memory or graphics card to decide anything. It knows a web address and a model
name.

So the API can run on a small server while the model runs on a machine with a
real graphics card in another room, or another country. That separation is why
the config above is enough to move it.

### Uploads are processed, not just stored

Dropping a PDF in a folder and searching it later gives bad answers, because the
model is handed whatever the text extractor happened to produce. The pipeline is
where the quality comes from.

```
   file
    │
    ▼  PARSING      pull out the text and how it was laid out
    │
    ▼  OCR          is this real text, or a picture of text?
    │               normal PDF: skipped entirely
    │               scan or photo: read it
    │               mixed file: only the pages that need it
    │
    ▼  CLEANING     tidy it, and keep a copy so this never repeats
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

Two of those matter more than they look.

**OCR only runs when it is needed.** Running it on a normal PDF wastes minutes
for a worse result than the text already in the file. Deciding that is harder
than counting characters: a PDF with a broken font table produces thousands of
characters of pure garbage, and a scanned contract stamped CONFIDENTIAL on every
page looks like it has text everywhere. The gate weighs several signals and
writes down why it chose what it chose, so the reason is always visible.

**Nothing is visible until it has passed.** Every piece is written switched off,
and the last step flips the new version on and the old one off in one
transaction. Upload version four while version three is answering questions and
version three keeps answering until version four proves it works. If it fails,
version three never moved.

The admin console streams all of this live, so uploading is something you watch
rather than something you wait for.

### Two searches, not one

```
   question
      │
      ▼  plan     what kind of question is this, what to search for
      │
      ▼  search   meaning and exact words, at the same time
      │           branch results and group results merged,
      │           branch wins on subjects both cover
      │
      ▼  tools    if no document knows (today's date, a conversion),
      │           call something that does
      │
      ▼  answer   write it from what was found, cite the passages
```

Searching by meaning finds the paragraph about dogs when somebody asked about
pets. Searching by exact words finds the platform number or the price they
typed. Either alone misses what the other catches, so both run and the results
are merged.

### Keeping organizations apart

If one company's documents can ever reach another company's visitor, nothing
else about this matters. The rule is that no request ever touches two
organizations, and it is enforced in four independent places because any one of
them can have a bug in it.

```
   1  token         says which organization you are, and nothing in the
                    request can change it

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
they see what was published for everyone and never one branch's private
material.

## Tech stack

| | |
|---|---|
| API and worker | Python 3.12, FastAPI, SQLAlchemy 2.0 |
| Database | PostgreSQL 16, pgvector for meaning, tsvector for exact words |
| Queue, cache, live updates | Redis 7 Streams |
| Model | Ollama by default. Anthropic, OpenAI, Gemini, vLLM and anything OpenAI compatible are one config entry away. |
| Embeddings | nomic-embed-text, 768 dimensions |
| OCR | Tesseract, behind an interface |
| Web | Next.js 15, TypeScript, no UI framework |
| Tests | pytest, 232 of them |

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
If it is this one, the default already points at it. On Windows, Ollama only
listens to itself until told otherwise, so Docker cannot reach it:

```powershell
[Environment]::SetEnvironmentVariable("OLLAMA_HOST", "0.0.0.0", "User")
```

Quit Ollama from the tray, start it again, then:

```bash
make seed
make web-install
make web
```

Open http://localhost:3000.

### Try this

1. Pick Sagar Hotels and ask what time breakfast is. You get 7:00 to 10:00,
   cited to the group handbook.
2. Sign in at `/admin` as `admin@sagarhotels.example` with the password
   `demo-password-12345`.
3. Open Knowledge, drop in a document or a photo of a notice, and watch it move
   through the stages live.
4. Ask the public site about what you just uploaded. It answers, and cites your
   file.

To see a branch override, the demo has a front desk account per property. Ask as
Ginza and breakfast runs to 11:00; ask as Chiyoda and it runs to 10:00. Same
stored knowledge, both answers correct. Those accounts are for the API at
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

Two of them are named after what they protect: one organization can never
retrieve another organization's data, and a new version never replaces a working
one until it has proved itself.

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
