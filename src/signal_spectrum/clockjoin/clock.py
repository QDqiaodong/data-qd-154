"""Exact-rational difference-constraint engine for clock joining.

Every sample time is modelled as::

    T(s, i) = x_s + 10**9 * i / sample_rate_s   [ns]

where ``x_s`` is the source's unknown clock offset.  The reference source is
pinned at ``x_ref = 0``.  Each anchor or bundle relation constrains

    lower <= T(right) - T(left) <= upper

which, with ``delta = 10**9*(i_R/r_R - i_L/r_L)``, becomes the two
difference constraints

    x_R <= x_L + (upper - delta)     [upper side: L -> R]
    x_L <= x_R + (delta - lower)     [lower side: R -> L]

An edge ``u -> v`` with weight ``w`` therefore means ``x_v <= x_u + w``.
All arithmetic is exact (:class:`fractions.Fraction`); weights are scaled by
a shared positive integer denominator so Bellman-Ford runs on plain Python
integers without any floating-point tolerance.

Feasibility of the *whole* bundle is decided on the joint graph of base
anchors plus that bundle's relations -- never by testing relations one at a
time.  When the joint graph is infeasible a real negative cycle of original
constraint edges is returned as machine-checkable evidence.

For a feasible system the componentwise-minimal solution is
``x_s = -dist(s, ref)`` (shortest path from ``s`` to the reference), which is
simultaneously minimal for every variable and satisfies every constraint.
"""
import math
from fractions import Fraction

from .errors import ClockJoinError

UPPER = "upper"
LOWER = "lower"
ANCHOR = "anchor"
RELATION = "relation"


class Edge:
    """One directed constraint edge, ``x_to <= x_from + weight``."""

    __slots__ = ("kind", "id", "side", "from_id", "to_id",
                 "from_idx", "to_idx", "weight")

    def __init__(self, kind, ident, side, from_id, to_id, from_idx, to_idx,
                 weight):
        self.kind = kind
        self.id = ident
        self.side = side
        self.from_id = from_id
        self.to_id = to_id
        self.from_idx = from_idx
        self.to_idx = to_idx
        self.weight = weight

    def sort_key(self):
        return (self.from_idx, self.to_idx, self.kind, self.id, self.side)


def constraint_edges(kind, ident, left_id, left_idx, right_id, right_idx,
                     delta, lower, upper):
    """Expand one two-sided constraint into its upper/lower directed edges."""
    return [
        Edge(kind, ident, UPPER, left_id, right_id, left_idx, right_idx,
             upper - delta),
        Edge(kind, ident, LOWER, right_id, left_id, right_idx, left_idx,
             delta - lower),
    ]


def _scaled(edges):
    """Return ``(scale, [(u, v, int_weight)])`` for exact integer BF."""
    scale = 1
    for edge in edges:
        scale = math.lcm(scale, edge.weight.denominator)
    integer = [(edge.from_idx, edge.to_idx,
                edge.weight.numerator * (scale // edge.weight.denominator))
               for edge in edges]
    return scale, integer


def floyd_base(node_count, edges):
    """All-pairs shortest paths of the base anchor graph.

    Returns ``(dist, scale)`` where ``dist[u][v]`` is an integer in units of
    ``1/scale``.  Raises :class:`ClockJoinError` if the base anchors are
    contradictory (negative cycle) or leave any source without finite bounds
    relative to the reference (an unreachable pair).
    """
    scale, integer = _scaled(edges)
    dist = [[None] * node_count for _ in range(node_count)]
    for node in range(node_count):
        dist[node][node] = 0
    for u, v, w in integer:
        if dist[u][v] is None or w < dist[u][v]:
            dist[u][v] = w
    for mid in range(node_count):
        row_mid = dist[mid]
        for u in range(node_count):
            row_u = dist[u]
            via = row_u[mid]
            if via is None:
                continue
            for v in range(node_count):
                step = row_mid[v]
                if step is None:
                    continue
                candidate = via + step
                current = row_u[v]
                if current is None or candidate < current:
                    row_u[v] = candidate
    for node in range(node_count):
        if dist[node][node] < 0:
            raise ClockJoinError(
                "base anchors are contradictory (negative cycle)")
    for u in range(node_count):
        for v in range(node_count):
            if dist[u][v] is None:
                raise ClockJoinError(
                    "base anchors leave a source without finite bounds "
                    "relative to the reference")
    return dist, scale


def _walk_predecessors(integer_edges, pred, start, node_count):
    """Return a real negative cycle as a list of edge indices, or ``None``.

    ``pred[v]`` is the index of the final edge that relaxed ``v``.  Following
    those links ``node_count`` times from any node reachable by a negative
    cycle lands on the cycle; walking until the node repeats lists its
    edges, which are then verified to close up and to have negative total.
    """
    node = start
    for _ in range(node_count):
        edge_index = pred[node]
        if edge_index < 0:
            return None
        node = integer_edges[edge_index][0]
    # ``node`` now lies on (or leads into) a negative cycle.
    seen = {}
    order = []
    current = node
    while current not in seen:
        edge_index = pred[current]
        if edge_index < 0:
            return None
        seen[current] = len(order)
        order.append(current)
        current = integer_edges[edge_index][0]
    members = order[seen[current]:]
    # pred[m] is the edge ``from -> m``; traversing the cycle forwards means
    # taking, for each member, the edge that enters the *previous* member, in
    # reverse member order.  Reversing the collected edge list yields a walk
    # whose head/tail nodes chain in cycle order.
    cycle = [pred[member] for member in members]
    cycle.reverse()
    for position, edge_index in enumerate(cycle):
        nxt = cycle[(position + 1) % len(cycle)]
        if integer_edges[edge_index][1] != integer_edges[nxt][0]:
            return None
    if sum(integer_edges[index][2] for index in cycle) >= 0:
        return None
    return cycle


def find_negative_cycle(node_count, edges):
    """Return a real negative cycle (list of :class:`Edge`) or ``None``.

    Edges are visited in a canonical order derived from source ids and
    constraint identifiers, so the same input always yields the same cycle
    even when array order in the job file changes.
    """
    ordered = sorted(edges, key=Edge.sort_key)
    scale, integer = _scaled(ordered)
    dist = [0] * node_count
    pred = [-1] * node_count
    last = -1
    for iteration in range(node_count):
        changed = False
        witness = -1
        for index, (u, v, w) in enumerate(integer):
            candidate = dist[u] + w
            if candidate < dist[v]:
                dist[v] = candidate
                pred[v] = index
                changed = True
                witness = v
        if not changed:
            return None
        last = witness
    # Still relaxing after node_count full passes: a negative cycle exists.
    indices = _walk_predecessors(integer, pred, last, node_count)
    if indices is None:
        # Deterministically try every node as the entry point.
        for start in range(node_count):
            indices = _walk_predecessors(integer, pred, start, node_count)
            if indices is not None:
                break
    if indices is None:
        raise ClockJoinError(
            "constraint system is infeasible but no negative cycle could be "
            "extracted")
    return [ordered[index] for index in indices]


def shortest_to_reference(node_count, edges, ref_idx):
    """Return ``-dist(s, ref)`` for every node as exact rationals.

    Used only after feasibility is established; every distance is finite
    because the base anchors pin each source above and below the reference.
    """
    scale, integer = _scaled(edges)
    reverse = [[] for _ in range(node_count)]
    for u, v, w in integer:
        reverse[v].append((u, w))
    dist = [None] * node_count
    dist[ref_idx] = 0
    for _ in range(node_count - 1):
        changed = False
        for node in range(node_count):
            base = dist[node]
            if base is None:
                continue
            for target, weight in reverse[node]:
                candidate = base + weight
                current = dist[target]
                if current is None or candidate < current:
                    dist[target] = candidate
                    changed = True
        if not changed:
            break
    if any(value is None for value in dist):
        raise ClockJoinError(
            "strengthened constraints leave a source without finite bounds")
    return [Fraction(-value, scale) for value in dist]


def relation_guaranteed(base_dist, base_scale, left_idx, right_idx, delta,
                        lower, upper):
    """True iff every base-feasible offset vector satisfies the relation.

    The maximum of ``x_R - x_L`` over the base region is the shortest path
    ``dist(L,R)``; the minimum is ``-dist(R,L)``.  The relation is redundant
    exactly when those extremes already respect the bounds.
    """
    max_diff = Fraction(base_dist[left_idx][right_idx], base_scale)
    min_diff = Fraction(-base_dist[right_idx][left_idx], base_scale)
    return max_diff <= upper - delta and min_diff >= lower - delta
