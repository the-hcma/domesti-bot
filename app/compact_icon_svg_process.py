"""Normalize Illustrator-exported compact tile SVGs for transparent tiles."""

from __future__ import annotations

import re
from xml.etree import ElementTree as ET

_SVG_NS = "http://www.w3.org/2000/svg"
_DEVICE_SCALE = 0.1
_MATTE_AREA_THRESHOLD = 900_000.0
_NORMALIZED_VIEWBOX_SIZE = 100.0
_NORMALIZED_MARGIN = 0.1
_NUMBER = re.compile(r"[-+]?(?:\d*\.\d+|\d+)(?:[eE][-+]?\d+)?")


def _collapse_path_data(d: str) -> str:
    return re.sub(r"\s+", " ", d).strip()


def _compound_icon_subpaths(subpaths: list[str]) -> list[str]:
    areas = [_path_area(subpath) for subpath in subpaths]
    matte_index = max(range(len(subpaths)), key=lambda index: areas[index])
    label_bounds = [_path_axis_bounds(subpath) for subpath in subpaths]
    kept: list[str] = []
    for index, subpath in enumerate(subpaths):
        if index == matte_index and areas[index] >= _MATTE_AREA_THRESHOLD:
            continue
        if _is_label_path(subpath, label_bounds):
            continue
        kept.append(subpath)
    return kept


def _device_bounds(
    d: str,
    view_height: float,
) -> tuple[float, float, float, float]:
    xmin, ymin, xmax, ymax = _path_axis_bounds(d)
    return (
        xmin * _DEVICE_SCALE,
        view_height - ymax * _DEVICE_SCALE,
        xmax * _DEVICE_SCALE,
        view_height - ymin * _DEVICE_SCALE,
    )


def _extract_icon_paths(path_data: list[str]) -> list[str]:
    primary_path, primary_icons = _primary_compound_icons(path_data)
    icon_paths = list(primary_icons)
    label_reference_bounds = [
        _path_axis_bounds(d) for d in path_data if not _is_outer_canvas_path(d)
    ]
    for d in path_data:
        if d == primary_path or _is_outer_canvas_path(d):
            continue
        subpaths = _split_subpaths(d)
        if len(subpaths) >= 2:
            icon_paths.extend(_compound_icon_subpaths(subpaths))
        elif _path_area(d) < _MATTE_AREA_THRESHOLD and not _is_overlay_filled_path(
            d, primary_icons
        ):
            if _skip_standalone_for_compound_export(primary_icons):
                continue
            icon_paths.append(d)
    return [d for d in icon_paths if not _is_label_path(d, label_reference_bounds)]


def _find_parent(root: ET.Element, target: ET.Element) -> ET.Element | None:
    for parent in root.iter():
        if target in list(parent):
            return parent
    return None


def _icon_artwork_bounds(
    bounds_list: list[tuple[float, float, float, float]],
) -> list[tuple[float, float, float, float]]:
    artwork = [bounds for bounds in bounds_list if bounds[3] > 400]
    return artwork if artwork else bounds_list


def _is_label_path(d: str, bounds_list: list[tuple[float, float, float, float]]) -> bool:
    if not bounds_list:
        return False
    _xmin, _ymin, xmax, ymax = _path_axis_bounds(d)
    height = ymax - _ymin
    width = xmax - _xmin
    artwork_bounds = _icon_artwork_bounds(bounds_list)
    global_ymax = max(bounds[3] for bounds in artwork_bounds)
    if global_ymax <= 0:
        return False
    in_bottom_band = ymax < global_ymax * 0.44
    aspect = width / max(height, 1.0)
    text_like = height <= 360 and width >= 40 and aspect >= 0.65
    return in_bottom_band and text_like


def _is_outer_canvas_path(d: str) -> bool:
    collapsed = _collapse_path_data(d)
    return bool(
        re.match(r"^M0\s+\d+", collapsed) and "570 0" in collapsed and "l0 -" in collapsed
    )


def _is_overlay_filled_path(d: str, primary_icons: list[str]) -> bool:
    """Drop bulb-style full white overlays duplicated outside the matte compound path."""
    if not primary_icons:
        return False
    compound_blob = " ".join(primary_icons)
    if "m-421" not in compound_blob and "m532" not in compound_blob:
        return False
    collapsed = _collapse_path_data(d)
    if not re.match(r"^M5\d{2}\s+\d{3}", collapsed):
        return False
    return _path_area(d) >= 300_000.0


def _local_tag(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _normalize_icon_group(root: ET.Element, path_data: list[str], view_height: float) -> None:
    if not path_data:
        return
    xmin = ymin = float("inf")
    xmax = ymax = float("-inf")
    for d in path_data:
        dx0, dy0, dx1, dy1 = _device_bounds(d, view_height)
        xmin = min(xmin, dx0)
        ymin = min(ymin, dy0)
        xmax = max(xmax, dx1)
        ymax = max(ymax, dy1)
    content_w = xmax - xmin
    content_h = ymax - ymin
    if content_w <= 0 or content_h <= 0:
        return
    margin = _NORMALIZED_MARGIN
    target = _NORMALIZED_VIEWBOX_SIZE
    fit_scale = (target * (1.0 - 2.0 * margin)) / max(content_w, content_h)
    center_x = (xmin + xmax) / 2.0
    center_y = (ymin + ymax) / 2.0
    root.set("viewBox", f"0 0 {target:g} {target:g}")
    icon_group = None
    for child in root:
        if _local_tag(child.tag) == "g":
            icon_group = child
            break
    if icon_group is None:
        return
    original = icon_group.get("transform", "")
    fit = (
        f"translate({target / 2:g},{target / 2:g}) "
        f"scale({fit_scale:g}) "
        f"translate({-center_x:g},{-center_y:g}) "
        f"{original}"
    )
    icon_group.set("transform", fit)


def _parse_view_height(root: ET.Element) -> float:
    viewbox = root.get("viewBox")
    if viewbox is None:
        return 95.0
    parts = [float(value) for value in viewbox.replace(",", " ").split()]
    if len(parts) == 4:
        return parts[3]
    return 95.0


def _path_area(d: str) -> float:
    xmin, ymin, xmax, ymax = _path_axis_bounds(d)
    return max(0.0, xmax - xmin) * max(0.0, ymax - ymin)


def _path_axis_bounds(d: str) -> tuple[float, float, float, float]:
    numbers = [float(value) for value in _NUMBER.findall(d)]
    if len(numbers) < 2:
        return 0.0, 0.0, 0.0, 0.0
    xs = numbers[0::2]
    ys = numbers[1::2]
    if len(ys) < len(xs):
        ys = [*ys, numbers[-1]]
    return min(xs), min(ys), max(xs), max(ys)


def _primary_compound_icons(path_data: list[str]) -> tuple[str | None, list[str]]:
    primary_path: str | None = None
    primary_matte_area = 0.0
    for d in path_data:
        if _is_outer_canvas_path(d):
            continue
        subpaths = _split_subpaths(d)
        if len(subpaths) < 2:
            continue
        matte_area = _path_area(subpaths[0])
        if matte_area > primary_matte_area:
            primary_matte_area = matte_area
            primary_path = d
    if primary_path is None:
        return None, []
    return primary_path, _compound_icon_subpaths(_split_subpaths(primary_path))


def _rebuild_icon_group(
    root: ET.Element,
    path_data: list[str],
) -> None:
    icon_group = None
    for child in list(root):
        if _local_tag(child.tag) == "g":
            icon_group = child
            break
    if icon_group is None:
        icon_group = ET.SubElement(root, f"{{{_SVG_NS}}}g")
    for child in list(icon_group):
        icon_group.remove(child)
    for d in path_data:
        ET.SubElement(
            icon_group,
            f"{{{_SVG_NS}}}path",
            d=d,
            fill="currentColor",
            stroke="none",
        )
    icon_group.set("fill", "currentColor")
    icon_group.set("stroke", "none")


def _skip_standalone_for_compound_export(primary_icons: list[str]) -> bool:
    compound_blob = " ".join(primary_icons)
    return "m-421" in compound_blob or "m532" in compound_blob


def _split_subpaths(d: str) -> list[str]:
    collapsed = _collapse_path_data(d)
    parts = re.split(r"(?=\s+m)", collapsed)
    return [part.strip() for part in parts if part.strip()]


def process_compact_icon_svg_bytes(raw: bytes) -> bytes:
    """Keep icon cutouts from compound paths; drop mattes, frames, overlays, and labels."""
    root = ET.fromstring(raw)
    view_height = _parse_view_height(root)
    source_paths = [
        path.get("d")
        for path in root.iter()
        if _local_tag(path.tag) == "path" and path.get("d") is not None
    ]
    icon_paths = _extract_icon_paths([d for d in source_paths if d is not None])
    _rebuild_icon_group(root, icon_paths)
    _normalize_icon_group(root, icon_paths, view_height)
    if root.get("width") is not None:
        del root.attrib["width"]
    if root.get("height") is not None:
        del root.attrib["height"]
    if root.tag.startswith("{"):
        ET.register_namespace("", _SVG_NS)
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)
