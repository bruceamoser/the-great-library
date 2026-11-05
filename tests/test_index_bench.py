"""Tests for benchmarks/index_bench.py.

The benchmark is what a throughput claim rests on, so the properties worth
pinning are the ones that make its numbers trustworthy: it measures the same
work in every configuration, it is deterministic for a seed, it never writes to
the library it reads, and it says so when pointed at nothing.

No real library is needed: the corpus is a handful of generated PDFs.
"""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / 'benchmarks' / 'index_bench.py'

# Distinct page counts, so a different sample is very unlikely to add up to the
# same total and the determinism test below can see the difference.
PAGES_PER_FILE = [2, 3, 4, 5, 6]


def make_corpus(directory, pages_per_file=PAGES_PER_FILE):
    """Write tiny but real PDFs; the indexer needs a real text layer."""
    import pymupdf
    for i, pages in enumerate(pages_per_file):
        doc = pymupdf.open()
        for page in range(pages):
            surface = doc.new_page()
            surface.insert_text(
                (72, 72), f'book {i} page {page} ' + 'dragon ' * 20)
        doc.save(str(Path(directory) / f'book{i}.pdf'))
        doc.close()


class IndexBenchTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.library = Path(self._tmp.name) / 'library'
        self.library.mkdir()
        make_corpus(self.library)

    def run_bench(self, *args, expect=0):
        result = subprocess.run(
            [sys.executable, str(BENCH), '--library', str(self.library), *args],
            capture_output=True, text=True, timeout=300)
        if expect is not None:
            self.assertEqual(result.returncode, expect, result.stderr)
        return result

    def test_reports_throughput_for_every_configuration(self):
        result = self.run_bench('--workers', '1,2', '--rounds', '1',
                                '--no-project')
        self.assertIn('pages/s', result.stdout)
        for configuration in ('1w streamed', '2w streamed', '1w inplace'):
            self.assertIn(configuration, result.stdout)

    def test_json_matches_the_corpus(self):
        result = self.run_bench('--workers', '1', '--rounds', '1',
                                '--no-project', '--json')
        data = json.loads(result.stdout)
        self.assertEqual(data['sample_files'], len(PAGES_PER_FILE))
        self.assertEqual(data['pages'], sum(PAGES_PER_FILE))
        self.assertEqual(len(data['configurations']), 2)  # streamed + inplace
        for row in data['configurations']:
            self.assertGreater(row['pages_s'], 0)
            self.assertGreater(row['median_s'], 0)

    def test_streamed_and_inplace_extract_the_same_text(self):
        # The benchmark exits non-zero if the configurations disagree, so a
        # clean run is the assertion: open_pdf() streaming and letting MuPDF
        # read the file must produce identical pages and characters.
        result = self.run_bench('--workers', '1', '--rounds', '1',
                                '--patterns', 'streamed,inplace', '--json',
                                '--no-project')
        data = json.loads(result.stdout)
        self.assertEqual(data['chars'] > 0, True)
        self.assertEqual({row['configuration'] for row in data['configurations']},
                         {'1w streamed', '1w inplace'})

    def test_sample_is_deterministic_for_a_seed(self):
        first = self.run_bench('--sample', '2', '--seed', '5', '--workers', '1',
                               '--rounds', '1', '--no-project', '--json')
        second = self.run_bench('--sample', '2', '--seed', '5', '--workers', '1',
                                '--rounds', '1', '--no-project', '--json')
        self.assertEqual(json.loads(first.stdout)['pages'],
                         json.loads(second.stdout)['pages'])
        # A different seed should pick a different pair of these five files.
        other = self.run_bench('--sample', '2', '--seed', '1', '--workers', '1',
                               '--rounds', '1', '--no-project', '--json')
        self.assertEqual(json.loads(other.stdout)['sample_files'], 2)

    def test_writes_nothing_into_the_library(self):
        before = sorted(p.name for p in self.library.iterdir())
        self.run_bench('--workers', '1', '--rounds', '1', '--no-project')
        self.assertEqual(sorted(p.name for p in self.library.iterdir()), before)
        self.assertEqual([p for p in self.library.rglob('*.db')], [])

    def test_projection_covers_the_whole_library(self):
        result = self.run_bench('--workers', '1', '--rounds', '1',
                                '--sample', '2', '--json')
        projection = json.loads(result.stdout)['projection']
        self.assertGreaterEqual(projection['library_mib'],
                                json.loads(result.stdout)['sample_mib'])
        self.assertGreater(projection['extraction_seconds'], 0)

    def test_rejects_a_missing_library(self):
        result = subprocess.run(
            [sys.executable, str(BENCH), '--library',
             str(Path(self._tmp.name) / 'nope')],
            capture_output=True, text=True, timeout=120)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('not a directory', result.stderr)

    def test_rejects_an_unknown_pattern(self):
        result = self.run_bench('--patterns', 'sideways', expect=None)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unknown pattern', result.stderr)


if __name__ == '__main__':
    unittest.main()
