# Beacon

Turn the information a place already has into an AI concierge that people can simply talk to.

Hotels, railway stations, shopping malls, tourist attractions, offices and other places can give Beacon their own information. Visitors can then ask questions in their preferred language and get clear answers based on that information.

## Talk to your place in any language

Visitors should not have to speak the language of the organization.

A visitor can ask a question in their preferred language and Beacon can answer in the same language using the knowledge provided by the organization.

The organization only needs to maintain its knowledge once. The same information can then help visitors from different countries without creating a separate knowledge base for every language.

For example, a hotel can provide information about breakfast, check in, facilities, nearby places and hotel rules.

One visitor can ask:

> What time does breakfast end?

Another visitor can ask the same question in Japanese.

Beacon uses the same hotel knowledge to answer both.

This is especially useful for hotels, tourism, transportation and large public facilities where visitors come from many countries.

## How Beacon works

An organization adds the information it already has, such as:

• Handbooks  
• PDFs  
• Notices  
• Guides  
• Images and scanned documents  
• Location specific information  

Beacon processes this information and makes it available for conversations.

A visitor simply chooses a place and asks a question.

```text
        Organization
              │
       Add its information
              │
              ▼
           Beacon
              │
       Understand the question
              │
       Find the right information
              │
              ▼
          Answer the visitor
```

Answers can show where the information came from, allowing visitors to check the source when needed.

## One organization, many places

A large organization may have many locations.

For example, a hotel group with 100 branches may have information that applies to every hotel, along with information that is different for each branch.

Beacon allows shared knowledge to be maintained once while each location can add its own information.

If the group says breakfast is available until 10 AM, that can apply to all hotels.

If the Ginza hotel serves breakfast until 11 AM, the Ginza location can provide its own information.

When a visitor asks a question, Beacon uses the information for that location together with the shared organization knowledge.

## Built for real visitors

The visitor does not need to understand how Beacon works.

They do not need to search through PDFs, read long notices or find the right page on a website.

They simply ask.

```text
Visitor:

Where can I find the nearest elevator?

Beacon:

The nearest elevator is beside Exit 3.
It is accessible from the main entrance.

Source: Station Accessibility Guide
```

The same experience can work across different places such as:

• Hotels  
• Railway stations  
• Shopping malls  
• Tourist attractions  
• Cinemas  
• Convenience store chains  
• Corporate offices  
• Public facilities  

## From documents to conversations

Beacon turns existing information into something people can interact with.

When an organization uploads a document, Beacon:

```text
Document
   ↓
Read and extract information
   ↓
OCR when needed
   ↓
Clean and split into useful sections
   ↓
Create embeddings
   ↓
Index for search
   ↓
Validate
   ↓
Make available for conversations
```

A new document version stays hidden until processing and validation are complete. The existing version remains available until the new version is ready.

This means an organization can update its knowledge without making incomplete information visible to visitors.

## Finding the right information

When a visitor asks a question, Beacon searches the organization's knowledge using both meaning and exact text.

Meaning based search helps find information that is expressed differently from the visitor's question.

Exact text search helps with things such as:

• Platform numbers  
• Prices  
• Opening hours  
• Room numbers  
• Names  
• Specific terms  

Beacon also understands the visitor's location within the organization.

If the same subject has different information for different branches, the information for that specific branch takes priority.

## Private by design

Some organizations may not want their internal information sent to an external AI service.

Beacon can be deployed inside the organization's own environment.

The model and embedding system can run locally or on infrastructure controlled by the organization. This can be a local machine, an internal server or a private GPU server.

For example, a hotel or railway station could run Beacon on its own local network.

Visitors connect to the facility WiFi, open the Beacon interface and ask questions. The application can then use the local model and local knowledge without requiring the organization's information to leave that environment.

```text
                 Organization
                       │
                 Local network
                       │
                ┌──────┴──────┐
                │    Beacon   │
                │    Server   │
                └──────┬──────┘
                       │
              Local model and
               local knowledge
                       │
                ┌──────┴──────┐
                │  Visitors   │
                │  on WiFi    │
                └─────────────┘
```

The current development setup uses a local model through Ollama.

Beacon does not depend on one particular model vendor. The model and embedding systems are kept separate from the rest of the application, so an organization can choose the setup that fits its privacy, cost and infrastructure requirements.

## Your knowledge stays under your control

Beacon is built around a simple idea:

**The organization owns the knowledge. Beacon makes that knowledge easy to talk to.**

The organization decides what information becomes available to visitors.

Its knowledge can be shared across locations while location specific information can be managed separately.

The same knowledge can power conversations through a public website, a local network, a kiosk or other interfaces.

## Designed for organizations

Beacon supports three types of users.

### Visitor

Visitors do not need an account.

They can:

• Browse available places  
• Select a place  
• Ask questions in their preferred language  
• Continue a conversation  
• See the source used for an answer  

### Organization administrator

Administrators can:

• Manage their organization  
• Add and manage locations  
• Upload documents  
• Add location specific information  
• Track document processing  
• Manage the knowledge available to visitors  

### Platform owner

The platform owner can:

• Create organizations  
• Create the first administrator  
• Manage the Beacon platform  

Platform owners cannot automatically read an organization's documents, searches or conversations.

## Built for privacy and isolation

Beacon is designed as a multi organization system.

Each organization's data is protected at several levels:

```text
Authentication
      ↓
Organization scope
      ↓
Query filters
      ↓
PostgreSQL row level security
      ↓
Database permissions
```

The public visitor experience is handled separately. Visitors can access a place without receiving access to the organization's private administration area.

## A flexible AI system

Beacon keeps the AI provider separate from the application.

The same system can work with:

• Ollama  
• OpenAI compatible models  
• Anthropic  
• Gemini  
• vLLM  
• Private model servers  

This also makes it possible to choose different models for different tasks.

A smaller model can handle simple classification while a stronger model can handle the final answer. An organization can also use a local model for private information and another model where appropriate.

The model does not need to run on the same machine as Beacon.

## Architecture

The visitor experience is handled through Next.js.

The application server communicates with the Beacon API.

```text
                    Browser
                       │
                    Next.js
                       │
                    FastAPI
                       │
             ┌─────────┼─────────┐
             │         │         │
        PostgreSQL    Redis      AI
             │         │         │
          pgvector   Streams   Model
          tsvector
```

Document processing happens separately from the API.

```text
Upload
  ↓
FastAPI
  ↓
Redis
  ↓
Worker
  ↓
Parse and OCR
  ↓
Chunk and embed
  ↓
PostgreSQL
```

The API does not directly open uploaded files. A separate worker processes them, keeping document processing isolated from the main application.

## Technology

### Backend

• Python 3.12  
• FastAPI  
• SQLAlchemy 2.0  
• PostgreSQL 16  
• pgvector  
• PostgreSQL full text search  
• Redis 7 Streams  

### AI

• Ollama  
• Qwen 2.5 3B  
• nomic embed text  
• Anthropic  
• OpenAI  
• Gemini  
• vLLM and OpenAI compatible servers  

### Document processing

• Tesseract OCR  
• Document parsing  
• Text cleaning  
• Semantic chunking  
• Embeddings  

### Frontend

• Next.js 15  
• TypeScript  
• React  

### Testing

• pytest  
• 232 tests  

## Run Beacon locally

Start the services:

```bash
docker compose up
```

Start the local models:

```bash
ollama pull qwen2.5:3b
ollama pull nomic-embed-text
```

Seed the database:

```bash
make seed
```

Install the web application:

```bash
make web-install
```

Start the web application:

```bash
make web
```

## Testing

Run the test suite:

```bash
make test
```

Run integration tests:

```bash
make test-integration
```

Run everything:

```bash
make test-all
```

## Demo

The included demo uses a hotel setup called **Sagar Hotels**.

It demonstrates:

• Visitor conversations  
• Hotel knowledge  
• Document upload  
• Document processing  
• Location specific information  
• Shared organization knowledge  
• Branch specific overrides  
• Source based answers  

For example, the organization can have a common breakfast policy while the Ginza and Chiyoda branches provide different breakfast timings.

A visitor only needs to ask the question.

Beacon finds the information that applies to that location and uses it to answer.

## What Beacon is trying to solve

People already have the information they need.

It is sitting inside PDFs, handbooks, signs, notices, websites and internal documents.

The problem is that visitors should not have to search through all of that information themselves.

Beacon turns that information into a conversation.

**Give a place its knowledge. Let people simply ask.**
