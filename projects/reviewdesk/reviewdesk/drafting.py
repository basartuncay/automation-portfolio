"""Reading a review and drafting the reply.

The language, the topics and anything serious are found with plain rules, so the result is predictable and free to
run. The draft is assembled from phrases written for each topic, in the reviewer's language (German or English), and
addressed by first name. It never argues, never promises money, and never repeats health or legal claims back.
A draft for 1-3 stars is always a suggestion for a person; it's the policy, not the wording, that keeps this safe.
"""
import re

DE_WORDS = {"und", "der", "die", "das", "sehr", "nicht", "war", "essen", "lecker", "freundlich", "leider", "wir", "ich", "mit", "super", "toll", "gut"}
TOPICS = {
    "food": r"food|taste|tasty|delicious|ramen|noodle|soup|broth|bland|salty|essen|lecker|geschmack|nudel|suppe|brühe|versalzen|fad",
    "service": r"service|staff|waiter|friendly|rude|bedienung|personal|kellner|freundlich|unfreundlich|unhöflich",
    "waiting": r"wait|waited|slow|took ages|long time|warten|gewartet|langsam|ewig|dauerte",
    "price": r"price|expensive|overpriced|value|cheap|preis|teuer|überteuert|günstig",
    "cleanliness": r"dirty|clean|toilet|sticky|schmutzig|sauber|dreckig|toilette",
    "booking": r"reservation|booking|booked|table|reservierung|reserviert|tisch",
}
URGENT = {"health": r"food poisoning|sick|ill after|vomit|allerg|lebensmittelvergiftung|krank|übel|erbrochen",
          "legal": r"lawyer|sue|court|police|anwalt|anzeige|gericht|polizei",
          "safety": r"glass in|hair in|insect|cockroach|mouse|maus|kakerlake|haar im|glas im"}

PHRASES = {
    "en": {"hi": "Hi {name},", "thanks_great": "thank you so much for the lovely review!", "thanks": "thank you for taking the time to write this.",
           "sorry": "I'm sorry your visit wasn't what it should have been.",
           "food+": "We're glad the food hit the spot.", "service+": "I'll pass your kind words on to the team.",
           "food-": "I've shared your comments on the food with our kitchen, so they can look at it straight away.",
           "service-": "That isn't how we want anyone to be treated, and I'll talk to the team about it.",
           "waiting-": "Waiting that long isn't acceptable, and we're looking at how orders flow on busy evenings.",
           "price-": "I understand the price felt too high for what you got; that's useful feedback for us.",
           "cleanliness-": "Cleanliness matters to us, and we've checked the area you mentioned.",
           "booking-": "Losing track of a booking is exactly what we don't want; we've changed how reservations are handled.",
           "contact": "If you're willing, please write to me at {email} so I can make it right.", "back": "We hope to see you again soon!",
           "serious": "This is serious, and I'd like to understand exactly what happened. Please contact me directly at {email}.", "sign": "Mara, {location}"},
    "de": {"hi": "Hallo {name},", "thanks_great": "vielen herzlichen Dank für die tolle Bewertung!", "thanks": "danke, dass Sie sich die Zeit genommen haben.",
           "sorry": "Es tut mir leid, dass Ihr Besuch nicht so war, wie er sein sollte.",
           "food+": "Schön, dass es Ihnen geschmeckt hat.", "service+": "Ihr Lob gebe ich gern an das Team weiter.",
           "food-": "Ihre Anmerkungen zum Essen habe ich direkt an unsere Küche weitergegeben.",
           "service-": "So möchten wir niemanden behandeln, und ich spreche das im Team an.",
           "waiting-": "So lange Wartezeiten sind nicht in Ordnung; wir schauen uns die Abläufe an vollen Abenden genau an.",
           "price-": "Ich verstehe, dass Ihnen der Preis für das Gebotene zu hoch war; das nehmen wir ernst.",
           "cleanliness-": "Sauberkeit ist uns wichtig, und wir haben den genannten Bereich geprüft.",
           "booking-": "Eine verlorene Reservierung darf nicht passieren; wir haben unsere Reservierungen umgestellt.",
           "contact": "Wenn Sie mögen, schreiben Sie mir gern an {email}, damit ich das wiedergutmachen kann.", "back": "Wir freuen uns auf Ihren nächsten Besuch!",
           "serious": "Das nehmen wir sehr ernst, und ich möchte genau verstehen, was passiert ist. Bitte melden Sie sich direkt bei mir: {email}.", "sign": "Mara, {location}"},
}


def language(text: str) -> str:
    words = set(re.findall(r"[a-zäöüß]+", text.lower()))
    return "de" if len(words & DE_WORDS) >= 2 or re.search(r"[äöüß]", text.lower()) else "en"


def analyse(text: str) -> tuple[list[str], str | None]:
    topics = [t for t, rx in TOPICS.items() if re.search(rx, text, re.I)]
    urgent = next((k for k, rx in URGENT.items() if re.search(rx, text, re.I)), None)
    return topics, urgent


def first_name(author: str) -> str:
    part = (author or "").strip().split(" ")[0]
    return part if part and not re.search(r"\d|@|google|user", part, re.I) and len(part) > 1 else ""


def draft(*, author: str, stars: int, text: str, lang: str, topics: list[str], urgent: str | None, location: str, email: str) -> str:
    p = PHRASES[lang]
    name = first_name(author)
    hi = p["hi"].format(name=name) if name else ("Hallo," if lang == "de" else "Hi,")
    sign = p["sign"].format(location=location.split(",")[0])
    if urgent:  # never discuss the claim in public: take it to a private conversation
        return f"{hi}\n\n{p['thanks']} {p['serious'].format(email=email)}\n\n{sign}"
    if stars >= 4:
        lines = [p["thanks_great"]] + [p[f"{t}+"] for t in topics if f"{t}+" in p][:2] + [p["back"]]
        return f"{hi}\n\n" + " ".join(lines) + f"\n\n{sign}"
    lines = [p["thanks"], p["sorry"]] + [p[f"{t}-"] for t in topics if f"{t}-" in p][:3] + [p["contact"].format(email=email)]
    return f"{hi}\n\n" + " ".join(lines) + f"\n\n{sign}"
