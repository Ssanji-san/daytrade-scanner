from scanner.config import Config
from scanner.gainers import top_gainers

from .fixtures import make_state

CFG = Config()


def sym(name, change5):
    return make_state(symbol=name, changes={5: change5, 10: None, 15: None})


def test_sorted_by_window_change_desc():
    states = [sym("A", 2.0), sym("B", 8.0), sym("C", 5.0)]
    rows = top_gainers(states, 5, CFG)
    assert [r["symbol"] for r in rows] == ["B", "C", "A"]


def test_drops_missing_and_non_positive_changes():
    states = [sym("A", 4.0), sym("B", None), sym("C", -1.0), sym("D", 0.0)]
    rows = top_gainers(states, 5, CFG)
    assert [r["symbol"] for r in rows] == ["A"]


def test_caps_rows():
    states = [sym(f"S{i}", float(i + 1)) for i in range(50)]
    rows = top_gainers(states, 5, CFG)
    assert len(rows) == CFG.gainer_rows
    assert rows[0]["symbol"] == "S49"


def test_uses_selected_window():
    a = make_state(symbol="A", changes={5: 1.0, 10: 9.0, 15: None})
    b = make_state(symbol="B", changes={5: 2.0, 10: 3.0, 15: None})
    assert [r["symbol"] for r in top_gainers([a, b], 5, CFG)] == ["B", "A"]
    assert [r["symbol"] for r in top_gainers([a, b], 10, CFG)] == ["A", "B"]
