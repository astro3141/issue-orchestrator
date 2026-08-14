"""One normalisation of "the commit this evidence is about".

Foundation admission (``docs/foundation/VALIDATED_WORK_DISPOSITION.md`` §4)
is stated as an equality between SHAs::

    validation.head_sha == review.reviewed_sha == A

An equality is only as trustworthy as the normalisation on both sides of it.
Two records that each accept an abbreviated or upper-case SHA, normalising
differently or not at all, can describe the same commit and still compare
unequal — or, worse, compare equal by truncation. So the rule lives in one
place and every authority record binding evidence to a candidate uses it.

Rejection rather than repair is deliberate. An abbreviated SHA *could* be
expanded by asking a repository, but that turns a pure value rule into an I/O
one and makes the answer depend on which repository was asked. A record that
cannot name its candidate exactly is not a record a later gate should admit.
"""

from __future__ import annotations

SHA_LENGTH = 40
_HEX_DIGITS = frozenset("0123456789abcdef")


def normalize_commit_sha(value: object, *, field_name: str) -> str:
    """Return ``value`` as a canonical full commit SHA, or raise.

    Abbreviated, uppercase, or non-hex values are rejected rather than
    normalised into something that would compare unequal to a real HEAD later.
    """
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be str")
    normalized = value.strip().lower()
    if len(normalized) != SHA_LENGTH or not set(normalized) <= _HEX_DIGITS:
        raise ValueError(
            f"{field_name} must be a full {SHA_LENGTH}-character hex SHA, "
            f"got {value!r}"
        )
    return normalized
