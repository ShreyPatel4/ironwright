# Sketch 03: Boards, the built-in BI

## Purpose

Pin any Explorer query as a tile; lay tiles on a board; every tile carries
its SQL and its freshness. Not a Looker clone: a truthful, minimal BI for
the lakehouse that is already queryable. The embed door (Looker/Power BI)
stays separate and later.

## What it reads

- The existing `/lake/query` surface (SELECT-only guard unchanged).
- Board definitions: user data, stored server-side under the workspace
  (`data_root/<ws>/boards/<slug>.json`), never in the repo. Shape:
  `{title, tiles: [{id, sql, viz: table|number|bar|line, pos, title}]}`.

## UI

```
BOARDS · weekly ops                         [new tile] [run all]
--------------------------------------------------------------------
+----------------------+  +----------------------+  +--------------+
| Gold rows by table   |  | Unexplained diffs    |  | Chain ratio  |
| [bar]                |  | 0                    |  | [line]       |
| run 14:02 · 0.3s     |  | number · run 14:02   |  | run 14:02    |
| sql ▸                |  | sql ▸                |  | sql ▸        |
+----------------------+  +----------------------+  +--------------+
every tile: its SQL one click away, last-run time always visible,
stale tiles say stale instead of showing old numbers as current
```

Charts hand-rolled SVG (bar, line, single number); no chart library until
a board needs more than those three.

## Endpoints

- `GET/PUT /workspaces/{ws}/boards` and `/boards/{slug}`; `POST
  /boards/{slug}/run` executes every tile through the same guarded query
  path and returns per-tile results + timings.

## Gate

Done when: a board with three tiles over retail gold survives server
restart, re-runs with one click, and a test proves tile SQL goes through
the SELECT-only guard (an UPDATE tile is rejected at save, not at run).

## Not in this slice

Cross-workspace boards; sharing; scheduled refresh (that is the
automations question and it stays honest: none until a scheduler exists).
