"""Fast accounting and schedule fixtures; live checks are run.py and operate.py."""
import unittest
from load import schedule


class Schedule(unittest.TestCase):
    def rows(self, profile):
        return sorted((r for j in range(4) for r in schedule(profile,120,4.4,j,4,0,.1,4)),
                      key=lambda r:r["index"])

    def test_unique_open_loop(self):
        rows=self.rows("uniform")
        self.assertEqual([r["index"] for r in rows],list(range(528)))
        self.assertEqual([r["offset"] for r in rows],[i/4.4 for i in range(528)])

    def test_uniform_vs_skew_counts(self):
        from collections import Counter
        self.assertEqual(Counter(r["client"] for r in self.rows("uniform")),{0:132,1:132,2:132,3:132})
        counts=Counter(r["client"] for r in self.rows("skew"))
        self.assertAlmostEqual(counts[0]/528,.8,delta=.003)

    def test_common_payload_and_clock(self):
        a,b=[self.rows(p) for p in ("uniform","skew")]
        for x,y in zip(a,b):
            self.assertEqual((x["offset"],x["body"]["prompt"],x["body"]["history"],x["body"]["chunks"]),
                             (y["offset"],y["body"]["prompt"],y["body"]["history"],y["body"]["chunks"]))


if __name__=="__main__":
    unittest.main(verbosity=2)
