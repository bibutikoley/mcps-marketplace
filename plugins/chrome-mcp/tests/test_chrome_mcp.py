import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chrome
import main


def _pages():
    return [
        {
            "id": "aaa",
            "title": "Example",
            "url": "https://example.com",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/dev/aaa",
        },
        {
            "id": "bbb",
            "title": "Docs",
            "url": "https://docs.example.com/x",
            "type": "page",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/dev/bbb",
        },
    ]


class TestConfirmGates(unittest.TestCase):
    def test_close_tabs_requires_confirm(self):
        with patch.object(chrome, "targets", return_value=_pages()):
            with patch.object(chrome, "close_target") as mock_close:
                res = main.chrome_close_tabs(tabIds=[1])
                self.assertTrue(res.is_error)
                mock_close.assert_not_called()
                res = main.chrome_close_tabs(tabIds=[1], confirm=True)
                self.assertFalse(res.is_error)
                mock_close.assert_called_once()

    def test_javascript_requires_confirm(self):
        with patch.object(chrome, "resolve_target", return_value=_pages()[0]):
            with patch.object(chrome, "evaluate", return_value=1) as mock_ev:
                res = main.chrome_javascript(code="1+1")
                self.assertTrue(res.is_error)
                mock_ev.assert_not_called()
                res = main.chrome_javascript(code="1+1", confirm=True)
                self.assertFalse(res.is_error)

    def test_bookmark_delete_requires_confirm(self):
        with patch.object(chrome, "bookmark_delete", return_value={"id": "1"}):
            res = main.chrome_bookmark_delete(bookmarkId="1")
            self.assertTrue(res.is_error)
            res = main.chrome_bookmark_delete(bookmarkId="1", confirm=True)
            self.assertFalse(res.is_error)

    def test_network_post_requires_confirm(self):
        with patch.object(chrome, "resolve_target", return_value=_pages()[0]):
            with patch.object(
                chrome, "network_request_via_page", return_value={"status": 200, "text": "ok"}
            ):
                res = main.chrome_network_request(
                    url="https://example.com", method="POST", body="{}"
                )
                self.assertTrue(res.is_error)
                res = main.chrome_network_request(
                    url="https://example.com", method="POST", body="{}", confirm=True
                )
                self.assertFalse(res.is_error)

    def test_network_get_no_confirm(self):
        with patch.object(chrome, "resolve_target", return_value=_pages()[0]):
            with patch.object(
                chrome, "network_request_via_page", return_value={"status": 200, "text": "ok"}
            ):
                res = main.chrome_network_request(url="https://example.com")
                self.assertFalse(res.is_error)

    def test_flow_run_requires_confirm(self):
        res = main.record_replay_flow_run(flowId="f1")
        self.assertTrue(res.is_error)
        self.assertIn("confirm=true", res.content[0].text)

    def test_inject_requires_confirm(self):
        res = main.chrome_inject_script(js="1")
        self.assertTrue(res.is_error)


class TestValidation(unittest.TestCase):
    def test_require_http_url(self):
        self.assertEqual(chrome.require_http_url("https://example.com/x"), "https://example.com/x")
        for bad in ["javascript:alert(1)", "file:///etc/passwd", "notaurl", ""]:
            with self.assertRaises(chrome.ChromeError):
                chrome.require_http_url(bad)

    def test_require_coords(self):
        chrome.require_coords(0, 0)
        with self.assertRaises(chrome.ChromeError):
            chrome.require_coords(-1, 10)
        with self.assertRaises(chrome.ChromeError):
            chrome.require_coords(10, 10001)

    def test_keyboard_map(self):
        self.assertEqual(chrome.keyboard_map("a")["key"], "a")
        self.assertEqual(chrome.keyboard_map("Enter")["windowsVirtualKeyCode"], 13)
        with self.assertRaises(chrome.ChromeError):
            chrome.keyboard_map("FancyKey42")

    def test_resolve_target_range(self):
        with patch.object(chrome, "targets", return_value=_pages()):
            with self.assertRaises(chrome.ChromeError):
                chrome.resolve_target(tab_id=99)
            t = chrome.resolve_target(tab_id=2)
            self.assertEqual(t["id"], "bbb")

    def test_find_ref_bad(self):
        with self.assertRaises(chrome.ChromeError):
            chrome.find_ref(_pages()[0], ref="nope", selector=None)

    def test_iso_rejects_garbage(self):
        with self.assertRaises(chrome.ChromeError):
            chrome.history_search(None, "not-a-date", None)


class TestSearch(unittest.TestCase):
    def test_tfidf_ranks_relevant_first(self):
        docs = [
            {"id": "1", "title": "Cats", "url": "u1", "text": "cats cats cats sitting on mats"},
            {"id": "2", "title": "Dogs", "url": "u2", "text": "dogs bark loudly at night"},
        ]
        ranked = chrome.tfidf_rank("cats", docs)
        self.assertEqual(ranked[0]["id"], "1")
        self.assertGreater(ranked[0]["score"], ranked[1]["score"])

    def test_tfidf_rejects_empty_query(self):
        with self.assertRaises(chrome.ChromeError):
            chrome.tfidf_rank("!!!", [{"id": "1", "title": "", "url": "", "text": ""}])


class TestProfileBackends(unittest.TestCase):
    def _profile_with(self, tmp: Path) -> Path:
        prof = tmp / "Chrome"
        (prof / "Default").mkdir(parents=True)
        (prof / "Default/Bookmarks").write_text(
            json.dumps(
                {
                    "roots": {
                        "bookmark_bar": {
                            "id": "1",
                            "name": "Bar",
                            "type": "folder",
                            "children": [
                                {
                                    "id": "2",
                                    "name": "Example",
                                    "type": "url",
                                    "url": "https://example.com",
                                }
                            ],
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return prof

    def test_bookmark_search_add_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            prof = self._profile_with(Path(tmp))
            with patch.object(chrome, "profile_dir", return_value=prof):
                marks = chrome.bookmark_search("example", None)
                self.assertEqual(len(marks), 1)
                node = chrome.bookmark_add("https://new.example.com", "New", None, False)
                self.assertEqual(node["url"], "https://new.example.com")
                self.assertEqual(len(chrome.bookmark_search("new.example", None)), 1)
                removed = chrome.bookmark_delete(None, "https://new.example.com", None)
                self.assertEqual(removed["url"], "https://new.example.com")
                with self.assertRaises(chrome.ChromeError):
                    chrome.bookmark_delete("nope", None, None)

    def test_history_missing_db(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(chrome, "profile_dir", return_value=Path(tmp)):
                with self.assertRaises(chrome.ChromeError):
                    chrome.history_search(None, None, None)


class TestStores(unittest.TestCase):
    def test_userscript_crud(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(chrome, "data_dir", return_value=Path(tmp)):
                created = chrome.userscript_dispatch("create", {"name": "t", "script": "1"})
                sid = created["id"]
                self.assertEqual(chrome.userscript_dispatch("list", {})["count"], 1)
                self.assertEqual(chrome.userscript_dispatch("get", {"id": sid})["name"], "t")
                chrome.userscript_dispatch("disable", {"id": sid})
                self.assertFalse(chrome.userscript_dispatch("get", {"id": sid})["enabled"])
                chrome.userscript_dispatch("remove", {"id": sid})
                self.assertEqual(chrome.userscript_dispatch("list", {})["count"], 0)

    def test_network_capture_roundtrip_without_browser(self):
        with patch.object(chrome, "targets", return_value=[]):
            started = chrome.network_capture_start(None, 1000)
            self.assertIn("capture_id", started)
            stopped = chrome.network_capture_stop(include_static=False)
            self.assertEqual(stopped["count"], 0)

    def test_trace_analyze_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "t.json"
            trace.write_text(
                json.dumps(
                    [
                        {
                            "method": "Tracing.dataCollected",
                            "params": {
                                "traceEvents": [
                                    {"name": "Paint", "dur": 5000},
                                    {"name": "Script", "dur": 9000},
                                ]
                            },
                        },
                    ]
                ),
                encoding="utf-8",
            )
            info = chrome.trace_analyze(str(trace))
            self.assertEqual(info["events"], 2)
            self.assertEqual(info["top_ms"][0]["name"], "Script")

    def test_gif_unknown_recording(self):
        with self.assertRaises(chrome.ChromeError):
            chrome.gif_status("gif-nope")


if __name__ == "__main__":
    unittest.main()
