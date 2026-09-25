"""Utilities for walking pydantic models."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Concatenate

from pydantic import BaseModel


def apply_func_to_model_attr_type[M: BaseModel, T, **P](
    source_model_instance: M,
    target_attribute_type: type[T],
    func_to_apply: Callable[Concatenate[T, P], Any],
    *args: P.args,
    **kwargs: P.kwargs,
) -> M:
    """Recursively apply ``func_to_apply`` to every attribute matching a type.

    Nested pydantic models are traversed depth-first. The source instance is
    mutated in place by ``func_to_apply`` and returned for chaining.
    """
    for _, source_model_value in source_model_instance:
        if isinstance(source_model_value, target_attribute_type):
            func_to_apply(source_model_value, *args, **kwargs)
        elif isinstance(source_model_value, BaseModel):
            apply_func_to_model_attr_type(
                source_model_value, target_attribute_type, func_to_apply, *args, **kwargs
            )
    return source_model_instance
