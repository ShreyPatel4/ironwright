# How I walk through Ironwright

## The 5 minute version

1. **The problem (30s).** "Most companies that want agents still run on mainframe files and old
   databases. The hard part isn't the agent. It's getting to data and actions you can trust."
2. **The picture (60s).** Show the stack strip at the top of the demo: legacy, bronze, silver,
   gold, ontology, agents, evals. Four companies, one engine.
3. **One run (2 min).** Open banking AML. Pick the alert the agent wanted to close. Show:
   it tried `close_alert`, policy denied it (agents never close alerts), it fell back to
   `recommend_close`, which needs an analyst. The analyst approved, and the resume is a separate
   trace linked to the first. Every sentence in the narrative cites an evidence id.
4. **Why it's trustworthy (60s).** Evals at every layer, ground truth from the generator.
   Show the confusion matrix and the action safety tests.
5. **Cost (30s).** All of it runs on a laptop for $0. It deploys to Cloudflare free tier, and
   each pack lists its AWS mapping.

## The 20 minute version

Add, in this order, depending on who's in the room:

- **Data engineers:** Write Audit Publish on Iceberg branches. Show the quarantine table in
  insurance. Show reconciliation explaining the double-counted POS file in retail.
- **Architects:** the ontology YAML. One owner per object type, per-property source precedence,
  the crosswalk. Why two Maria Garcias with different birth dates stay separate (`veto_if_conflict`).
- **AI engineers:** rules decide and the LLM narrates. Groundedness guardrail. Checkpointed
  interrupts. Tools generated from the ontology into MCP.
- **Security / risk:** default deny, forbid beats permit, policy before validation (build log #1),
  sensitive fields masked by principal, hash-chained audit, compensation on rejected writebacks.

## Questions I expect

**Why not let the LLM decide?** You can't test it cheaply, and you can't explain a wrong
decision to an auditor. Rules give a label you can eval. The model is used where it's good:
explaining evidence in plain language. Swapping in a model classifier later is one node.

**Why an ontology instead of just gold tables?** Gold answers questions. The ontology also owns
identity and writes. An agent needs "this vendor" (one object from three systems) and "reorder
this item" (a governed action with an audit trail), not a table name.

**How would this scale?** Swap the pieces, keep the contracts: S3 Tables for the warehouse,
Glue or Spark for models, Aurora for the store, AgentCore for runtime and Cedar policy. The packs
and the evals don't change.

**What's fake?** The companies and the data. The engine is real: real Iceberg snapshots and
branches, real LangGraph checkpoints, real policy evaluation, real hash chain.
