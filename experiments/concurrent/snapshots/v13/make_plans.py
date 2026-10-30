"""Freeze the approved 87-cohort matrix and paired arrival schedules."""
import json
import math
from pathlib import Path
import random
from outer import ROOT


def main():
    cases = json.loads((ROOT / 'inputs/cases.json').read_text())
    history = {'dask': 264.96, 'numba': 839.08, 'pydicom': 393.28, 'streamlink': 723.71}
    plans = ROOT / 'plans'
    plans.mkdir(exist_ok=False)
    matrix = []
    for n in [4, 8, 12, 16]:
        for repetition, seed in enumerate([20261029, 20261030, 20261031], 1):
            rng = random.Random(seed + n * 1000)
            selected = [cases[i % 4] for i in range(n)]
            rng.shuffle(selected)
            slots = []
            for lane, case in enumerate(selected):
                old = next(value for name, value in history.items() if '__' + name in case['name'])
                slots.append(dict(lane=lane, case=case['name'],
                    offset_s=round((lane + rng.random()) * 10 / n, 6),
                    deadline_s=None,
                    deadline_authority='disabled by user protocol correction; observed completion time is measured'))
            reference = f'C0-n{n}-r{repetition}'
            entries = [dict(label=reference, category='C0', strategy='REF', medium='dram',
                            dram_gib=96, cxl_gib=0, reference=None)]
            treatments = [(medium, strategy) for medium in ['ssd', 'cxl'] for strategy in ['D', 'F', 'P']]
            # Rotate execution order without changing any paired input or release offset.
            shift = (repetition - 1) % len(treatments)
            treatments = treatments[shift:] + treatments[:shift]
            entries += [dict(label=f'C1-{medium}-{strategy}-n{n}-r{repetition}',
                category='C1', strategy=strategy, medium=medium, dram_gib=8,
                cxl_gib=64 if medium == 'cxl' and strategy != 'D' else 0, reference=reference)
                for medium, strategy in treatments]
            if n == 8:
                entries.append(dict(label=f'C2-cxl-L-n{n}-r{repetition}', category='C2',
                    strategy='L', medium='cxl', dram_gib=8, cxl_gib=64, reference=reference))
            for entry in entries:
                entry.update(n=n, repetition=repetition, seed=seed, slots=slots)
                (plans / (entry['label'] + '.json')).write_text(json.dumps(entry, indent=2) + '\n')
                matrix.append(entry)
    assert len(matrix) == 87 and sum(x['n'] for x in matrix) == 864
    (ROOT / 'matrix.json').write_text(json.dumps(matrix, indent=2) + '\n')
    print('PLAN_PASS:', len(matrix), 'cohorts;', sum(x['n'] for x in matrix), 'slots')


if __name__ == '__main__':
    main()
