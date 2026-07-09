import tempfile
import unittest
from types import SimpleNamespace

from atlas.storage import AtlasStore
from atlas.search import HybridSearchEngine
from atlas.diffing import DiffEngine
from atlas.common import feature_hash_vector
from atlas.indexer import AtlasIndexer
from atlas.evals import _hit_matches_query
from atlas.media import MediaResearch


class AtlasCoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        self.tmp.close()
        self.store = AtlasStore(self.tmp.name)

    def doc(self, id, title, text, hashv):
        return {
            "doc_id": id,
            "source_id": "test",
            "canon": "Test",
            "title": title,
            "url": "https://example/" + id,
            "language": "en",
            "text": text,
            "summary": text[:120],
            "content_hash": hashv,
            "vector": feature_hash_vector(title + " " + text),
            "metadata": {},
            "archived": False,
        }

    def test_hybrid_search(self):
        self.store.upsert_document(
            self.doc(
                "1",
                "School Bus",
                "A driverless bus stops four times and only one exit is safe",
                "a",
            )
        )
        self.store.upsert_document(
            self.doc(
                "2",
                "Office Hall",
                "An endless office corridor with fluorescent lights",
                "b",
            )
        )
        result = HybridSearchEngine(self.store).search("bus with multiple stops safe exit")
        self.assertEqual(result["results"][0]["doc_id"], "1")

    def test_snapshot_diff(self):
        self.store.upsert_document(self.doc("1", "Test", "old text", "a"))
        self.store.upsert_document(self.doc("1", "Test", "new text", "b"))
        result = DiffEngine(self.store).diff_latest("1")
        self.assertTrue(result["ok"])
        self.assertIn("new text", result["unified_diff"])

    def test_index_page_payload_persists_document_and_edges(self):
        indexer = AtlasIndexer(self.store, None, None, None)
        payload = {
            "source_id": "test-source",
            "canon": "Test Canon",
            "title": "Bus Stops",
            "url": "https://example.test/bus-stops",
            "language": "en",
            "text": "A bus network with numbered stops.",
            "analysis": {
                "summary": "A bus network with numbered stops.",
                "named_signals": {
                    "level_designations": [],
                    "entity_designations": [],
                    "groups": [],
                },
            },
            "links": [
                {"title": "Level bus", "url": "https://example.test/level-bus"}
            ],
            "image_urls": [],
            "provenance": {},
            "archived": False,
        }
        doc = indexer.index_page_payload(payload)
        self.assertIsNotNone(self.store.get_document(doc["doc_id"]))
        edges = self.store.edges_from(doc["doc_id"])
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["to_title"], "Level bus")

    def test_atomic_document_and_edges_roll_back_together(self):
        doc = self.doc("atomic", "Atomic", "text", "hash")
        broken_edges = [{"source_id": "test"}]  # missing to_key
        with self.assertRaises(KeyError):
            self.store.upsert_document_with_edges(doc, broken_edges)
        self.assertIsNone(self.store.get_document("atomic"))

    def test_liminal_eval_match_accepts_correct_url_when_title_is_site_name(self):
        hit = SimpleNamespace(
            title="Liminal Archives",
            url="http://liminal-archives.wikidot.com/baby-food",
        )
        self.assertTrue(_hit_matches_query(hit, "Baby Food"))

    def test_media_filter_rejects_pdf_and_scores_relevant_image(self):
        self.assertFalse(MediaResearch._usable_image({"mime": "application/pdf", "url": "x"}))
        self.assertTrue(MediaResearch._usable_image({"mime": "image/jpeg", "url": "x"}))
        relevant = MediaResearch._relevance_score(
            "school bus interior",
            "File:School bus interior seats.jpg",
            "Interior aisle and seats of a yellow school bus",
            "Buses",
        )
        irrelevant = MediaResearch._relevance_score(
            "school bus interior",
            "File:History book.pdf",
            "Digitized educational book",
            "Books",
        )
        self.assertGreater(relevant, irrelevant)
        self.assertGreaterEqual(relevant, 0.55)


if __name__ == "__main__":
    unittest.main()
