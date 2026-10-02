from minedocscan.evaluate.metrics import auto_rate, cer, corpus_cer, field_accuracy, normalize


def test_normalize_collapses_whitespace():
    assert normalize("  a   b \n c ") == "a b c"
    assert normalize(None) == ""


def test_cer_basic():
    assert cer("abc", "abc") == 0.0
    assert cer("abd", "abc") == 1 / 3
    assert cer("", "abc") == 1.0
    assert cer("abcd", "abc") == 1 / 3


def test_cer_empty_truth():
    assert cer("", "") == 0.0
    assert cer("x", "") == 1.0          # 빈 칸에 값을 만들어 낸 경우


def test_corpus_cer_weights_by_length():
    pairs = [("aaaaaaaaaa", "aaaaaaaaaa"), ("b", "c")]
    assert corpus_cer(pairs) == 1 / 11
    assert corpus_cer([]) == 0.0


def test_field_accuracy_and_auto_rate():
    assert field_accuracy([("a", "a"), ("a ", "a"), ("b", "a")]) == 2 / 3
    assert auto_rate(["auto", "pending", "auto", "reviewed"]) == 0.5
    assert auto_rate([]) == 0.0
