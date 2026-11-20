import json
import sqlite3
import unittest
from contextlib import closing
from types import SimpleNamespace

from briefing import Decision, Perspective, init_preferences, sweep


class DefaultPerspectiveTest(unittest.TestCase):
    def test_default_pacing_exposure_and_replay(self):
        db = self.enterContext(closing(sqlite3.connect(":memory:")))
        council = self.enterContext(closing(sqlite3.connect(":memory:")))
        init_preferences(db)
        with db:
            db.execute("INSERT INTO preferences VALUES ('reader', 'Energy')")
        council.execute("""CREATE TABLE observations
            (article_id, url, persona_id, observation, supporting_quote, kind, relevant)""")
        choices = iter([0, 1, 1])
        exposures = []

        def parse(*, text_format, input, **kwargs):
            if text_format is Decision:
                result = Decision(recommend=True, summary="Prices rose.")
            else:
                payload = json.loads(input)
                self.assertEqual(payload["reader_note"], "")
                exposures.append(payload["previous_reviewer_counts"])
                choice = next(choices)
                result = Perspective(review_number=choice, spoken_angle="Sami says rent rose." if choice else "")
            return SimpleNamespace(status="completed", output_parsed=result)

        client = SimpleNamespace(responses=SimpleNamespace(parse=parse))
        for number in range(1, 10):
            council.execute("INSERT INTO observations VALUES (?, '', 'sami', 'Rent rose.', 'higher rent', 'persona', 1)",
                            (str(number),))
            items = list(sweep(council, db, "reader", "mock", lambda: client, voice=number == 9))
            self.assertEqual(len(items), 1)
            self.assertEqual("Supporting quote: higher rent" in items[0][2], number == 5)
            if number == 9:
                self.assertEqual(items[0][2], "Prices rose. Sami says rent rose.")
                self.assertEqual(db.execute("SELECT spoken_text FROM decisions WHERE article_id='9'").fetchone()[0],
                                 items[0][2])
        self.assertEqual(exposures, [{}, {}, {"sami": 1}])
        self.assertEqual(list(sweep(council, db, "reader", "mock", lambda: client)), [])
        self.assertEqual(db.execute("SELECT COUNT(*), SUM(checked), COUNT(reviewer) FROM perspective_results")
                         .fetchone(), (9, 3, 2))
