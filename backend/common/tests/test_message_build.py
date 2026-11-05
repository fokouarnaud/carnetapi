"""Translation build output must be consumable by the standard gettext reader."""
import gettext
from io import BytesIO
from django.test import SimpleTestCase


class MessageBuildTests(SimpleTestCase):
    def test_utf8_plural_and_context_survive_standard_gettext_loading(self):
        from tools.compile_messages import build_mo
        messages = {"": "Language: en\nContent-Type: text/plain; charset=UTF-8\nPlural-Forms: nplurals=2; plural=(n != 1);\n",
                    "élément": "item", "une pièce\x00plusieurs pièces": "one piece\x00many pieces",
                    "verb\x04fermer": "close"}
        translated = gettext.GNUTranslations(BytesIO(build_mo(messages)))
        self.assertEqual(translated.gettext("élément"), "item")
        self.assertEqual(translated.ngettext("une pièce", "plusieurs pièces", 1), "one piece")
        self.assertEqual(translated.ngettext("une pièce", "plusieurs pièces", 2), "many pieces")
        self.assertEqual(translated.pgettext("verb", "fermer"), "close")
        self.assertEqual(translated.gettext("inconnu"), "inconnu")

    def test_freshness_check_detects_stale_or_missing_files_without_rewriting(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from tools.compile_messages import compile_catalogues
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "en" / "LC_MESSAGES" / "django.json"
            source.parent.mkdir(parents=True)
            messages = {"": "Language: en\nContent-Type: text/plain; charset=UTF-8\n", "clé": "value"}
            source.write_text(json.dumps(messages), encoding="utf-8")
            target = source.with_suffix(".mo")
            self.assertEqual(compile_catalogues(root, check=True), [target])
            self.assertFalse(target.exists())
            self.assertEqual(compile_catalogues(root), [])
            original = target.read_bytes()
            self.assertEqual(compile_catalogues(root, check=True), [])
            messages["clé"] = "new value"
            source.write_text(json.dumps(messages), encoding="utf-8")
            self.assertEqual(compile_catalogues(root, check=True), [target])
            self.assertEqual(target.read_bytes(), original)
