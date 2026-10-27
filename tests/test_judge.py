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
