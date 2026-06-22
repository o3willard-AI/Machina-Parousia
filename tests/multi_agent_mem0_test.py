#!/usr/bin/env python3
"""
Section 8: Multi-Agent Mem0 Cross-Agent Verification
Runs on Parousia AWS (32.197.57.145) with /opt/parousia/venv/bin/python3
"""
import json, sys, time, subprocess, os, threading

sys.path.insert(0, "/opt/parousia/src")

from parousia.memory.config import Mem0Config
from parousia.memory.recorder import MemoryRecorder

config = Mem0Config.from_file("/etc/parousia/mem0.yaml")
recorder = MemoryRecorder(config)
recorder._consecutive_failures = 0
recorder._breaker_open_until = 0.0

# Recorder adds "parousia-" prefix — pass bare names
AGENTS = ["hermes", "claude", "openclaw"]
PASS = 0; FAIL = 0

def ok(desc, cond, detail=""):
    global PASS, FAIL
    if cond: PASS += 1; print(f"  ✓ {desc}")
    else: FAIL += 1; print(f"  ✗ {desc}  {detail}")

def wait_writes(secs=2):
    """Let daemon Mem0 write threads finish."""
    time.sleep(secs)

def qdrant_scroll(limit=50):
    r = subprocess.run(["curl", "-s", "--max-time", "5",
        "http://localhost:6333/collections/mem0/points/scroll",
        "-H", "Content-Type: application/json",
        "-d", json.dumps({"limit": limit, "with_payload": True})],
        capture_output=True, text=True, timeout=10)
    try: return json.loads(r.stdout)
    except: return {"error": r.stdout[:200]}

def qdrant_search(text, user_id=None):
    """Scroll all points and text-match."""
    result = qdrant_scroll(100)
    pts = result.get("result", {}).get("points", [])
    matches = []
    for p in pts:
        pl = p.get("payload", {})
        data = pl.get("data", "")
        uid = pl.get("user_id", "")
        if text.lower() in data.lower():
            if user_id is None or uid == user_id or uid.endswith(user_id):
                matches.append({"user_id": uid, "data": data[:200]})
    return matches

def qdrant_agents():
    result = qdrant_scroll(100)
    pts = result.get("result", {}).get("points", [])
    return set(p.get("payload", {}).get("user_id", "") for p in pts)

def record(tool, args, res, agent):
    recorder.record_tool_call(tool, args, res, AGENTS[agent] if isinstance(agent, int) else agent)

# ═══════════════════════════════════════════════════
print("=" * 60)
print("SECTION 8: MULTI-AGENT MEM0 VERIFICATION")
print("=" * 60)

# Phase 0
print("\n── Phase 0: Connectivity ──")
qd = qdrant_scroll(1)
ok("Qdrant reachable", "error" not in qd)
ok("Mem0 collection exists", qd.get("result") is not None)
ok("MemoryRecorder initialized", recorder is not None)
baseline = len(qd.get("result", {}).get("points", []))
print(f"  Baseline: {baseline} points")

# Scenario 1: Cross-Agent Temporal Awareness
print("\n── Scenario 1: Cross-Agent Temporal Awareness ──")
record("schedule_event",
    {"title": "Deploy v0.3.0 to production", "start_time": "2026-06-27T14:00:00Z", "flexibility": "medium"},
    {"scheduled": True, "event_id": "evt_deploy_v030", "title": "Deploy v0.3.0 to production", "conflicts": []}, 0)
wait_writes()

own = qdrant_search("Deploy v0.3.0", user_id=AGENTS[0])
ok("Hermes finds own scheduled event", len(own) >= 1, f"found {len(own)}")

cross = qdrant_search("Deploy v0.3.0")
ok("Cross-search finds Hermes facts", len(cross) >= 1, f"found {len(cross)}")
ok("Cross includes Hermes user_id", any(AGENTS[0] in f["user_id"] for f in cross))

record("schedule_event",
    {"title": "Post-deploy monitoring window", "start_time": "2026-06-27T14:15:00Z"},
    {"scheduled": True, "event_id": "evt_monitor_1", "title": "Post-deploy monitoring window", "conflicts": []}, 1)
wait_writes()

both = qdrant_search("monitoring window")
ok("Cross finds Claude monitoring", len(both) >= 1, f"found {len(both)}")
ok("Cross includes Claude user_id", any(AGENTS[1] in f["user_id"] for f in both))

# Scenario 2: Email + Event Synthesis
print("\n── Scenario 2: Email + Event Synthesis ──")
record("send_email",
    {"to": "team@example.com", "subject": "v0.3.0 rollout plan", "body": "Rolling out Wednesday..."},
    {"sent": True, "message_id": "msg_test_001"}, 1)
record("schedule_event",
    {"title": "v0.3.0 rollout", "start_time": "2026-06-25T09:00:00Z"},
    {"scheduled": True, "event_id": "evt_rollout", "title": "v0.3.0 rollout", "conflicts": []}, 0)
wait_writes()

rollout = qdrant_search("rollout")
ok("Cross-search finds rollout facts", len(rollout) >= 2, f"found {len(rollout)}")
agents_s2 = set(f["user_id"] for f in rollout)
ok("Rollout facts from both agents", len(agents_s2) >= 2, f"agents: {agents_s2}")

email_f = qdrant_search("rollout plan")
ok("Claude email fact recorded", len(email_f) >= 1, f"found {len(email_f)}")

# Scenario 3: Spatial Discovery Chain
print("\n── Scenario 3: Spatial Discovery Chain ──")
record("browse_to",
    {"url": "https://github.com/NousResearch/hermes-agent/releases"},
    {"url": "https://github.com/NousResearch/hermes-agent/releases", "extracted": True,
     "sdom": "<sdom>Hermes Agent v2.0.0 release notes...</sdom>"}, 2)
wait_writes()

browse = qdrant_search("hermes-agent")
ok("Cross finds OpenClaw browse", len(browse) >= 1, f"found {len(browse)}")
ok("Browse attributed to OpenClaw", any(AGENTS[2] in f["user_id"] for f in browse))

record("nominate_milestone",
    {"title": "Hermes Agent latest release noted", "entry_type": "discovery", "occurred_at": "2026-06-22"},
    {"recorded": True, "journal_id": "jnl_001", "title": "Hermes Agent latest release noted"}, 1)
wait_writes()

timeline = qdrant_search("release noted")
ok("Timeline has discovery facts", len(timeline) >= 1, f"found {len(timeline)}")
agents_s3 = set(f["user_id"] for f in timeline)
ok("Timeline from ≥2 agents", len(agents_s3) >= 2, f"agents: {agents_s3}")

# Scenario 4: Multi-Agent Timeline Reconstruction
s4_before = len(qdrant_scroll(200).get("result", {}).get("points", []))
print("\n── Scenario 4: Multi-Agent Timeline Reconstruction ──")
record("set_timer_alarm",
    {"title": "QA review deadline", "trigger_at": "2026-06-23T17:00:00Z"},
    {"set": True, "alarm_id": "alarm_qa", "type": "alarm", "remaining_seconds": 82800}, 0)
record("nominate_milestone",
    {"title": "Mem0 memory layer v0.3.0", "entry_type": "release", "occurred_at": "2026-06-22"},
    {"recorded": True, "journal_id": "jnl_002", "title": "Mem0 memory layer v0.3.0"}, 1)
record("browse_to",
    {"url": "https://docs.mem0.ai"},
    {"url": "https://docs.mem0.ai", "extracted": True, "sdom": "<sdom>Mem0 docs...</sdom>"}, 2)
record("resolve_conflicts", {},
    {"resolved": True, "conflicts_found": 0, "resolutions": []}, 0)
wait_writes(3)

# Count all facts — Scenario 4 writes should have increased count
s4_count = len(qdrant_scroll(200).get("result", {}).get("points", []))
ok("Scenario 4 produced ≥4 new facts", s4_count - s4_before >= 4, f"Δ={s4_count - s4_before}")
s4_agents = qdrant_agents()
ok("Facts from ≥2 agents", len(s4_agents) >= 2, f"agents: {sorted(s4_agents)}")
ok("Facts from all 3 agents", len(s4_agents) >= 3, f"agents: {sorted(s4_agents)}")

# Scenario 5: Circuit Breaker Resilience
print("\n── Scenario 5: Circuit Breaker Resilience ──")

# 5.1 Baseline
b_before = len(qdrant_scroll(200).get("result", {}).get("points", []))
for i in range(3):
    record("schedule_event",
        {"title": f"Baseline {i+1}", "start_time": f"2026-06-28T0{i+1}:00:00Z"},
        {"scheduled": True, "event_id": f"b{i+1}"}, 0)
wait_writes(2)
b_after = len(qdrant_scroll(200).get("result", {}).get("points", []))
ok("Baseline events recorded", b_after > b_before, f"{b_before}→{b_after}")

# 5.2 Stop Qdrant
print("  Stopping Qdrant...")
subprocess.run(["sudo", "systemctl", "stop", "qdrant"], capture_output=True, timeout=10)
time.sleep(2)
hc = subprocess.run(["curl", "-s", "--max-time", "2", "http://localhost:6333/healthz"],
    capture_output=True, text=True)
ok("Qdrant stopped", hc.returncode != 0 or "healthz check passed" not in hc.stdout)

# Reset breaker from any pre-existing failures
recorder._consecutive_failures = 0
recorder._breaker_open_until = 0.0

# 5.3 6 tool calls during outage
print("  Sending 6 tool calls during Qdrant outage...")
for i in range(6):
    start = time.time()
    record("schedule_event",
        {"title": f"Outage {i+1}", "start_time": f"2026-06-28T1{i}:00:00Z"},
        {"scheduled": True, "event_id": f"out_{i+1}"}, 0)
    elapsed = time.time() - start
    ok(f"Call {i+1} <1s (fire-and-forget)", elapsed < 1.0, f"{elapsed:.2f}s")

# 5.4 Check breaker
wait_writes(1)  # Let threads fail
ok("Circuit breaker tripped", recorder._consecutive_failures >= 5,
    f"failures={recorder._consecutive_failures}, open_until={recorder._breaker_open_until}")

# 5.5 Post-breaker call
start = time.time()
record("schedule_event",
    {"title": "Post-breaker event", "start_time": "2026-06-28T20:00:00Z"},
    {"scheduled": True, "event_id": "post_breaker"}, 0)
elapsed = time.time() - start
ok("Post-breaker <1s (skipped)", elapsed < 1.0, f"{elapsed:.2f}s")

# 5.6 Restart Qdrant
print("  Restarting Qdrant...")
subprocess.run(["sudo", "systemctl", "start", "qdrant"], capture_output=True, timeout=10)
time.sleep(3)
hc2 = subprocess.run(["curl", "-s", "--max-time", "5", "http://localhost:6333/healthz"],
    capture_output=True, text=True)
ok("Qdrant restarted", "healthz check passed" in hc2.stdout, hc2.stdout[:100])

# 5.7 Reset breaker for recovery test
recorder._breaker_open_until = 0.0
recorder._consecutive_failures = 0

# 5.8 Post-recovery write
record("schedule_event",
    {"title": "Post-recovery event", "start_time": "2026-06-29T10:00:00Z"},
    {"scheduled": True, "event_id": "recovery_1"}, 0)
wait_writes(2)

recov = qdrant_search("Post-recovery event")
ok("Post-recovery fact recorded", len(recov) >= 1, f"found {len(recov)}")

# Phase 5: Regression
print("\n── Phase 5: Tool Regression ──")
for tool in ["schedule_event", "nominate_milestone", "set_timer_alarm", "browse_to"]:
    try:
        record(tool,
            {"title": f"Reg {tool}", "start_time": "2026-07-01T00:00:00Z",
             "occurred_at": "2026-07-01", "url": "https://example.com",
             "trigger_at": "2026-07-01T00:00:00Z"},
            {"success": True}, 0)
        ok(f"{tool} dispatches clean", True)
    except Exception as e:
        ok(f"{tool} dispatches clean", False, str(e))

# Final
print("\n" + "=" * 60)
print(f"RESULTS: {PASS} passed, {FAIL} failed")
print("=" * 60)

final_agents = qdrant_agents()
final_count = len(qdrant_scroll(100).get("result", {}).get("points", []))
print(f"Qdrant: {final_count} facts from {len(final_agents)} agents: {sorted(final_agents)}")

if FAIL == 0:
    print("\n✓ MULTI-AGENT VERIFICATION: PASSED")
    sys.exit(0)
else:
    print(f"\n✗ MULTI-AGENT VERIFICATION: FAILED ({FAIL} failures)")
    sys.exit(1)
