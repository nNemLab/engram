from engram.research import rerank


def test_score_passes_full_passage_to_model(monkeypatch):
    captured = {}

    class FakeModel:
        def predict(self, pairs, show_progress_bar=False):
            captured["pairs"] = pairs
            return [0.1]

    monkeypatch.setattr(rerank, "_get_model", lambda: FakeModel())

    long_passage = "x" * 3000
    scores = rerank.score("query", [long_passage])

    assert scores == [0.1]
    assert captured["pairs"] == [("query", long_passage)]
