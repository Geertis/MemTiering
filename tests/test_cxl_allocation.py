import random
import unittest
from cxl_controller import settle_grants, waterfill


class AllocationTest(unittest.TestCase):
    def test_integer_conservation_and_demand_caps(self):
        rng = random.Random(37)
        for _ in range(2000):
            demands = {str(k): rng.randrange(200000) for k in range(rng.randrange(1, 9))}
            weights = {k: rng.uniform(.1, 8) for k in demands}
            budget = rng.randrange(800000)
            result = waterfill(demands, weights, budget)
            self.assertEqual(sum(result.values()), min(budget, sum(demands.values())))
            for k, demand in demands.items():
                self.assertLessEqual(result[k], demand)

    def test_unmigrated_donor_creates_no_credit(self):
        desired = {'wait': 10, 'active': 90}
        previous = {'wait': 90, 'active': 10}
        actual = {'wait': 90, 'active': 10}
        self.assertEqual(settle_grants(desired, previous, actual, 100, ['active', 'wait']),
                         {'wait': 10, 'active': 10})
        actual['wait'] = 10
        self.assertEqual(settle_grants(desired, previous, actual, 100, ['active', 'wait']), desired)

    def test_existing_overshoot_cannot_fund_promotions(self):
        result = settle_grants({'a': 50, 'b': 50}, {'a': 50, 'b': 0},
                               {'a': 110, 'b': 0}, 100, ['b', 'a'])
        self.assertEqual(result['b'], 0)

    def test_simultaneous_resumption_remains_bounded(self):
        desired = waterfill(dict.fromkeys('abcd', 200), dict.fromkeys('abcd', 1), 100)
        result = settle_grants(desired, dict.fromkeys('abcd', 0),
                               dict.fromkeys('abcd', 0), 100, list('abcd'))
        self.assertEqual(result, dict.fromkeys('abcd', 25))

    def test_overshoot_revokes_other_unbacked_reservations(self):
        result = settle_grants({'a': 80, 'b': 20}, {'a': 60, 'b': 20},
                               {'a': 110, 'b': 0}, 100, ['a', 'b'])
        self.assertEqual(result['b'], 0)
        self.assertLessEqual(sum(result.values()), 100)

    def test_existing_residency_does_not_overcommit_quota_grants(self):
        result = settle_grants({'a': 100, 'b': 100}, {'a': 0, 'b': 100},
                               {'a': 90, 'b': 110}, 100, ['a', 'b'])
        self.assertEqual(result, {'a': 0, 'b': 100})

    def test_random_settlement_with_existing_reservations_and_overshoot(self):
        rng = random.Random(81)
        for _ in range(10000):
            keys = [str(k) for k in range(rng.randrange(1, 9))]
            budget = rng.randrange(1, 10000)
            previous = waterfill({k: rng.randrange(20000) for k in keys},
                                 dict.fromkeys(keys, 1), budget)
            desired = waterfill({k: rng.randrange(20000) for k in keys},
                                dict.fromkeys(keys, 1), budget)
            resident = {k: rng.randrange(20000) for k in keys}
            rng.shuffle(keys)
            result = settle_grants(desired, previous, resident, budget, keys)
            self.assertLessEqual(sum(result.values()), budget)
            committed = sum(max(result[k], resident[k]) for k in keys)
            self.assertLessEqual(committed, max(budget, sum(resident.values())))
            for k in keys:
                self.assertLessEqual(result[k], desired[k])


if __name__ == '__main__':
    unittest.main()
