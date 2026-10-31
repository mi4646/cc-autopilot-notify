from cc_autopilot_notify import judge


def _q(options, question="Which?"):
    return {"question": question, "options": options}


def test_english_recommended_suffix():
    q = _q([{"label": "Alpha"}, {"label": "Beta (Recommended)"}])
    assert judge.marker_index(q) == 1


def test_chinese_recommended_suffix():
    q = _q([{"label": "甲"}, {"label": "乙（推荐）"}])
    assert judge.marker_index(q) == 1


def test_description_marker():
    q = _q([{"label": "Alpha", "description": "recommended for speed"},
            {"label": "Beta", "description": "slower"}])
    assert judge.marker_index(q) == 0


def test_no_marker_returns_none():
    assert judge.marker_index(_q([{"label": "A"}, {"label": "B"}])) is None


def test_negated_description_is_not_a_marker():
    q = _q([{"label": "A", "description": "This is not recommended for production"},
            {"label": "B", "description": "slower"}])
    assert judge.marker_index(q) is None


def test_negated_chinese_description_is_not_a_marker():
    q = _q([{"label": "甲", "description": "不推荐用于生产"},
            {"label": "乙", "description": "更慢"}])
    assert judge.marker_index(q) is None


def test_ambiguous_markers_return_none():
    q = _q([{"label": "A (Recommended)"}, {"label": "B (Recommended)"}])
    assert judge.marker_index(q) is None


def test_marker_wins_without_calling_model():
    calls = []
    q = _q([{"label": "A"}, {"label": "B (Recommended)"}])
    d = judge.pick(q, model="m", call_model=lambda *a: calls.append(a) or 0)
    assert (d.index, d.fallback) == (1, False)
    assert calls == []


def test_model_used_when_no_marker():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model="m", call_model=lambda qq, mm: 1)
    assert (d.index, d.fallback) == (1, False)


def test_model_failure_falls_back_to_first():
    q = _q([{"label": "A"}, {"label": "B"}])
    def boom(*a):
        raise RuntimeError("gateway down")
    d = judge.pick(q, model="m", call_model=boom)
    assert (d.index, d.fallback) == (0, True)


def test_no_model_falls_back_to_first():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model=None)
    assert (d.index, d.fallback) == (0, True)


def test_fallback_delegate_returns_free_text():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model=None, fallback="delegate")
    assert d.delegate is True
    assert d.labels == (judge.DELEGATE_TEXT,)


def test_model_failure_is_retried_once():
    calls = []
    q = _q([{"label": "A"}, {"label": "B"}])

    def boom(*a):
        calls.append(1)
        raise RuntimeError("gateway down")

    judge.pick(q, model="m", call_model=boom)
    assert len(calls) == 2, "one retry is allowed before falling back"


def test_model_index_out_of_range_falls_back():
    q = _q([{"label": "A"}, {"label": "B"}])
    d = judge.pick(q, model="m", call_model=lambda qq, mm: 7)
    assert (d.index, d.fallback) == (0, True)


def test_empty_options_raises():
    try:
        judge.pick(_q([]), model=None)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_pick_many_selects_every_marked_option():
    q = _q([{"label": "A (Recommended)"}, {"label": "B"},
            {"label": "C (Recommended)"}])
    d = judge.pick_multi(q, model=None)
    assert d.indices == (0, 2)
    assert d.fallback is False


def test_pick_many_single_marker():
    q = _q([{"label": "A"}, {"label": "B (Recommended)"}])
    d = judge.pick_multi(q, model=None)
    assert (d.indices, d.fallback) == ((1,), False)


def test_pick_many_without_marker_delegates_as_free_text():
    q = _q([{"label": "A"}, {"label": "B"}, {"label": "C"}])
    d = judge.pick_multi(q, model=None)
    assert d.indices == ()
    assert d.labels == (judge.DELEGATE_TEXT,)
    assert d.fallback is True
    assert d.delegate is True


def test_pick_many_never_calls_the_model():
    calls = []
    q = _q([{"label": "A"}, {"label": "B"}])
    judge.pick_multi(q, model="m", call_model=lambda *a: calls.append(a) or 1)
    assert calls == [], "multi-select has no agreed way to ask for a set; do not guess"


def test_pick_exposes_indices_for_single_select():
    q = _q([{"label": "A"}, {"label": "B (Recommended)"}])
    d = judge.pick(q, model=None)
    assert (d.indices, d.index, d.label) == ((1,), 1, "B (Recommended)")
