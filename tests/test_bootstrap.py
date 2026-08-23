from docassemble_simulator.bootstrap import deep_merge


class TestDeepMerge:
    def test_nested_merge_preserves_unrelated_keys(self):
        base = {"a": {"x": 1, "y": 2}, "keep": True}
        deep_merge(base, {"a": {"y": 3}})
        assert base == {"a": {"x": 1, "y": 3}, "keep": True}

    def test_scalar_override_replaces_dict(self):
        base = {"a": {"x": 1}}
        deep_merge(base, {"a": "flat"})
        assert base == {"a": "flat"}

    def test_dict_override_replaces_scalar(self):
        base = {"a": "flat"}
        deep_merge(base, {"a": {"x": 1}})
        assert base == {"a": {"x": 1}}

    def test_new_key_added(self):
        base = {}
        deep_merge(base, {"redis": "redis://localhost:6399"})
        assert base == {"redis": "redis://localhost:6399"}

    def test_deeply_nested(self):
        base = {"a": {"b": {"c": 1, "d": 2}}}
        deep_merge(base, {"a": {"b": {"d": 9}}})
        assert base == {"a": {"b": {"c": 1, "d": 9}}}
