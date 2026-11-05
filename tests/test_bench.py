import json, subprocess, sys, tempfile, unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
BENCH=ROOT/'benchmarks'/'search_bench.py'

class SearchBenchTests(unittest.TestCase):
    """The benchmark is the measurement everything else is judged against, so it
    has to keep running offline against a schema it never drifts from."""
    def run_bench(self,*args):
        return subprocess.run([sys.executable,str(BENCH),*map(str,args)],capture_output=True,text=True,timeout=300)
    def test_prints_p50_and_p95_per_section(self):
        r=self.run_bench('--files',200,'--pages',800,'--runs',2,'--warmup',0)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('p50 ms',r.stdout);self.assertIn('p95 ms',r.stdout)
        for section in ('collections','files','content','campaigns'):
            self.assertIn(section,r.stdout)
    def test_json_output_is_parseable(self):
        r=self.run_bench('--files',120,'--pages',400,'--runs',2,'--warmup',0,'--json')
        self.assertEqual(r.returncode,0,r.stderr)
        data=json.loads(r.stdout)
        self.assertEqual(set(data['sections']),{'collections','files','content','campaigns'})
        self.assertEqual(data['catalogue']['pages'],400)
        for stats in data['sections'].values():
            self.assertEqual(len(stats['samples']),2);self.assertGreater(stats['p50'],0)
    def test_refuses_to_overwrite_an_existing_database(self):
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'library.db';path.write_text('do not touch')
            r=self.run_bench('--db',path,'--files',10,'--pages',10,'--runs',1)
            self.assertNotEqual(r.returncode,0)
            self.assertIn('refusing',r.stderr)
            self.assertEqual(path.read_text(),'do not touch')
