import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from corrections import load_dictionary_file, load_settings, save_settings


class DictionaryFiles(unittest.TestCase):
    def test_round_trip_preserves_japanese_deletions_and_auto_apply(self):
        settings = {"version": 1, "auto_apply": False,
                    "rules": [{"before": "文字お越し", "after": "文字起こし"},
                              {"before": "えーと", "after": ""}]}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "辞書.json"
            save_settings(settings, path)
            self.assertEqual(load_dictionary_file(path), settings)
            path.write_text(path.read_text("utf-8"), encoding="utf-8-sig")
            self.assertEqual(load_dictionary_file(path), settings)
            self.assertEqual(load_settings(path), settings)

    def test_rejects_unrelated_invalid_and_missing_files(self):
        invalid = [{}, {"segments": []}, [], {"rules": "words"}, {"rules": [], "version": 2},
                   {"rules": [], "auto_apply": "false"}, {"rules": [{"before": "", "after": "語"}]},
                   {"rules": [{"before": "語", "after": "単語"}, {"before": "語", "after": "別名"}]},
                   {"rules": [{"before": "語", "after": "複数\n行"}]}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "input.json"
            for data in invalid:
                with self.subTest(data=data):
                    path.write_text(json.dumps(data), encoding="utf-8")
                    with self.assertRaises(ValueError):
                        load_dictionary_file(path)
            path.write_bytes(b"\xff")
            with self.assertRaises(ValueError):
                load_dictionary_file(path)
            with self.assertRaises(FileNotFoundError):
                load_dictionary_file(Path(temp) / "missing.json")
            path.write_text('{"rules":[]}', encoding="utf-8")
            self.assertEqual(load_dictionary_file(path), {"version": 1, "auto_apply": True, "rules": []})

    def test_interrupted_export_keeps_existing_file(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "export.json"
            original = save_settings({"rules": [{"before": "昔", "after": "以前"}]}, path)
            with patch("corrections.os.replace", side_effect=OSError("disk unavailable")):
                with self.assertRaises(OSError):
                    save_settings({"rules": []}, path)
            self.assertEqual(load_dictionary_file(path), original)
            self.assertEqual(list(Path(temp).iterdir()), [path])


if __name__ == "__main__":
    unittest.main()
