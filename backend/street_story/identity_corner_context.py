"""Actual OSM segment adjacency for the spatial SOURCE/MAP evidence packet.

This module does NOT infer photographed facades, yaw, building identity or camera
distance. It only indexes already transmitted OSM boundary segments, so the
LLM can nominate a real joined corner rather than two unrelated long walls.
"""
from __future__ import annotations

import math


def observed_connected_pairs(sides, *, max_pairs=8, closed_rings=None):
    """Return bounded [ring, first_index, second_index, turn_degrees].

    Input side rows are [ring_index, segment_index, length_m, start_xy, end_xy]
    from immutable OSM vertices. Edges must be consecutive indices on the
    SAME observed ring and share its already supplied endpoint. Rounding map
    coordinates cannot create a join between non-neighbouring raw OSM edges.
    Optional closed_rings maps confirmed closed ring indices to the final edge
    index; only then may that edge connect back to index zero. No intersection,
    facade or missing vertex is manufactured.
    """
    by_start = {}
    valid = []
    for side in sides:
        if (not isinstance(side, (list, tuple)) or len(side) < 5
                or type(side[0]) is not int or type(side[1]) is not int
                or side[0] < 0 or side[1] < 0
                or not isinstance(side[2], (int, float)) or isinstance(side[2], bool)
                or not math.isfinite(side[2]) or side[2] <= 0
                or not isinstance(side[3], (list, tuple)) or len(side[3]) != 2
                or not isinstance(side[4], (list, tuple)) or len(side[4]) != 2):
            continue
        coords = [*side[3], *side[4]]
        if any(isinstance(v, bool) or not isinstance(v, (int, float))
                or not math.isfinite(v) for v in coords):
            continue
        if tuple(side[3]) == tuple(side[4]):
            continue
        valid.append(side)
        by_start.setdefault((side[0], tuple(side[3])), []).append(side)
    pairs = []
    for first in valid:
        for second in by_start.get((first[0], tuple(first[4])), []):
            forward = second[1] == first[1] + 1
            closed = (second[1] == 0 and isinstance(closed_rings, dict)
                and closed_rings.get(first[0]) == first[1])
            if not (forward or closed):
                continue
            vx, vy = first[4][0]-first[3][0], first[4][1]-first[3][1]
            wx, wy = second[4][0]-second[3][0], second[4][1]-second[3][1]
            degrees = round(math.degrees(math.atan2(vx*wy-vy*wx, vx*wx+vy*wy)), 1)
            pairs.append((first, second, degrees))
    pairs.sort(key=lambda item: (
        -max(item[0][2], item[1][2]), -min(item[0][2], item[1][2]),
        item[0][0], item[0][1], item[1][1]))
    return ([[a[0], a[1], b[1], turn] for a,b,turn in pairs[:max_pairs]],
            max(0, len(pairs)-max_pairs))


def preserve_one_connected_pair(all_sides, shown_sides, *, limit=4, closed_rings=None):
    """Only repair a completely disconnected *display excerpt*, not its geometry.

    When four longest edges were all separate, show one REAL connected pair and
    retain up to two other longest edges. The complete OSM outline and pool never
    change; map_detail continues to expose every actual side.
    """
    if len(shown_sides) > limit or len(shown_sides) < 2 or observed_connected_pairs(
            shown_sides, closed_rings=closed_rings)[0]:
        return shown_sides
    pairs, _ = observed_connected_pairs(all_sides, max_pairs=max(1,len(all_sides)*2),
        closed_rings=closed_rings)
    if not pairs:
        return shown_sides
    ring, first_idx, second_idx, _turn = pairs[0]
    entries = {(s[0],s[1]):s for s in all_sides}
    chosen = [entries[(ring,first_idx)],entries[(ring,second_idx)]]
    chosen_ids = {(s[0],s[1]) for s in chosen}
    chosen.extend(s for s in shown_sides if (s[0],s[1]) not in chosen_ids)
    return chosen[:limit]
