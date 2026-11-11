import unittest
from datetime import date, timedelta

import reservations as r

D = (date.today() + timedelta(days=3)).isoformat()


class T(unittest.TestCase):
    def setUp(self):
        self.db = r.connect()

    n = 0

    def book(self, t="19:00", party=2, phone=None, name="Anna"):
        T.n += 1  # fresh number per call, the per-phone limit would otherwise stop the fill-up tests
        return r.create_reservation(self.db, name, phone or f"0721 {T.n:06d}", D, t, party)

    def test_book_and_find(self):
        b = self.book(phone="0721 123456")
        self.assertEqual(r.find_reservation(self.db, "anna", "0721 123456")["reservations"][0]["id"], b["id"])

    def test_odd_party_fits(self):
        self.assertTrue(r.check_availability(self.db, D, "19:00", 3)["available"])

    def test_full_then_rejects(self):
        for _ in range(len(r.CFG["tables"])):
            self.book(party=2)  # small parties spill onto bigger tables until all are taken
        with self.assertRaises(r.Err):
            self.book(party=2)

    def test_overlap_vs_after_stay(self):
        for _ in range(len(r.CFG["tables"])):
            self.book("19:00")
        self.assertTrue(r.check_availability(self.db, D, "20:30", 2)["available"])  # 75 min stay is over

    def test_big_table_not_double_booked(self):
        self.book(party=6)
        with self.assertRaises(r.Err):
            self.book(party=6)

    def test_rules(self):
        for bad in [("23:00", 2), ("19:00", 12), ("19:00", 0)]:
            with self.assertRaises(r.Err):
                r.check_availability(self.db, D, *bad)
        with self.assertRaises(r.Err):
            r.check_availability(self.db, "2020-01-01", "19:00", 2)
        with self.assertRaises(r.Err):
            r.create_reservation(self.db, "", "0721 123456", D, "19:00", 2)
        with self.assertRaises(r.Err):
            r.create_reservation(self.db, "Anna", "abc", D, "19:00", 2)

    def test_cancel_needs_matching_phone(self):
        b = self.book(phone="0721 123456")
        with self.assertRaises(r.Err):
            r.cancel_reservation(self.db, b["id"], "0000 000000")
        r.cancel_reservation(self.db, b["id"], "0721 123456")
        self.assertEqual(r.find_reservation(self.db, "Anna", "0721 123456")["reservations"], [])


class W(unittest.TestCase):
    def setUp(self):
        self.db = r.connect()

    def test_slots_and_party_limit(self):
        s = r.day_slots(self.db, D, 2)
        self.assertIn("19:00", s)
        self.assertEqual(s[0], r.CFG["open"])
        with self.assertRaises(r.Err):
            r.day_slots(self.db, D, 12)
        with self.assertRaises(r.Err):
            r.day_slots(self.db, "morgen", 2)

    def test_full_slot_disappears(self):
        for _ in range(len(r.CFG["tables"])):
            T.n += 1
            r.create_reservation(self.db, "A", f"0721 {T.n:06d}", D, "19:00", 2)
        self.assertNotIn("19:00", r.day_slots(self.db, D, 2))

    def test_cancel_with_token(self):
        b = r.create_reservation(self.db, "Anna", "0721 123456", D, "19:00", 2)
        with self.assertRaises(r.Err):
            r.cancel_by_token(self.db, b["id"], "wrong")
        r.cancel_by_token(self.db, b["id"], b["token"])
        with self.assertRaises(r.Err):
            r.cancel_by_token(self.db, b["id"], b["token"])

    def test_phone_limit(self):
        for t in ("12:00", "13:00", "14:00"):
            r.create_reservation(self.db, "Anna", "0721 123456", D, t, 2)
        with self.assertRaises(r.Err):
            r.create_reservation(self.db, "Anna", "0721123456", D, "15:00", 2)


class Q(unittest.TestCase):
    def test_outbox(self):
        db = r.connect()
        self.assertTrue(r.send_sms(db, "0721 123456", "Reservierung bestätigt")["queued"])
        self.assertTrue(r.request_callback(db, "Anna", "0721 123456", "Gruppe 12")["queued"])
        self.assertEqual(db.execute("SELECT count(*) FROM outbox").fetchone()[0], 2)
        with self.assertRaises(r.Err):
            r.send_sms(db, "x", "hi")
        with self.assertRaises(r.Err):
            r.request_callback(db, "", "0721 123456", "x")


if __name__ == "__main__":
    unittest.main()
