"""Use the site-configured, paired matrix without legacy result dependencies."""
import json
from pathlib import Path
from outer import ROOT, save


def plans():
    matrix = json.loads((Path(__file__).parent / 'configured-matrix.json').read_text())
    save(ROOT / 'matrix.json', matrix)
    return matrix
