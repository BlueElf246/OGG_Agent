## Plan: Oracle GoldenGate Monitor Graph

Build a LangGraph workflow that turns GoldenGate process monitoring into a bounded investigation loop: collect process status, detect anomalies, enrich with diagnostics, assess impact and likely cause, then either resolve, notify, or escalate. Reuse the lesson’s single shared state plus conditional loop pattern, but replace essay drafting/review with operational analysis and remediation decisions. The monitor_plan node should produce explicit monitoring rules in three categories: process state, lag time, and disk usage.

**Steps**
1. Define a monitoring-oriented shared state. Include fields for monitor_target, run_mode, inventory, process_snapshot, anomalies, evidence, assessment, actions, notifications, escalation_decision, incident_status, poll_count, max_polls, investigation_round, max_investigation_rounds, last_error, and monitor_rules. Inside monitor_rules, separate rule groups for process_state_rules, lag_time_rules, and disk_usage_rules. This replaces the lesson’s task/plan/draft/critique/content/revision fields with operational equivalents.
2. Phase 1: intake and scope definition. Add an intake node that accepts the Oracle GoldenGate environment, process filters, thresholds, and alerting policy. Add a monitor_plan node that converts those inputs into a concrete run plan: which processes to inspect, what health criteria apply, and when to escalate. The primary output of monitor_plan should be three explicit rule sets: process state rules such as running, stopped, abended, or missing; lag time rules such as warning and critical lag thresholds; and disk usage rules such as filesystem warning and critical thresholds for GoldenGate homes, trail locations, and report directories. This is the monitoring equivalent of the lesson’s planner node.
3. Phase 2: inventory and status collection. Add a discover_processes node to enumerate Extract, Replicat, Distribution, Receiver, and Manager processes from GGSCI, Admin Client, REST API, or deployment metadata. Add a collect_status node that gathers current process state, lag, checkpoint age, abended/stopped/running status, error snippets, last restart time, host/deployment metadata, and filesystem usage for the GoldenGate installation, trail file locations, and log/report directories. This is the operational equivalent of the initial research node.
4. Phase 3: anomaly detection and severity classification. Add a detect_anomalies node that compares collected status against the monitor_plan rule sets and creates structured anomaly records, such as abended process, excessive lag, long checkpoint age, repeated restart, missing expected process, or disk usage above warning or critical thresholds. Add a classify_severity node that assigns severity and operator priority based on process criticality, duration, business impact, and whether a disk threshold threatens trail growth or process stability.
5. Phase 4: evidence enrichment. Add an enrich_diagnostics node that pulls targeted evidence for each anomaly, such as report files, discard logs, recent error messages, trail file backlog, target database reachability, credential store issues, filesystem usage breakdown, recent disk growth, and deployment events. Add a correlate_context node that attaches surrounding context such as recent releases, maintenance windows, known incidents, or dependent system failures.
6. Phase 5: assessment and decisioning. Add an assess_incident node that synthesizes the evidence into a concise operational summary: current health, suspected root cause, confidence, impact, and recommended next action. Add a review_decision node that serves the same purpose as the lesson’s reflection node, but checks whether the evidence is sufficient and chooses one of these outcomes: healthy, notify, investigate_more, escalate, or stop_due_to_error_budget.
7. Phase 6: action handling. Add a notify_node for operator or channel notifications when issues are clear but do not require full escalation. Add an escalate_node that creates or updates an incident, ticket, or handoff package when severity or uncertainty warrants it. Add a record_observation node that persists the latest monitoring result and timestamps for auditability and trend analysis.
8. Add bounded loop control. If review_decision returns investigate_more and investigation_round is still below the configured limit, route back to enrich_diagnostics and then reassess. If run_mode is continuous polling and the environment is healthy, route from record_observation to a wait_or_schedule_next_poll node and then back to collect_status until max_polls or an external scheduler stops the run. If run_mode is one-shot, end after record_observation when no further action is needed.
9. Add explicit failure handling. Route telemetry collection and enrichment failures into a handle_collection_error node that records the failure, decides whether partial data is acceptable, and either continues with degraded confidence or escalates immediately.

**Relevant files**
- d:\project\prj4\context\Lesson_6_Student.py — reuse the single TypedDict state pattern, node-per-function structure, conditional edge pattern after the main synthesis step, and bounded loop concept from plan_node, research_plan_node, generation_node, reflection_node, research_critique_node, and should_continue.

**Verification**
1. Validate the graph on three scenarios: all processes healthy, one Replicat abended, and one Extract with sustained lag above threshold.
2. Confirm each anomaly path produces the expected terminal route: healthy ends cleanly, medium-severity issues notify, and severe or low-confidence issues escalate.
3. Confirm monitor_plan emits the expected three rule groups: process_state_rules, lag_time_rules, and disk_usage_rules.
4. Confirm the investigation loop stops when max_investigation_rounds is reached and does not spin indefinitely on unresolved evidence gaps.
5. Confirm duplicate evidence is deduplicated or source-tagged so repeated polls do not bloat the state.
6. Confirm partial telemetry failures still produce a deterministic decision and include degraded-confidence markers.

**Decisions**
- Included scope: process health monitoring, lag monitoring, disk usage monitoring, anomaly detection, enrichment, assessment, notification, escalation, and optional polling loop.
- Excluded scope: automatic restart or remediation actions. Add those only after the read-only monitoring graph is stable.
- Assumption: GoldenGate data will come from GGSCI, Admin Client, REST API, or wrapper functions around those interfaces, and those integrations sit behind the collection/enrichment nodes rather than inside decision nodes.
- Recommendation: keep the graph read-only first; do not let the graph mutate GoldenGate state until alert quality and escalation logic are validated.

**Further Considerations**
1. Recommended node split: keep status collection separate from anomaly detection so the same snapshot can support both dashboards and incident triage.
2. Recommended branch point: make review_decision the main conditional router, not assess_incident, because operational routing depends on confidence and severity checks after synthesis.
3. Recommended persistence: replace in-memory checkpointing with a durable store before production use so investigations survive worker restarts.

**Proposed nodes**
1. intake
2. monitor_plan
3. discover_processes
4. collect_status
5. detect_anomalies
6. classify_severity
7. enrich_diagnostics
8. correlate_context
9. assess_incident
10. review_decision
11. notify_node
12. escalate_node
13. record_observation
14. wait_or_schedule_next_poll
15. handle_collection_error

**monitor_plan outputs**
1. process_state_rules: expected process states and routing rules for running, stopped, abended, or missing processes.
2. lag_time_rules: warning and critical lag thresholds, optionally by process type or process name.
3. disk_usage_rules: warning and critical filesystem thresholds for GoldenGate home, trail directories, and report or log locations.
4. escalation_policy: how state, lag, and disk findings should map to notify versus escalate decisions.

**Proposed edges**
1. intake -> monitor_plan
2. monitor_plan -> discover_processes
3. discover_processes -> collect_status
4. collect_status -> detect_anomalies
5. collect_status -> handle_collection_error when status collection fails
6. detect_anomalies -> classify_severity
7. classify_severity -> record_observation when no anomalies are found in one-shot mode
8. classify_severity -> wait_or_schedule_next_poll when no anomalies are found in continuous mode
9. classify_severity -> enrich_diagnostics when anomalies are found
10. enrich_diagnostics -> correlate_context
11. enrich_diagnostics -> handle_collection_error when diagnostic collection fails
12. correlate_context -> assess_incident
13. assess_incident -> review_decision
14. review_decision -> record_observation when decision is healthy
15. review_decision -> notify_node when decision is notify
16. review_decision -> escalate_node when decision is escalate
17. review_decision -> enrich_diagnostics when decision is investigate_more and investigation budget remains
18. review_decision -> escalate_node when decision is investigate_more but investigation budget is exhausted
19. notify_node -> record_observation
20. escalate_node -> record_observation
21. record_observation -> wait_or_schedule_next_poll in continuous mode
22. wait_or_schedule_next_poll -> collect_status
23. record_observation -> END in one-shot mode
24. handle_collection_error -> assess_incident when partial data is acceptable
25. handle_collection_error -> escalate_node when telemetry loss prevents reliable assessment
26. handle_collection_error -> END when policy says to fail fast

**Recommended minimal first version**
1. Start with intake, discover_processes, collect_status, detect_anomalies, enrich_diagnostics, assess_incident, review_decision, notify_node, escalate_node, and record_observation.
2. Add correlate_context, polling, and advanced error routing only after the first end-to-end path works.
3. Keep the first release focused on read-only monitoring and operator-facing output.