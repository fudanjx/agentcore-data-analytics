"""String casing helpers."""

from __future__ import annotations


def pascal_to_snake(pascal_string: str) -> str:
    """Convert PascalCase or camelCase into snake_case."""
    character_list: list[str] = []
    for index, char in enumerate(pascal_string):
        if char.isupper():
            if index < len(pascal_string) - 1 and pascal_string[index + 1].islower():
                character_list.append("_" + char.lower())
                continue
            if index != 0 and pascal_string[index - 1].islower():
                character_list.append("_" + char)
                continue
        character_list.append(char)
    return "".join(character_list).lstrip("_").lower()
