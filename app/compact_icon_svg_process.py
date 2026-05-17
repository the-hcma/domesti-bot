"""Normalize Illustrator-exported compact tile SVGs for transparent tiles."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_SVG_NS = "http://www.w3.org/2000/svg"
_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _collapse_path_data(d: str) -> str:
    return re.sub(r"\s+", " ", d).strip()


def _find_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        if target in list(parent):
            return parent
    return None


def _is_background_path(d: str) -> bool:
    collapsed = _collapse_path_data(d)
    if re.match(r"^M0\s+\d+", collapsed) and "570 0" in collapsed and "l0 -" in collapsed:
        return True
    if re.search(r"\b44[67]\s+-3\b", collapsed):
        return True
    if re.search(r"\b444\s+0\s+444\s+0", collapsed) and re.search(
        r"\b0\s+35[0-9]\b",
        collapsed,
    ):
        return True
    if (
        re.search(r"\bl0\s+-\d{3}\b", collapsed)
        and re.search(r"\b0\s+35[0-9]\b", collapsed)
        and re.search(r"\b4[34]\d\s+0\s+4[34]\d\s+0", collapsed)
        and re.match(r"^M\d{2,3}\s+\d{3}", collapsed)
    ):
        return True
    return False


def _is_black_fill(elem: ET.Element) -> bool:
    fill = (elem.get("fill") or "").strip().lower()
    if fill in {"#000", "#000000", "black", "rgb(0,0,0)", "rgb(0%,0%,0%)"}:
        return True
    style = (elem.get("style") or "").lower()
    return any(token in style for token in ("fill:#000", "fill:black", "fill:rgb(0,0,0)"))


def _icon_artwork_bounds(
    bounds_list: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    """Ignore bottom-band paths when measuring artwork height (labels skew the span)."""
    artwork = [bounds for bounds in bounds_list if bounds[3] > 400]
    return artwork if artwork else bounds_list


def _is_label_path(d: str, bounds_list: list[tuple[float, float, float, float]]) -> bool:
    if not bounds_list:
        return False
    _xmin, ymin, xmax, ymax = _path_axis_bounds(d)
    height = ymax - ymin
    width = xmax - _xmin
    artwork_bounds = _icon_artwork_bounds(bounds_list)
    global_ymax = max(bounds[3] for bounds in artwork_bounds)
    if global_ymax <= 0:
        return False
    in_bottom_band = ymax < global_ymax * 0.44
    aspect = width / max(height, 1.0)
    text_like = height <= 360 and width >= 40 and aspect >= 0.65
    return in_bottom_band and text_like


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _path_axis_bounds(d: str) -> tuple[float, float, float, float]:
    numbers = [float(value) for value in _NUMBER.findall(d)]
    if len(numbers) < 2:
        return 0.0, 0.0, 0.0, 0.0
    xs = numbers[0::2]
    ys = numbers[1::2]
    if len(ys) < len(xs):
        ys = [*ys, numbers[-1]]
    return min(xs), min(ys), max(xs), max(ys)


def _remove_path(root: ET.Element, path_elem: ET.Element) -> None:
    parent = _find_parent(root, path_elem)
    if parent is not None:
        parent.remove(path_elem)


def _set_current_color_fills(root: ET.Element) -> None:
    for elem in root.iter():
        if _local_tag(elem.tag) == "g" and elem.get("fill") == "white":
            elem.set("fill", "currentColor")
        if _local_tag(elem.tag) == "path":
            if elem.get("fill") == "white":
                elem.set("fill", "currentColor")
            if _is_black_fill(elem):
                elem.set("fill", "none")


def process_compact_icon_svg_bytes(raw: bytes) -> bytes:
    """Drop labels/backgrounds; keep white artwork as ``currentColor`` on transparency."""
    root = ET.fromstring(raw)
    path_elems = [
        elem
        for elem in root.iter()
        if _local_tag(elem.tag) == "path" and elem.get("d") is not None
    ]
    for path_elem in list(path_elems):
        d = path_elem.get("d")
        assert d is not None
        if _is_black_fill(path_elem) or _is_background_path(d):
            _remove_path(root, path_elem)

    remaining: list[str] = []
    for elem in root.iter():
        if _local_tag(elem.tag) != "path":
            continue
        d = elem.get("d")
        if d is not None:
            remaining.append(d)
    bounds_list = [_path_axis_bounds(d) for d in remaining]
    for path_elem in list(root.iter()):
        if _local_tag(path_elem.tag) != "path":
            continue
        d = path_elem.get("d")
        if d is None:
            continue
        if _is_label_path(d, bounds_list):
            _remove_path(root, path_elem)

    _set_current_color_fills(root)
    if root.get("width") is not None:
        del root.attrib["width"]
    if root.get("height") is not None:
        del root.attrib["height"]
    if root.tag.startswith("{"):
        ET.register_namespace("", _SVG_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
