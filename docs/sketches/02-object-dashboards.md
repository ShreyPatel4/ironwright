# Sketch 02: Governed object dashboards

## Purpose

Every ontology object gets a standing page that answers the operator's
real question: what is this thing, what touched it, what is pending on
it, and who may do what to it. Foundry's object view, built from tables
we already keep.

## What it reads (verified shapes)

- `objects(type, id, title, props, version, updated_at)` and `links`
- `object_history(type, id, version, props, changed_by, action_run, ts)`
- `crosswalk(type, id, system, source_key, method, confidence)`
- `audit_log` rows whose `objects` JSON mentions this id (already stored
  per action; the Agents report's runs carry `objects` too)
- pending approvals filtered to params naming this id
- policy: which actions on this type the current principal may take
  (dry-run the real PolicyEngine per action, the Governance tester's path)

## UI

```
Vendor · VEN-9245f9e5                         ● L3 workspace badge
Harbor Pet Foods LLC                     [flag_for_review] [propose..]
--------------------------------------------------------------------
PROPS                    IDENTITY (crosswalk)        version 3
name, tax_id, email...   mainframe 100207  exact:tax_id  1.00
                         portal   V-HAR02  exact:tax_id  1.00
                         portal   V-HAR99  exact:tax_id  1.00
--------------------------------------------------------------------
TRAIL  seq 41  agent:retail-triage  flag_for_review   applied
       seq 23  user:cm.1            propose_facing..  needs_approval
HISTORY v1 -> v2 by ar-54340d (diff on click)
LINKS  supplies -> 12 Items          PENDING  1 approval on this object
--------------------------------------------------------------------
action buttons render ONLY if the dry-run says permit; a denied action
shows as text with the denying rule, which is governance made visible
```

## Endpoints

- `GET /workspaces/{ws}/objects/{type}/{id}/dashboard` -> props + crosswalk
  + history + trail (audit rows touching id) + pending + allowed_actions
  (list of {action, allowed, rule}) for the X-Principal.

## Gate

Done when: the Harbor Pet Foods page shows its three-source identity, its
audit trail including the denied delist attempt, and the action row
differs between user:category.manager.1 and vendor:V-HAR02 purely from
policy dry-runs; a test asserts both renders from the API payload.

## Not in this slice

Editing props from the page; cross-object graph canvas; saved views.
