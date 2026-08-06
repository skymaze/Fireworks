"""B4：node_metrics limit<=0 不再除零（回归：limit=0 曾抛 ZeroDivisionError -> 500）。"""

from app.routers.nodes import node_metrics


class _Row:
    def __init__(self, ts: float):
        self.ts = ts
        self.data = {"cpu": 1.0}


class _Query:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return self.rows


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def get(self, model, node_id):  # get_node_or_404 用
        return object()

    def query(self, model):
        return _Query(self.rows)


def _db_with(n_rows: int):
    return _FakeDB([_Row(float(i)) for i in range(n_rows)])


def test_limit_zero_returns_empty():
    assert node_metrics(1, None, None, 0, _db_with(100)) == []


def test_negative_limit_returns_empty():
    assert node_metrics(1, None, None, -5, _db_with(100)) == []


def test_positive_limit_downsamples():
    out = node_metrics(1, None, None, 10, _db_with(100))
    assert len(out) == 10
    assert [r["ts"] for r in out] == sorted(r["ts"] for r in out)


def test_no_downsample_when_rows_within_limit():
    out = node_metrics(1, None, None, 2000, _db_with(100))
    assert len(out) == 100
