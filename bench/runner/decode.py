"""Decode JSON at record boundaries using the record's annotations."""

from __future__ import annotations

import math
import types
from dataclasses import MISSING, fields, is_dataclass
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


def decode(cls, value):
    origin, args = get_origin(cls), get_args(cls)
    if cls is Any:
        return value
    if origin in (Union, types.UnionType):
        for option in args:
            try:
                return decode(option, value)
            except (ValueError, TypeError):
                pass
        raise ValueError(f"expected {cls}")
    if origin is Literal:
        if any(type(value) is type(v) and value == v for v in args):
            return value
        raise ValueError(f"expected {cls}")
    if is_typeddict(cls):
        hints = get_type_hints(cls)
        if not isinstance(value, dict) or cls.__required_keys__ - value.keys():
            raise ValueError(f"incomplete {cls.__name__}")
        if value.keys() - hints.keys():
            raise ValueError(f"invalid fields for {cls.__name__}")
        return {k: decode(hints[k], v) for k, v in value.items()}
    if is_dataclass(cls):
        if not isinstance(value, dict):
            raise ValueError(f"expected object for {cls.__name__}")
        fs, hints = fields(cls), get_type_hints(cls)
        required = {
            f.name for f in fs if f.default is MISSING and f.default_factory is MISSING
        }
        if required - value.keys() or value.keys() - {f.name for f in fs}:
            raise ValueError(f"invalid fields for {cls.__name__}")
        decoded = {k: decode(hints[k], v) for k, v in value.items()}
        record = cls(
            **{f.name: decoded[f.name] for f in fs if f.init and f.name in decoded}
        )
        if any(not f.init and getattr(record, f.name) != decoded[f.name] for f in fs):
            raise ValueError(f"invalid derived fields for {cls.__name__}")
        return record
    if origin in (tuple, list) or cls is tuple:
        if not isinstance(value, (list, tuple)):
            raise ValueError("expected array")
        if not args:
            result = list(value)
        elif (len(args) == 2 and args[1] is Ellipsis) or origin is list:
            result = [decode(args[0], v) for v in value]
        else:
            if len(value) != len(args):
                raise ValueError("wrong tuple length")
            result = [decode(t, v) for t, v in zip(args, value, strict=True)]
        return tuple(result) if origin is tuple or cls is tuple else result
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError("expected object")
        return {decode(args[0], k): decode(args[1], v) for k, v in value.items()}
    if cls is float:
        if type(value) not in (int, float) or not math.isfinite(value):
            raise ValueError("expected finite number")
        return float(value)
    if cls in (str, int, bool, type(None)) and type(value) is cls:
        return value
    raise ValueError(f"expected {cls}")
