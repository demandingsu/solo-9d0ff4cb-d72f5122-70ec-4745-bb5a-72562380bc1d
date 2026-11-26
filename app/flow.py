"""Dinic 最大流（整数容量）。

试配建模
--------
页码节点 1 … N+1。一垛可用散帖 [a, b]（版号逐页相符）是一条有向边
``a -> b+1``，容量 = 该垛当前剩余份数。
每本待配样书（已配到 k 页）是一单位流：
``超级源 -> 样书节点(容量1) -> 页码 k+1 -> …散帖边… -> N+1 -> 超级汇``。
最大流值 = 最多能同时补齐的样书本数；对流做路径分解即得到逐本取帖单。
所有容量都是整数，最大流天然给出整数分配。
"""

from __future__ import annotations

import sys
from collections import deque
from dataclasses import dataclass

sys.setrecursionlimit(1_000_000)


class Edge:
    __slots__ = ("to", "rev", "cap", "init_cap", "tag")

    def __init__(self, to: int, rev: int, cap: int, tag: int | None = None) -> None:
        self.to = to
        self.rev = rev
        self.cap = cap
        self.init_cap = cap
        self.tag = tag  # 散帖垛 ID（散帖边使用）


class Dinic:
    def __init__(self, n: int) -> None:
        self.n = n
        self.graph: list[list[Edge]] = [[] for _ in range(n)]
        self.level: list[int] = [-1] * n
        self.it: list[int] = [0] * n

    def add_edge(self, u: int, v: int, capacity: int, tag: int | None = None) -> Edge:
        forward = Edge(v, len(self.graph[v]), capacity, tag)
        backward = Edge(u, len(self.graph[u]), 0)
        self.graph[u].append(forward)
        self.graph[v].append(backward)
        return forward

    def _bfs(self, s: int, t: int) -> bool:
        self.level = [-1] * self.n
        self.level[s] = 0
        queue: deque[int] = deque([s])
        while queue:
            u = queue.popleft()
            for e in self.graph[u]:
                if e.cap > 0 and self.level[e.to] == -1:
                    self.level[e.to] = self.level[u] + 1
                    queue.append(e.to)
        return self.level[t] != -1

    def _dfs(self, u: int, t: int, pushed: int) -> int:
        if u == t:
            return pushed
        while self.it[u] < len(self.graph[u]):
            e = self.graph[u][self.it[u]]
            if e.cap > 0 and self.level[e.to] == self.level[u] + 1:
                sent = self._dfs(e.to, t, min(pushed, e.cap))
                if sent:
                    e.cap -= sent
                    self.graph[e.to][e.rev].cap += sent
                    return sent
            self.it[u] += 1
        return 0

    def max_flow(self, s: int, t: int) -> int:
        total = 0
        inf = 10**18
        while self._bfs(s, t):
            self.it = [0] * self.n
            while True:
                pushed = self._dfs(s, t, inf)
                if pushed == 0:
                    break
                total += pushed
        return total


@dataclass
class PileEdgeRecord:
    pile_id: int
    from_page: int
    to_page_exclusive: int  # end_page + 1
    edge: Edge


def reachable(adjacency: dict[int, list[int]], start: int, target: int) -> bool:
    """DAG/通用图可达性（页码严格递增，实际是 DAG）。"""
    seen = {start}
    stack = [start]
    while stack:
        u = stack.pop()
        if u == target:
            return True
        for v in adjacency.get(u, ()):  # type: ignore[arg-type]
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return False
