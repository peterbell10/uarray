"""
Support reading and compiling to python AST of NumPy API

Array -> AST of numpy.ndarray
Vector -> tuple
Naturalural -> number
"""
import ast
import dataclasses
import typing

import numpy

from .lazy_ndarray import *  # type: ignore
from ..core import *
from ..dispatch import *
from ..moa import *
from .mutable_arrays import *
from .lazy_ndarray import numpy_transpose

__all__ = ["AST", "to_ast"]
T_box = typing.TypeVar("T_box", bound=Box)


@register(numpy_transpose)
def numpy_transpose_ast(array: LazyNDArray[T_box]) -> LazyNDArray[T_box]:
    if isinstance(array.value, AST):
        return LazyNDArray(
            AST(
                ast.Call(
                    ast.Attribute(
                        value=array.value.get, attr="transpose", ctx=ast.Load()
                    ),
                    args=[],
                    keywords=[],
                )
            ).include(array.value),
            dtype=array.dtype,
        )

    return NotImplemented


@dataclasses.dataclass(frozen=True)
class AST:
    # AST expression that returns this value
    get: ast.AST
    # AST statements that initialize this value
    init: typing.Iterable[ast.AST] = ()

    def include(self, other: "AST") -> "AST":
        return AST(self.get, (*other.init, *self.init))

    def includes(self, *others: "AST") -> "AST":
        for other in others:
            self = self.include(other)
        return self


def is_ast(b: Box[object]) -> bool:
    return isinstance(b.value, AST)


@register(to_array)  # type: ignore
def to_array(b: Box) -> Array:
    if not is_ast(b):
        return NotImplemented
    return Array(b.value, Box())


@operation
def to_ast(b: typing.Any) -> typing.Any:
    return b


def _ast_abstraction_inner(
    fn: typing.Callable[..., AST], rettype: T_box, *args: Box[AST]
) -> T_box:
    asts: typing.List[AST] = [a.value for a in args]
    return rettype.replace(fn(*asts))


def create_ast_abstraction(
    fn: typing.Callable[..., AST], rettype: Box, n_args: int
) -> T_box:
    return Abstraction.create_nary_native(  # type: ignore
        Partial(_ast_abstraction_inner, (fn, rettype)), rettype, *[is_ast] * n_args
    )


def as_ast(fn: typing.Callable[..., AST], rettype: T_box, *args: Box) -> T_box:
    """
    Takes in a function mapping arguments of type AST to an AST 
    """
    res: typing.Any = create_ast_abstraction(fn, rettype, len(args))
    for a in args:
        res = res(to_ast(a))
    return typing.cast(T_box, res)


@register(to_ast)
def to_ast_numbers(b: T_box) -> T_box:
    if not isinstance(b.value, (int, float)):
        return NotImplemented
    return b.replace(AST(ast.Num(b.value)))


_nat_bin_ops = {
    Natural.__add__: ast.Add(),
    Natural.__mul__: ast.Mult(),
    Natural.__sub__: ast.Sub(),
    Natural.__floordiv__: ast.FloorDiv(),
    Natural.__mod__: ast.Mod(),
}


@register(to_ast)
def to_ast_nat_bin_ops(b: T_box) -> T_box:
    if not isinstance(b.value, Operation) or b.value.name not in _nat_bin_ops:
        return NotImplemented

    def inner(l_ast: AST, r_ast: AST) -> AST:
        return AST(
            ast.BinOp(l_ast.get, _nat_bin_ops[b.value.name], r_ast.get)
        ).includes(l_ast, r_ast)

    return as_ast(inner, b, *b.value.args)


@register(to_ast)
def to_ast_tuple(b: T_box) -> T_box:
    if (
        not isinstance(b, Vec)
        or not isinstance(b.value, Operation)
        or b.value.name != Vec.create
    ):
        return NotImplemented

    length, lst = b.value.args
    if not isinstance(length.value, int):
        return NotImplemented

    def inner(*args: AST) -> AST:
        return AST(ast.Tuple([a.get for a in args], ast.Load())).includes(*args)

    return as_ast(  # type: ignore
        inner, b, *(lst[Natural(i)] for i in range(length.value))
    )


@register(to_ast)
def to_ast__ufunc(b: T_box) -> T_box:
    if not isinstance(b.value, Operation) or b.value.name != numpy_ufunc:
        return NotImplemented
    ufunc, *args = b.value.args
    if not isinstance(ufunc.value, numpy.ufunc):
        return NotImplemented

    def _call_ufunc(*args: AST, name: str = ufunc.value.__name__) -> AST:
        return AST(
            ast.Call(
                ast.Attribute(
                    value=ast.Name(
                        id="numpy",
                        ctx=ast.Load(),  # TODO: Maybe don't get off global numpy?
                    ),
                    attr=name,
                    ctx=ast.Load(),
                ),
                args=[a.get for a in args],
                keywords=[],
            )
        ).includes(*args)

    return as_ast(_call_ufunc, b, *args)


_i = 0


def _gen_id() -> str:
    global _i
    i = f"v{_i}"
    _i += 1
    return i


def create_id() -> typing.Tuple[ast.AST, ast.AST]:
    """
    return store/load expressions for new name
    """
    i = _gen_id()
    return ast.Name(i, ast.Store()), ast.Name(i, ast.Load())


def create_for(
    store_index: ast.AST,
    store_result: ast.AST,
    load_result: ast.AST,
    n_ast: AST,
    initial_ast: AST,
    load_loop_result: AST,
) -> AST:
    return AST(
        load_result,
        [
            ast.Assign([store_result], initial_ast.get),
            ast.For(
                store_index,
                ast.Call(ast.Name("range", ast.Load()), [n_ast.get], []),
                [
                    *load_loop_result.init,
                    ast.Assign([store_result], load_loop_result.get),
                ],
                [],
            ),
        ],
    ).includes(initial_ast, n_ast)


@register(to_ast)
def to_ast_loop(b: T_box) -> T_box:
    if not isinstance(b.value, Operation) or b.value.name != Natural.loop:
        return NotImplemented
    n, initial, fn = typing.cast(
        typing.Tuple[Natural, T_box, Abstraction[T_box, Abstraction["Natural", T_box]]],
        b.value.args,
    )

    store_index, load_index = create_id()
    store_result, load_result = create_id()

    return as_ast(
        Partial(create_for, (store_index, store_result, load_result)),
        b,
        n,
        initial,
        fn(initial.replace(AST(load_result)))(Natural(AST(load_index))),
    )


@register(Array._get_shape)
def _get_shape(self: Array[T_box]) -> Vec[Natural]:
    if not is_ast(self):
        return NotImplemented
    ndim = Natural(
        AST(ast.Attribute(self.value.get, "ndim", ast.Load())).include(self.value)
    )

    @List.from_function
    def list_fn(idx: Natural, self_ast=self.value) -> Natural:
        def inner(idx_ast: AST, self_ast=self_ast) -> AST:
            return AST(
                ast.Subscript(
                    ast.Attribute(self_ast.get, "shape", ast.Load()),
                    ast.Index(idx_ast.get),
                    ast.Load(),
                )
            ).includes(self_ast, idx_ast)

        return as_ast(inner, Natural(), idx)

    return Vec.create(ndim, list_fn)


@register(Array._get_idx_abs)
def _get_idx_abs(self: Array[T_box]) -> Abstraction[Vec[Natural], T_box]:
    if not is_ast(self):
        return NotImplemented

    @Array.create_idx_abs
    def idx_abs(idx: Vec[Natural], self_ast=self.value) -> T_box:
        def inner(idx_ast: AST, self_ast=self_ast) -> AST:
            if not idx_ast.get.elts:  # type: ignore
                return self_ast
            return AST(
                ast.Subscript(self_ast.get, ast.Index(idx_ast.get), ast.Load())
            ).includes(self_ast, idx_ast)

        return as_ast(inner, self.dtype, idx)

    return idx_abs


@register(to_ast)
def to_ast__create_empty(b: List[T_box]) -> List[T_box]:
    dtype, length = typing.cast(
        typing.Tuple[T_box, Natural], extract_args(create_empty, b)  # type: ignore
    )

    def inner(length_ast: AST) -> AST:
        return AST(
            ast.Call(
                ast.Attribute(ast.Name("numpy", ast.Load()), "empty", ast.Load()),
                [length_ast.get],
                [
                    ast.keyword(
                        arg="dtype",
                        value=ast.Attribute(
                            ast.Name("numpy", ast.Load()), "int64", ast.Load()
                        ),
                    )
                ],
            )
        ).includes(length_ast)

    return as_ast(inner, b, length)


@register(to_ast)
def to_ast__from_list_nd(b: Array[T_box]) -> Array[T_box]:
    data, shape = typing.cast(
        typing.Tuple[List[T_box], Vec[Natural]],
        extract_args(Array.from_list_nd, b),  # type: ignore
    )

    def inner(data_ast: AST, shape_ast: AST) -> AST:
        return AST(
            ast.Call(
                ast.Attribute(data_ast.get, "reshape", ast.Load()), [shape_ast.get], []
            )
        ).includes(data_ast, shape_ast)

    return as_ast(inner, b, data, shape)


@register(to_ast)
def to_ast__set_item(b: List[T_box]) -> List[T_box]:
    lst, index, item = typing.cast(
        typing.Tuple[List[T_box], Natural, T_box],
        extract_args(set_item, b),  # type: ignore
    )

    def inner(lst_ast: AST, index_ast: AST, item_ast) -> AST:
        return AST(
            lst_ast.get,
            [
                ast.Assign(
                    [ast.Subscript(lst_ast.get, ast.Index(index_ast.get), ast.Store())],
                    item_ast.get,
                )
            ],
        ).includes(lst_ast, index_ast, item_ast)

    return as_ast(inner, b, lst, index, item)


@register(to_ast)
def to_ast__array(b: T_box) -> T_box:
    extract_args(Array.create, b)  # type: ignore
    return to_ast(create_and_fill(b))  # type: ignore


@register(to_ast)
def to_ast_already_ast(b: T_box) -> T_box:
    if not is_ast(b):
        return NotImplemented
    return b
