import contextlib
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import messenger_plots as m


def message(sender, text, date, **extra):
    return dict(senderName=sender,text=text,timestamp=m.date_ms(date),type="text",media=[],reactions=[],isUnsent=False,**extra)


def thread(name, participants, messages):
    return dict(threadName=name,participants=participants,messages=messages)


def write(path,obj):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(obj,ensure_ascii=False),encoding="utf-8")
    return path


class WindowTests(unittest.TestCase):
    def test_default_anniversary_and_inclusive_end(self):
        w=m.resolve_window("1y",m.date_ms("2026-09-29"))
        self.assertEqual(w.start,m.date_ms("2025-09-29"))
        self.assertEqual(w.end,m.date_ms("2026-09-30"))

    def test_leap_and_month_clamping(self):
        self.assertEqual(m.resolve_window("1y",m.date_ms("2024-02-29")).start,m.date_ms("2023-02-28"))
        self.assertEqual(m.resolve_window("1m",m.date_ms("2024-03-31")).start,m.date_ms("2024-02-29"))

    def test_day_week_counts(self):
        for spec,days in [("30d",30),("2w",14)]:
            w=m.resolve_window(spec,m.date_ms("2026-09-29"))
            self.assertEqual(w.end-w.start,days*86400000)

    def test_custom_bounds_override_duration(self):
        w=m.resolve_window("1y",m.date_ms("2026-09-29"),m.date_ms("2025-01-01"),m.date_ms("2025-12-31"))
        self.assertEqual(w.start,m.date_ms("2025-01-01"))
        self.assertEqual(w.end,m.date_ms("2026-01-01"))
        with self.assertRaises(m.InputError):m.resolve_window("1y",0,m.date_ms("2026-01-02"),m.date_ms("2026-01-01"))

    def test_invalid_window(self):
        with self.assertRaises(m.InputError):m.resolve_window("potato",m.date_ms("2026-01-01"))
        with self.assertRaises(m.InputError):m.resolve_window("0y",m.date_ms("2026-01-01"))


@unittest.skipUnless(shutil.which("jq"),"jq is required for integration fixtures")
class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.raw=self.root/"raw";self.raw.mkdir()
        alice=[message("Me","one two three","2024-09-28"),message("Alice","four five","2025-09-29"),message("Me","isn't it café’s?","2026-09-29")]
        alice[1]['reactions']=[dict(actor="Me",reaction="❤")]
        self.a=write(self.raw/"Alice's conversation 1.json",thread("Alice_12",["Me","Alice"],alice))
        self.b=write(self.raw/"nested"/"Bob 2.json",thread("Bob_77",["Me","Bob"],[message("Bob","six seven eight nine","2025-10-01")]))
        self.self_file=write(self.raw/"self.json",thread("Me_3",["Me","Me"],[message("Me","note","2026-09-28")]))
        self.empty=write(self.raw/"empty.json",thread("Empty_9",["Me","Empty"],[]))
        self.files=[self.a,self.b,self.self_file,self.empty]
        self.summary=m.summarize(self.files)

    def tearDown(self):self.temp.cleanup()

    def run_main(self,*args):
        with contextlib.redirect_stdout(io.StringIO()),contextlib.redirect_stderr(io.StringIO()):
            return m.main(list(args))

    def test_jq_word_count_and_self_exclusion(self):
        self.assertEqual(self.summary['overall']['total_messages'],5)
        self.assertEqual(self.summary['overall']['total_words'],13)
        self.assertEqual(self.summary['overall']['total_reactions'],1)
        d=m.prepare(self.summary,"All","Me")
        self.assertEqual(len(d.direct),2)
        self.assertEqual(d.aggregate['my_messages'],2)
        self.assertEqual(d.aggregate['my_words'],6)
        self.assertEqual(d.aggregate['other_messages'],2)
        self.assertEqual(d.aggregate['other_words'],6)
        bob=next(x for x in d.direct if x['name']=='Bob')
        self.assertIsNone(bob['my_wpm'])
        self.assertTrue(any('duplicate' in w for w in d.warnings))

    def test_filter_start_and_end_milliseconds(self):
        start=m.date_ms("2025-09-29");end=m.date_ms("2026-09-30")
        records=[]
        for ts in [start-1,start,end-1,end]:
            msg=message("Me","word","2025-01-01");msg['timestamp']=ts;records.append(msg)
        path=write(self.root/"boundary.json",thread("Boundary_1",["Me","Other"],records))
        result=m.summarize([path],start_ms=start,end_ms=end)
        self.assertEqual(result['overall']['total_messages'],2)
        self.assertEqual(result['overall']['earliest_timestamp'],start)
        self.assertEqual(result['overall']['latest_timestamp'],end-1)

    def test_discovery_ignores_previous_summary(self):
        write(self.raw/"chat_stats.json",self.summary)
        write(self.raw/"metadata.json",{"unrelated":True})
        summary,files,skipped=m.discover(self.raw,True)
        self.assertIsNone(summary);self.assertEqual(len(files),4);self.assertEqual(len(skipped),2)
        self.assertEqual(len(m.discover(self.raw,False)[1]),3)

    def test_default_from_single_full_export(self):
        out=self.root/"reports"
        self.assertEqual(self.run_main("--input",str(self.raw),"--me","Me","--out",str(out),"--summarize-only"),0)
        a=m.read_json(out/"chat_stats_a.json");b=m.read_json(out/"chat_stats_b.json");analysis=m.read_json(out/"analysis.json")
        self.assertEqual(a['overall']['total_messages'],5)
        self.assertEqual(b['overall']['total_messages'],4)
        self.assertEqual(b['overall']['earliest_date'],'2025-09-29')
        self.assertEqual(analysis['comparison_mode'],'nested')

    def test_disjoint_and_partially_overlapping_ranges(self):
        out=self.root/"reports"
        common=["--input",str(self.raw),"--me","Me","--out",str(out),"--summarize-only"]
        self.assertEqual(self.run_main(*common,"--start-a","2024-01-01","--end-a","2024-12-31","--start-b","2025-01-01","--end-b","2026-12-31"),0)
        self.assertEqual(m.read_json(out/"analysis.json")['comparison_mode'],'disjoint')
        self.assertEqual(self.run_main(*common,"--start-a","2024-01-01","--end-a","2025-12-31","--start-b","2025-01-01","--end-b","2026-12-31"),0)
        self.assertEqual(m.read_json(out/"analysis.json")['comparison_mode'],'unavailable')

    def test_summaries_need_no_jq_but_cannot_be_refiltered(self):
        path=write(self.root/"stats.json",self.summary);out=self.root/"reports"
        self.assertEqual(self.run_main("--input",str(path),"--window-b","none","--me","Me","--out",str(out),"--jq","missing-jq","--summarize-only"),0)
        self.assertEqual(self.run_main("--input",str(path),"--me","Me","--out",str(out),"--summarize-only"),2)

    def test_totals_and_ambiguous_threads_fail(self):
        bad=deepcopy(self.summary);bad['overall']['total_messages']+=1
        with self.assertRaises(m.InputError):m.prepare(bad,"Bad","Me")
        bad=deepcopy(self.summary);dup=deepcopy(bad['threads'][0]);dup['thread_name']=m.normalize_key(dup)[0]+"_999";bad['threads'].append(dup)
        with self.assertRaisesRegex(m.InputError,"ambiguous"):m.prepare(bad,"Bad","Me")

    def test_raw_export_keeps_schema_and_refuses_input_tree(self):
        target=self.root/"filtered";w=m.resolve_window("1y",m.date_ms("2026-09-29"))
        self.assertEqual(m.export_filtered(self.files,self.raw,target,w,"jq"),4)
        result=m.read_json(target/self.a.name)
        self.assertEqual(result['participants'],['Me','Alice']);self.assertEqual(len(result['messages']),2)
        self.assertTrue((target/"nested"/"Bob 2.json").exists())
        with self.assertRaises(m.InputError):m.export_filtered(self.files,self.raw,self.raw/"year",w,"jq")
        with self.assertRaises(m.InputError):m.export_filtered(self.files,self.raw,target,w,"jq")

    def test_input_cannot_be_overwritten(self):
        path=write(self.root/"chat_stats_a.json",self.summary);before=path.read_bytes()
        self.assertEqual(self.run_main("--input",str(path),"--window-b","none","--me","Me","--out",str(self.root),"--summarize-only"),2)
        self.assertEqual(path.read_bytes(),before)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"),"plotting dependencies unavailable")
    def test_sparse_and_empty_render_eight_charts(self):
        for name,inputs in [("sparse",self.raw),("empty",self.empty)]:
            out=self.root/name
            self.assertEqual(self.run_main("--input",str(inputs),"--me","Me","--out",str(out),"--dpi","50"),0)
            manifest=m.read_json(out/"chart_manifest.json")
            self.assertEqual(len(manifest),8)
            self.assertEqual(len(list(out.glob('*.png'))),8)
            self.assertTrue((out/'Messenger_chat_report.pdf').read_bytes().startswith(b'%PDF'))


if __name__=="__main__":unittest.main()
