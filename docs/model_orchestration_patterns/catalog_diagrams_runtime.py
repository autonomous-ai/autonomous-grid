"""Catalog diagrams for trust, owned-runtime, and sovereignty patterns."""

from build_diagrams import Diagram


def shadow_model():
    d = Diagram("Shadow Model", m_l=180)
    d.place("job", "live traffic", "terminal", row=1, stage=0)
    d.place("current", "current model", "work", row=0, stage=1)
    d.place("candidate", "candidate", "work", row=2, stage=1)
    d.place("rule", "evidence rule", "decide", row=1, stage=2)
    d.place("answer", "live answer", "terminal", row=0, stage=3)
    d.place("promote", "promote candidate", "terminal", row=1, stage=3)
    d.place("reject", "reject", "terminal", row=2, stage=3)
    d.edge("job", "current", "serves")
    d.edge("job", "candidate", "read-only shadow", dashed=True)
    d.edge("current", "answer")
    d.edge("current", "rule", "baseline")
    d.edge("candidate", "rule", "observed evidence")
    d.edge("rule", "promote", "pass")
    d.edge("rule", "reject", "fail")
    return d


def model_audition():
    d = Diagram("Model Audition", m_l=180)
    d.place("candidate", "candidate model", "terminal", row=0, stage=0)
    d.place("pack", "private task pack", "work", row=2, stage=0)
    d.place("test", "run audition", "work", row=1, stage=1)
    d.place("fit", "role fit?", "decide", row=1, stage=2)
    d.place("role", "assign role", "terminal", row=0, stage=3)
    d.place("reject", "reject", "terminal", row=2, stage=3)
    d.edge("candidate", "test")
    d.edge("pack", "test", "offline cases")
    d.edge("test", "fit")
    d.edge("fit", "role", "proves fit")
    d.edge("fit", "reject", "fails")
    return d


def night_shift():
    d = Diagram("Night Shift", m_l=180)
    d.place("change", "staged change", "work", row=2, stage=0)
    d.place("proof", "independent proof", "decide", row=2, stage=1)
    d.place("promote", "promote", "work", row=1, stage=2)
    d.place("discard", "discard", "terminal", row=3, stage=2)
    d.place("live", "live state", "terminal", row=0, stage=3)
    d.edge("change", "proof")
    d.edge("proof", "promote", "pass")
    d.edge("proof", "discard", "fail")
    d.edge("promote", "live", "atomic replace")
    return d


def pinned_model():
    d = Diagram("Pinned Model")
    d.place("role", "role", "terminal", row=1, stage=0)
    d.place("pin", "pinned bundle ID", "decide", row=1, stage=1)
    d.place("build", "exact build", "work", row=1, stage=2)
    d.place("run", "run", "terminal", row=1, stage=3)
    d.edge("role", "pin")
    d.edge("pin", "build", "immutable match")
    d.edge("build", "run")
    return d


def fit_the_box():
    d = Diagram("Fit the Box", m_l=180)
    d.place("recipe", "recipe", "terminal", row=0, stage=0)
    d.place("memory", "free memory", "work", row=2, stage=0)
    d.place("fit", "fits now?", "decide", row=1, stage=1)
    d.place("run", "run", "terminal", row=0, stage=2)
    d.place("shrink", "shrink recipe", "work", row=1, stage=2)
    d.place("wait", "wait", "terminal", row=2, stage=2)
    d.edge("recipe", "fit")
    d.edge("memory", "fit")
    d.edge("fit", "run", "yes")
    d.edge("fit", "shrink", "try smaller")
    d.edge("fit", "wait", "none fits")
    return d


def keep_it_warm():
    d = Diagram("Keep It Warm", m_l=180)
    d.place("demand", "measured demand", "work", row=0, stage=0)
    d.place("memory", "available memory", "work", row=2, stage=0)
    d.place("choose", "choose hot set", "decide", row=1, stage=1)
    d.place("warm", "resident models", "work", row=0, stage=2)
    d.place("cold", "load on demand", "work", row=2, stage=2)
    d.edge("demand", "choose")
    d.edge("memory", "choose")
    d.edge("choose", "warm", "hot")
    d.edge("choose", "cold", "others")
    return d


def idle_worker():
    d = Diagram("Idle Worker", m_l=180)
    d.place("backlog", "background backlog", "terminal", row=2, stage=0)
    d.place("idle", "idle?", "decide", row=1, stage=1)
    d.place("quantum", "bounded quantum", "work", row=1, stage=2)
    d.place("live", "live work", "terminal", row=0, stage=3)
    d.place("checkpoint", "checkpoint", "terminal", row=2, stage=3)
    d.edge("backlog", "idle")
    d.edge("idle", "quantum", "yes")
    d.edge("idle", "live", "busy")
    d.edge("quantum", "checkpoint", "quantum ends")
    d.edge("quantum", "live", "live arrival: yield", dashed=True)
    return d


def power_budget():
    d = Diagram("Power Budget", m_l=180)
    d.place("job", "job", "terminal", row=0, stage=0)
    d.place("signals", "power + heat limit", "work", row=2, stage=0)
    d.place("limit", "inside envelope?", "decide", row=1, stage=1)
    d.place("full", "run", "work", row=0, stage=2)
    d.place("reduce", "reduce work", "work", row=1, stage=2)
    d.place("defer", "defer", "terminal", row=2, stage=2)
    d.edge("job", "limit")
    d.edge("signals", "limit")
    d.edge("limit", "full", "inside")
    d.edge("limit", "reduce", "near limit")
    d.edge("limit", "defer", "at limit")
    return d


def straggler_backup():
    d = Diagram("Straggler Backup", m_l=180)
    d.place("job", "parallel job", "terminal", row=1, stage=0)
    d.place("primary", "original lane", "work", row=0, stage=1)
    d.place("late", "overdue?", "decide", row=1, stage=2)
    d.place("backup", "backup lane", "work", row=2, stage=3)
    d.place("result", "first valid result", "terminal", row=1, stage=4)
    d.edge("job", "primary")
    d.edge("primary", "result")
    d.edge("primary", "late", dashed=True)
    d.edge("late", "backup")
    d.edge("backup", "result")
    return d


def circuit_breaker():
    d = Diagram("Circuit Breaker — stop a failing route and probe recovery",
                m_l=220)
    d.place("failures", "repeated failures", "terminal", row=1, stage=0)
    d.place("breaker", "open breaker", "decide", row=1, stage=1)
    d.place("fallback", "safe fallback", "work", row=0, stage=2)
    d.place("probe", "later probe", "work", row=2, stage=2)
    d.place("answer", "fallback answer", "terminal", row=0, stage=3)
    d.place("state", "reopen / stay open", "terminal", row=2, stage=3)
    d.edge("failures", "breaker")
    d.edge("breaker", "fallback")
    d.edge("breaker", "probe", dashed=True)
    d.edge("fallback", "answer")
    d.edge("probe", "state")
    return d


def local_cascade():
    d = Diagram("Local Cascade", m_l=180)
    d.place("request", "request", "terminal", row=1, stage=0)
    d.place("local", "local attempt", "work", row=1, stage=1)
    d.place("enough", "enough?", "decide", row=1, stage=2)
    d.place("gate", "policy gate", "decide", row=2, stage=3)
    d.place("remote", "remote attempt", "work", row=2, stage=4)
    d.place("defer", "defer", "terminal", row=3, stage=4)
    d.place("answer", "answer", "terminal", row=0, stage=5)
    d.edge("request", "local")
    d.edge("local", "enough")
    d.edge("enough", "answer")
    d.edge("enough", "gate")
    d.edge("gate", "remote")
    d.edge("gate", "defer")
    d.edge("remote", "answer")
    return d


def data_stays_put():
    d = Diagram("Data Stays Put", m_l=220)
    d.place("query", "query", "terminal", row=0, stage=0)
    d.place("data", "raw data stays here", "work", row=2, stage=0)
    d.place("infer", "inference at data node", "work", row=1, stage=1)
    d.place("result", "derived result", "terminal", row=0, stage=2)
    d.edge("query", "infer")
    d.edge("data", "infer", "local read")
    d.edge("infer", "result", "minimum result only")
    return d


def privacy_boundary():
    d = Diagram("Privacy Boundary", m_l=180)
    d.place("data", "sensitive data", "terminal", row=1, stage=0)
    d.place("classify", "label use", "decide", row=1, stage=1)
    d.place("local", "local path", "work", row=0, stage=2)
    d.place("gate", "policy gate", "decide", row=2, stage=2)
    d.place("external", "external use", "work", row=2, stage=3)
    d.place("deny", "defer / refuse", "terminal", row=3, stage=3)
    d.place("answer", "answer", "terminal", row=0, stage=4)
    d.edge("data", "classify")
    d.edge("classify", "local")
    d.edge("classify", "gate")
    d.edge("gate", "external")
    d.edge("gate", "deny")
    d.edge("local", "answer")
    d.edge("external", "answer")
    return d


def offline_island():
    d = Diagram("Offline Island", m_l=180)
    d.place("offline", "network absent", "terminal", row=1, stage=0)
    d.place("fan", "", "dot", row=1, stage=1)
    d.place("models", "pinned models", "work", row=0, stage=2)
    d.place("tools", "local tools", "work", row=1, stage=2)
    d.place("data", "local data", "work", row=2, stage=2)
    d.place("path", "complete local path", "work", row=1, stage=3)
    d.place("continue", "continue usefully", "terminal", row=1, stage=4)
    d.edge("offline", "fan")
    d.edge("fan", "models")
    d.edge("fan", "tools")
    d.edge("fan", "data")
    d.edge("models", "path")
    d.edge("tools", "path")
    d.edge("data", "path")
    d.edge("path", "continue")
    return d


def private_memory():
    d = Diagram("Private Memory", m_l=200)
    d.place("history", "local private history", "work", row=0, stage=0)
    d.place("scope", "person + purpose", "work", row=2, stage=0)
    d.place("retrieve", "scoped retrieval", "decide", row=1, stage=1)
    d.place("model", "model", "work", row=1, stage=2,
            sub="minimum context only", w=330)
    d.place("answer", "answer", "terminal", row=1, stage=3)
    d.edge("history", "retrieve")
    d.edge("scope", "retrieve")
    d.edge("retrieve", "model")
    d.edge("model", "answer")
    return d


RUNTIME_BUILDERS = {
    "shadow_model": shadow_model,
    "model_audition": model_audition,
    "night_shift": night_shift,
    "pinned_model": pinned_model,
    "fit_the_box": fit_the_box,
    "keep_it_warm": keep_it_warm,
    "idle_worker": idle_worker,
    "power_budget": power_budget,
    "straggler_backup": straggler_backup,
    "circuit_breaker": circuit_breaker,
    "local_cascade": local_cascade,
    "data_stays_put": data_stays_put,
    "privacy_boundary": privacy_boundary,
    "offline_island": offline_island,
    "private_memory": private_memory,
}
