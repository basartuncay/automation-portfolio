"""Demo data relative to today, so the skonto window and the overdue dates work on any day: the accounting stand-in's
invoices, and bank statements (CAMT.053 and a German CSV) paying some of them in all the usual messy ways.
Write fresh statements with:  python -m paymatch.demo_data samples/"""
import sys
from datetime import date, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

T = date.today()
d = lambda n: (T + timedelta(days=n)).isoformat()

INVOICES = [  # id, number, customer, customer no, email, issued, due, total cents
    ("inv_101", "RE-2026-0101", "Nordlicht Shop GmbH", "KD-1001", "buchhaltung@nordlicht-shop.de", d(-20), d(-6), 119000),
    ("inv_102", "RE-2026-0102", "Kaffeekontor Hamburg", "KD-1002", "office@kaffeekontor.de", d(-5), d(9), 238000),
    ("inv_103", "RE-2026-0103", "Bergwerk Outdoor GmbH", "KD-1003", "rechnung@bergwerk-outdoor.at", d(-12), d(2), 84000),
    ("inv_104", "RE-2026-0104", "Tulpen Design B.V.", "KD-1004", "finance@tulpen-design.nl", d(-15), d(-1), 45000),
    ("inv_105", "RE-2026-0105", "Tulpen Design B.V.", "KD-1004", "finance@tulpen-design.nl", d(-9), d(5), 29900),
    ("inv_106", "RE-2026-0106", "Greenleaf Cosmetics GmbH", "KD-1006", "ap@greenleaf-cosmetics.de", d(-25), d(-11), 100000),
    ("inv_107", "RE-2026-0107", "Pfotenglück UG (haftungsbeschränkt)", "KD-1007", "mia@pfoten-glueck.de", d(-22), d(-8), 56000),
    ("inv_108", "RE-2026-0108", "Velo Atelier", "KD-1008", "luca@velo-atelier.ch", d(-40), d(-26), 150000),
    ("inv_109", "RE-2026-0109", "Lumen Lighting", "KD-1009", "eva@lumen-lighting.nl", d(-24), d(-10), 32000),
    ("inv_110", "RE-2026-0110", "Brauhaus Direkt", "KD-1010", "felix@brauhaus-direkt.de", d(-3), d(11), 56000),
    ("inv_111", "RE-2026-0111", "Urban Sneaker GmbH", "KD-1011", "ap@urban-sneaker.de", d(-9), d(5), 200000),
]

STATEMENT = [  # booked, cents, payer, iban, purpose, bank reference
    (d(-2), 119000, "NORDLICHT SHOP GMBH", "DE89370400440532013000", "RE-2026-0101 KD-1001", "2026BANKREF0001"),
    (d(-1), 233240, "Kaffeekontor Hamburg GmbH", "DE12500105170648489890", "Rechnung 2026 0102 abzgl. 2% Skonto", "2026BANKREF0002"),
    (d(-1), 83250, "Bergwerk Outdoor", "AT611904300234573201", "RE20260103", "2026BANKREF0003"),
    (d(-1), 74900, "Tulpen Design", "NL91ABNA0417164300", "RE-2026-0104 en RE-2026-0105", "2026BANKREF0004"),
    (d(-1), 40000, "Greenleaf Cosmetics", "DE75512108001245126199", "Teilzahlung RE-2026-0106", "2026BANKREF0005"),
    (d(0), 56000, "Mia Wagner", "DE02120300000000202051", "Danke!", "2026BANKREF0006"),
    (d(0), 200000, "Urban Sneaker GmbH", "DE44500105175407324931", "Invoice RE-2026-0111", "2026BANKREF0007"),
    (d(0), -250000, "Hausverwaltung Nord", "DE02100100100006820101", "Miete Dezember", "2026BANKREF0008"),
    (d(0), 8900, "AMAZON PAYMENTS EUROPE", "LU89751000135104200E", "Auszahlung 4471-2201", "2026BANKREF0009"),
]


def camt() -> str:
    entries = []
    for booked, cents, name, iban, purpose, ref in STATEMENT:
        credit = cents > 0
        party, acct = ("Dbtr", "DbtrAcct") if credit else ("Cdtr", "CdtrAcct")
        entries.append(f"""      <Ntry>
        <Amt Ccy="EUR">{abs(cents) / 100:.2f}</Amt><CdtDbtInd>{'CRDT' if credit else 'DBIT'}</CdtDbtInd><Sts>BOOK</Sts>
        <BookgDt><Dt>{booked}</Dt></BookgDt><ValDt><Dt>{booked}</Dt></ValDt><AcctSvcrRef>{ref}</AcctSvcrRef>
        <NtryDtls><TxDtls>
          <Refs><EndToEndId>NOTPROVIDED</EndToEndId></Refs>
          <RltdPties><{party}><Nm>{escape(name)}</Nm></{party}><{acct}><Id><IBAN>{iban}</IBAN></Id></{acct}></RltdPties>
          <RmtInf><Ustrd>{escape(purpose)}</Ustrd></RmtInf>
        </TxDtls></NtryDtls>
      </Ntry>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Document xmlns="urn:iso:std:iso:20022:tech:xsd:camt.053.001.02">
  <BkToCstmrStmt>
    <GrpHdr><MsgId>STMT-{T:%Y%m%d}</MsgId><CreDtTm>{T}T06:00:00</CreDtTm></GrpHdr>
    <Stmt>
      <Id>STMT-{T:%Y%m%d}-1</Id><Acct><Id><IBAN>DE27100777770209299700</IBAN></Id></Acct>
{chr(10).join(entries)}
    </Stmt>
  </BkToCstmrStmt>
</Document>
"""


def german_csv() -> str:
    """A second export from online banking, overlapping the CAMT file by two entries (imported once only) plus one new one."""
    rows = ["Kontoumsätze Geschäftskonto;;;;", "", "Buchungstag;Valuta;Auftraggeber / Empfänger;IBAN;Verwendungszweck;Betrag (EUR)"]
    fmt = lambda c: f"{c / 100:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    for booked, cents, name, iban, purpose, _ in STATEMENT[:2]:
        rows.append(f"{date.fromisoformat(booked):%d.%m.%Y};{date.fromisoformat(booked):%d.%m.%Y};{name};{iban};{purpose};{fmt(cents)}")
    rows.append(f"{T:%d.%m.%Y};{T:%d.%m.%Y};Lumen Lighting B.V.;NL20INGB0001234567;RE-2026-0109;{fmt(32000)}")
    return "\n".join(rows) + "\n"


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "samples")
    out.mkdir(exist_ok=True)
    (out / "statement.camt053.xml").write_text(camt())
    (out / "umsaetze.csv").write_text(german_csv(), encoding="cp1252")
    print(f"wrote {out}/statement.camt053.xml and {out}/umsaetze.csv")
