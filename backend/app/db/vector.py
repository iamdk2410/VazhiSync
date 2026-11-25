"""A minimal SQLAlchemy type for pgvector's `vector(n)` column (B22, decision 6).

pgvector reads and writes vectors as text ('[0.1,0.2,...]'). asyncpg has no codec for the
extension type, so values travel as text: parameters are sent as TEXT and cast to vector in
SQL, results are cast back to TEXT. Distances use pgvector's `<->` (L2) via `l2_distance()`.
No extra package needed.
"""

from typing import Any

from sqlalchemy import ColumnElement, Float, Text, cast, literal, type_coerce
from sqlalchemy.types import TypeDecorator, UserDefinedType


def cast_expr(expr: Any) -> ColumnElement[float]:
    """`Comparator.op()` is typed as returning Operators; it builds a ColumnElement."""
    return expr  # type: ignore[no-any-return]


def to_text(value: list[float] | str | None) -> str | None:
    if value is None or isinstance(value, str):
        return value
    return "[" + ",".join(repr(float(v)) for v in value) + "]"


class _VectorText(TypeDecorator[str]):
    """The parameter as asyncpg sees it: plain text ('[0.1,0.2]'), cast to vector in SQL."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Any) -> str | None:
        return to_text(value)


class Vector(UserDefinedType[list[float]]):
    cache_ok = True

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def get_col_spec(self, **_: Any) -> str:
        return f"vector({self.dim})"

    def bind_expression(self, bindvalue: Any) -> Any:
        return cast(type_coerce(bindvalue, _VectorText()), self)  # $1::text::vector

    def column_expression(self, col: Any) -> Any:
        return type_coerce(cast(col, Text), self)  # vector::text, parsed below

    def bind_processor(self, dialect: Any) -> Any:
        dim = self.dim

        def process(value: list[float] | None) -> str | None:
            if value is None:
                return None
            if len(value) != dim:
                raise ValueError(f"expected {dim} values, got {len(value)}")
            return to_text(value)

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            if value is None:
                return None
            return [float(v) for v in value.strip("[]").split(",") if v]

        return process

    class comparator_factory(UserDefinedType.Comparator[list[float]]):
        def l2_distance(self, other: list[float]) -> ColumnElement[float]:
            distance = self.op("<->", return_type=Float())(literal(other, type_=self.type))
            return cast_expr(distance)
