# Voice Reservation (Karlsruhe pilot: Min Ramen Bar)

Goal: AI phone agent that answers restaurant calls, takes table reservations, confirms by SMS.
First target: Min Ramen Bar, Waldstr. 19, Karlsruhe (public listing, nothing contacted yet).

## Evidence gathered (public sources, 2026-11-10)
- Instagram bio: "Reservierungen nur per Telefon." Facebook adds a WhatsApp number. No online booking.
- Maps: 4.6 stars, 2,591 reviews, open daily 11:30-22:00, tel +49 721 14597578.
- Read the 60 lowest-rated reviews (via Firecrawl Maps reviews). About 10 are about reservation or phone problems (paraphrased, no names kept):
  - Phone staff told a guest no reservation was needed before 17:00; on arrival at 15:45 the place was full.
  - A booking made by phone at 17:00 for 20:00 (confirmed) was missing on arrival.
  - Someone tried three days to reserve by phone, WhatsApp and Facebook, no answer; the phone is a mobile.
  - A caller asked by phone whether a table was free, was told yes, then was told on arrival there was none.
  - A reserved table still meant long waits; odd party sizes (3, 5) were hard to place.
  - A card-reader double charge, with the guest's phone calls unanswered.
- Most 1-star reviews are about food and service, so the pitch is "never lose a booking call", not "fix the restaurant".
- Only the 60 lowest-rated of 2,591 were read; good reviews were not sampled.

## Pitch
1. Every call answered, even at rush hour. 2. A written, consistent booking record plus SMS confirmation (fixes "booking disappeared", "wrong promise"). 3. Runs in the cloud, so the shop's bad internet does not matter.

## Files
- `agent_prompt_de.md` - German system prompt for the agent.
- `test_scenarios.md` - calls to try before showing anyone.

## Backend (works now, no accounts needed)
- `reservations.py` core + `server.py` webhook + `retell_tools.json` tool definitions.
- Run tests: `python3 -m unittest test_reservations`. Run server: `TOOL_TOKEN=<secret> python3 server.py` (127.0.0.1:8787; needs a tunnel for the voice platform).
- `send_sms` and `request_callback` only queue rows in an `outbox` table; a real SMS sender is still to build.
- Table sizes and hours in `CFG` are placeholders.
- `prospects.csv` has 21 Karlsruhe restaurants from Maps (public listing data); chains marked low fit.
- `pitch_de.md` is the draft in-person offer.

## Website + online booking (built, local only)
- `site/index.html` is served at `/` by `server.py`; guests book through `/api/slots`, `/api/book`, `/api/cancel`.
- Run: `TOOL_TOKEN=<secret> python3 server.py`, open http://127.0.0.1:8787/ ; owner panel at `/panel` (user: any, password: TOOL_TOKEN).
- Abuse limits: honeypot field, 5 bookings/hour per client address, max 3 open bookings per phone number, 4 KB body cap.
- Before going live: host the two Google Fonts locally (loading them from Google is a data-protection problem in Germany), add Impressum and Datenschutz, replace the drawn bowl or add the owner's own photos, real menu/prices, real tables/hours in `CFG`, and put it behind HTTPS.
- Guests get an on-screen confirmation and a cancel button only; no SMS or email yet.

## Open items (need the owner)
- Real table sizes, max party size, turn times, lunch vs dinner rules, walk-in policy.
- Where bookings land: Google Calendar vs sheet vs their own list.
- Legal: say "KI-Assistent" at call start; tell callers about recording; no cold-calling businesses (UWG §7), visit in person. Ask a lawyer once before selling.
