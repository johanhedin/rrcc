"""Tests for the rpm version comparison (rpmvercmp, evr_cmp)."""

import random
import unittest

from support import have_module, load_rrcc

rrcc = load_rrcc()

# The test cases of rpm itself (tests/rpmvercmp.at in the rpm sources),
# including ~ (sorts before anything) and ^ (sorts after the end, before
# anything else).
RPM_CASES = [
    ('1.0',               '1.0',                0),
    ('1.0',               '2.0',               -1),
    ('2.0',               '1.0',                1),
    ('2.0.1',             '2.0.1',              0),
    ('2.0',               '2.0.1',             -1),
    ('2.0.1',             '2.0',                1),
    ('2.0.1a',            '2.0.1a',             0),
    ('2.0.1a',            '2.0.1',              1),
    ('2.0.1',             '2.0.1a',            -1),
    ('5.5p1',             '5.5p1',              0),
    ('5.5p1',             '5.5p2',             -1),
    ('5.5p2',             '5.5p1',              1),
    ('5.5p10',            '5.5p10',             0),
    ('5.5p1',             '5.5p10',            -1),
    ('5.5p10',            '5.5p1',              1),
    ('10xyz',             '10.1xyz',           -1),
    ('10.1xyz',           '10xyz',              1),
    ('xyz10',             'xyz10',              0),
    ('xyz10',             'xyz10.1',           -1),
    ('xyz10.1',           'xyz10',              1),
    ('xyz.4',             'xyz.4',              0),
    ('xyz.4',             '8',                 -1),
    ('8',                 'xyz.4',              1),
    ('xyz.4',             '2',                 -1),
    ('2',                 'xyz.4',              1),
    ('5.5p2',             '5.6p1',             -1),
    ('5.6p1',             '5.5p2',              1),
    ('5.6p1',             '6.5p1',             -1),
    ('6.5p1',             '5.6p1',              1),
    ('6.0.rc1',           '6.0',                1),
    ('6.0',               '6.0.rc1',           -1),
    ('10b2',              '10a1',               1),
    ('10a2',              '10b2',              -1),
    ('1.0aa',             '1.0aa',              0),
    ('1.0a',              '1.0aa',             -1),
    ('1.0aa',             '1.0a',               1),
    ('10.0001',           '10.0001',            0),
    ('10.0001',           '10.1',               0),
    ('10.1',              '10.0001',            0),
    ('10.0001',           '10.0039',           -1),
    ('10.0039',           '10.0001',            1),
    ('4.999.9',           '5.0',               -1),
    ('5.0',               '4.999.9',            1),
    ('20101121',          '20101121',           0),
    ('20101121',          '20101122',          -1),
    ('20101122',          '20101121',           1),
    ('2_0',               '2_0',                0),
    ('2.0',               '2_0',                0),
    ('2_0',               '2.0',                0),
    ('a',                 'a',                  0),
    ('a+',                'a+',                 0),
    ('a+',                'a_',                 0),
    ('a_',                'a+',                 0),
    ('+a',                '+a',                 0),
    ('+a',                '_a',                 0),
    ('_a',                '+a',                 0),
    ('+_',                '+_',                 0),
    ('_+',                '+_',                 0),
    ('_+',                '_+',                 0),
    ('+',                 '_',                  0),
    ('_',                 '+',                  0),
    ('1.0~rc1',           '1.0~rc1',            0),
    ('1.0~rc1',           '1.0',               -1),
    ('1.0',               '1.0~rc1',            1),
    ('1.0~rc1',           '1.0~rc2',           -1),
    ('1.0~rc2',           '1.0~rc1',            1),
    ('1.0~rc1~git123',    '1.0~rc1~git123',     0),
    ('1.0~rc1~git123',    '1.0~rc1',           -1),
    ('1.0~rc1',           '1.0~rc1~git123',     1),
    ('1.0^',              '1.0^',               0),
    ('1.0^',              '1.0',                1),
    ('1.0',               '1.0^',              -1),
    ('1.0^git1',          '1.0^git1',           0),
    ('1.0^git1',          '1.0',                1),
    ('1.0',               '1.0^git1',          -1),
    ('1.0^git1',          '1.0^git2',          -1),
    ('1.0^git2',          '1.0^git1',           1),
    ('1.0^git1',          '1.01',              -1),
    ('1.01',              '1.0^git1',           1),
    ('1.0^20160101',      '1.0^20160101',       0),
    ('1.0^20160101',      '1.0.1',             -1),
    ('1.0.1',             '1.0^20160101',       1),
    ('1.0^20160101^git1', '1.0^20160101^git1',  0),
    ('1.0^20160102',      '1.0^20160101^git1',  1),
    ('1.0^20160101^git1', '1.0^20160102',      -1),
    ('1.0~rc1^git1',      '1.0~rc1^git1',       0),
    ('1.0~rc1^git1',      '1.0~rc1',            1),
    ('1.0~rc1',           '1.0~rc1^git1',      -1),
    ('1.0^git1~pre',      '1.0^git1~pre',       0),
    ('1.0^git1',          '1.0^git1~pre',       1),
    ('1.0^git1~pre',      '1.0^git1',          -1),
    ('1b.fc17',           '1b.fc17',            0),
    ('1b.fc17',           '1.fc17',            -1),
    ('1.fc17',            '1b.fc17',            1),
    ('1g.fc17',           '1g.fc17',            0),
    ('1g.fc17',           '1.fc17',             1),
    ('1.fc17',            '1g.fc17',           -1),
]


class RpmVerCmpTest(unittest.TestCase):
    def test_rpm_upstream_cases(self):
        for a, b, expected in RPM_CASES:
            with self.subTest(a=a, b=b):
                self.assertEqual(rrcc.rpmvercmp(a, b), expected)

    @unittest.skipUnless(have_module("rpm"), "python3-rpm is not installed")
    def test_random_against_rpm(self):
        import rpm
        tokens = ["1", "01", "2", "10", "a", "b", "A", "alpha", "~", "^", "~rc1", "^git",
                  "1.0", "1_0", "1.0.1", "el8", "el9", ".", "+", "", "0", "00", "9",
                  "z1", "1z", "rc", "1.el8_5.2", "5.module_el8+123+abc", "20220101git"]
        rnd = random.Random(1)
        for _ in range(20000):
            a = "".join(rnd.choice(tokens) for _ in range(rnd.randint(0, 4)))
            b = "".join(rnd.choice(tokens) for _ in range(rnd.randint(0, 4)))
            if not (a and b):
                continue  # rpm 4.16+ refuses an empty version ("invalid version")
            expected = rpm.labelCompare(("0", a, "0"), ("0", b, "0"))
            self.assertEqual(rrcc.rpmvercmp(a, b), expected, f"{a!r} vs {b!r}")


def pkg(version, release="1", epoch="0"):
    return {"epoch": epoch, "version": version, "release": release}


class EvrCmpTest(unittest.TestCase):
    def test_epoch_wins(self):
        self.assertEqual(rrcc.evr_cmp(pkg("1.0", epoch="1"), pkg("9.0", epoch="0")), 1)
        self.assertEqual(rrcc.evr_cmp(pkg("9.0", epoch="0"), pkg("1.0", epoch="1")), -1)

    def test_numeric_epoch(self):
        self.assertEqual(rrcc.evr_cmp(pkg("1", epoch="10"), pkg("1", epoch="9")), 1)

    def test_missing_epoch_is_zero(self):
        self.assertEqual(rrcc.evr_cmp(pkg("1.0", epoch=None), pkg("1.0", epoch="0")), 0)

    def test_version_before_release(self):
        self.assertEqual(rrcc.evr_cmp(pkg("1.1", "1"), pkg("1.0", "9")), 1)

    def test_release(self):
        self.assertEqual(rrcc.evr_cmp(pkg("1.0", "10.el8"), pkg("1.0", "9.el8")), 1)
        self.assertEqual(rrcc.evr_cmp(pkg("1.0", "1.el8"), pkg("1.0", "1.el8")), 0)


if __name__ == "__main__":
    unittest.main()
