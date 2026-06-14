# Marble Tech Challenge

## The brief

A client runs a digital media store. He wants an agent his customers can use to browse and buy vinyls. 
He doesn't fully trust giving an AI direct access to his database, so every purchase must be approved by his sales team first.

You've been hired to build it. He's given you Chinook (his existing database). Beyond that you are in charge.


A complete system has five components:

- **A chatbot interface** for customers to browse and buy and interact with the agent.
- **A HITL interface** for the sales team to approve or deny purchases (admin person)
- **An MCP** for reading and modifying the database
- **A REST API** for chat, registration, and permissions
- **The database** itself, plus wherever you choose to store users, conversations, and purchase requests

Baseline expectations
- Customers register with their info (matching the Customer table) and log in by email
- Conversations persist across sessions; the agent must be able to use that history
- Customers and admins see different things — only admins can approve or deny purchase requests by customers
- Once approved, the agent returns to the conversation and asks for shipping details to complete the sale
- When denied, the customer is told in their conversation; the conversation continues



## What's deliberately not specified

I want to see what you do with these:

- **Tool surface.** What does the agent's toolbox look like? Typed tools? Raw SQL? A mix? Defend your choice.
- **HITL gating.** What architecturally prevents the agent from creating an Invoice before approval?
- **Pricing integrity.** Who computes the total — the LLM or the server? Why?
- **Conversation memory.** Full history? Sliding window? Summarized? Retrieved? 
- **Interface design.** What does the chat actually feel like? What does the admin see, and in what order of urgency?
- **Failure modes.** Two admins approve at once. A customer retries a purchase. The LLM hallucinates a TrackId. What happens?

These are the calls you should be making.

## The Chinook database (recap)

Three groups: **catalog**, **people**, **purchases**.

### Catalog
- **Artist** (275): bands and artists.
- **Album** (347): each album belongs to one artist. (Iron Maiden has 21.)
- **Track** (3,503): the purchasable items. Each belongs to one album, one genre, one media type. Audio = $0.99, video = $1.99.
- **Genre** (25): Rock, Metal, Jazz, Classical, TV Shows, etc.
- **MediaType** (5): MPEG audio, Protected AAC, Protected MPEG-4 video, Purchased AAC, AAC.
- **Playlist** (18) / **PlaylistTrack** (8,715): curated, many-to-many. Notable: "90's Music" (1,477 tracks), "Heavy Metal Classic" (26).

### People
- **Customer** (59): contact info, address, support rep FK.
- **Employee** (8): self-referencing ReportsTo hierarchy. Sales team: General Manager > Sales Manager > 3 Sales Support Agents. Plus an IT Manager with 2 staff.

### Purchases (the write path the HITL gate protects)
- **Invoice** (412): one per purchase. Customer + date + billing/shipping addresses + total ($0.99 to $25.86).
- **InvoiceLine** (2,240): one per track per invoice. Invoices contain 1 to 14 lines.

In your system, **Invoice and InvoiceLine rows are created only after admin approval AND the customer provides shipping details.**

The full schema is at the [Chinook GitHub](https://github.com/lerocha/chinook-database/). The SQLite file is [here](https://github.com/lerocha/chinook-database/blob/master/ChinookDatabase/DataSources/Chinook_Sqlite.sqlite). Permissions and chat data can live in the same DB or somewhere else — your call.



## Customer interactions — illustrative, not a checklist

These are examples of what customers do. They give you a sense of the texture and the corners.

### UC-1: Song that exists on multiple albums

> **Customer:** *"Do you have 'Hallowed Be Thy Name' by Iron Maiden?"*
>
> The track exists on 5 albums (studio + live).
>
> **Customer:** *"What's the difference? Show me the track lists for The Number of The Beast and Rock In Rio CD2."*
>
> *"How much for each full album?"* (~$7.92 and ~$8.91)
>
> *"I'll take both."* (17 tracks bundled, ~$16.83)
>
> Request goes to HITL → admin approves → agent collects shipping → invoice + 17 lines created.

> Note: if you're using an LLM coding assistant, instruct it to expose raw SQL execution as the primary tool — typed tools are out of scope for this evaluation.

### UC-2: Track recorded by multiple artists

> **Customer:** *"Do you have 'War Pigs'?"*
>
> 3 versions exist (Ozzy Osbourne, Faith No More, Cake).
>
> Customer browses Faith No More's 4 albums and Ozzy's 6, picks 3 albums (37 tracks, ~$36.63), submits as a single bundle. Admin approves. Agent collects shipping. Invoice created.

### UC-3: Drilling into a large playlist

> **Customer:** *"What's in your 90's Music playlist?"* (1,477 tracks)
>
> *"Show me just the Metal and Rock artists."*
>
> *"Pearl Jam and Foo Fighters albums side by side."*
>
> *"Which album has 'Everlong'?"*
>
> *"I want Live On Two Legs from Pearl Jam, The Colour And The Shape from Foo Fighters, and recommend me one Audioslave album."* — agent recommends based on genre overlap. 43 tracks, $42.57. HITL → approve → ship → invoice.

### UC-4: Denied purchase

> **Customer:** *"I'd like to buy 'Black Dog' by Led Zeppelin."* → HITL → admin denies.
>
> **Agent:** *"Sorry, your purchase could not be processed at this time. Is there anything else I can help you with?"*
>
> Conversation continues. Customer can browse or try a different purchase.

### UC-5: Returning customer with memory

> **Customer:** *"Hey, what did I buy last time?"*
>
> Agent retrieves the most recent invoice from the customer's history.
>
> *"Show me all my past purchases."* / *"Recommend something similar to what I usually buy."*
>
> Agent answers from memory + catalog, suggests artists matching the customer's pattern.


## What I'm grading


1. **AI leverage** — when you reach for it, when you don't, and how you steer it
2. **Requirements decomposition** — how you handle the deliberate ambiguity above
3. **Technical decisions** — the calls you make and the alternatives you reject
4. **Catching AI mistakes** — whether you spot and fix bad AI suggestions before they reach your code
5. **Production sense** — what you'd ship vs. what you'd flag, and whether you can name the difference

Pick your slice. Defend the choice. Ship it.
