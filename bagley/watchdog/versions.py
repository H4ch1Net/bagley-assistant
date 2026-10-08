"""Arch Linux version comparison in pure Python, a port of libalpm's ``alpm_pkg_vercmp``.

Used when ``vercmp`` (part of pacman) can't run, and to pre-filter the security tracker so
``vercmp`` only confirms the few packages that look affected.
"""

from __future__ import annotations


def _isdigit(c: str) -> bool:
    return "0" <= c <= "9"


def _isalpha(c: str) -> bool:
    return "a" <= c <= "z" or "A" <= c <= "Z"


def _isalnum(c: str) -> bool:
    return _isdigit(c) or _isalpha(c)


def rpmvercmp(a: str, b: str) -> int:
    """Compare two version segments the way pacman does: -1, 0 or 1."""
    if a == b:
        return 0
    one = two = 0  # Start of the current segment.
    end1 = end2 = 0  # End of the previous segment.
    while one < len(a) and two < len(b):
        while one < len(a) and not _isalnum(a[one]):
            one += 1
        while two < len(b) and not _isalnum(b[two]):
            two += 1
        if one >= len(a) or two >= len(b):
            break
        # Separators of different lengths decide it: 1.0 < 1..0
        if one - end1 != two - end2:
            return -1 if one - end1 < two - end2 else 1
        end1, end2 = one, two
        if _isdigit(a[end1]):
            while end1 < len(a) and _isdigit(a[end1]):
                end1 += 1
            while end2 < len(b) and _isdigit(b[end2]):
                end2 += 1
            isnum = True
        else:
            while end1 < len(a) and _isalpha(a[end1]):
                end1 += 1
            while end2 < len(b) and _isalpha(b[end2]):
                end2 += 1
            isnum = False
        seg1, seg2 = a[one:end1], b[two:end2]
        if not seg2:
            return 1 if isnum else -1  # Numeric segments are newer than alpha ones.
        if isnum:
            seg1, seg2 = seg1.lstrip("0"), seg2.lstrip("0")
            if len(seg1) != len(seg2):
                return 1 if len(seg1) > len(seg2) else -1
        if seg1 != seg2:
            return 1 if seg1 > seg2 else -1
        one, two = end1, end2
    rest1, rest2 = a[one:], b[two:]
    if not rest1 and not rest2:
        return 0
    # A remaining alpha string never beats an empty one: 1.0 > 1.0rc1, 1.0.1 > 1.0.
    if (not rest1 and not _isalpha(rest2[0])) or (rest1 and _isalpha(rest1[0])):
        return -1
    return 1


def _parse_evr(version: str) -> tuple[str, str, str | None]:
    """Split ``[epoch:]version[-release]``."""
    i = 0
    while i < len(version) and _isdigit(version[i]):
        i += 1
    if i < len(version) and version[i] == ":":
        epoch, rest = version[:i] or "0", version[i + 1 :]
    else:
        epoch, rest = "0", version
    if "-" in rest:
        ver, rel = rest.rsplit("-", 1)
        return epoch, ver, rel
    return epoch, rest, None


def vercmp(a: str, b: str) -> int:
    """Compare two full package versions like ``vercmp a b``: -1 if a is older, 0, or 1."""
    if a == b:
        return 0
    epoch1, ver1, rel1 = _parse_evr(a)
    epoch2, ver2, rel2 = _parse_evr(b)
    ret = rpmvercmp(epoch1, epoch2)
    if ret == 0:
        ret = rpmvercmp(ver1, ver2)
        if ret == 0 and rel1 is not None and rel2 is not None:
            ret = rpmvercmp(rel1, rel2)
    return ret
