"""
End-to-end simulation: 2 sessions register into same room, exchange messages,
close thread. Runs handler functions directly (bypassing MCP transport).
"""
import os
import shutil
import tempfile
from pathlib import Path


def test_two_session_flow(tmp_path=None):
    if tmp_path is None:
        tmp = Path(tempfile.mkdtemp(prefix="xtalk-e2e-"))
        cleanup = True
    else:
        tmp = tmp_path
        cleanup = False
    os.environ["HOME"] = str(tmp)

    # Re-import with fresh HOME
    import importlib
    from xtalk import storage
    importlib.reload(storage)
    storage.XTALK_ROOT = tmp / ".claude" / "xtalk"

    from xtalk import server
    importlib.reload(server)
    # Rebind XTALK_ROOT reference the reloaded server holds
    server.storage.XTALK_ROOT = storage.XTALK_ROOT

    workspace = str(tmp / "work")
    os.makedirs(workspace, exist_ok=True)
    os.chdir(workspace)

    # --- Session A registers as 'coder' ---
    server.CTX = server.SessionCtx()
    r = server.handle_register({"alias": "coder"})
    sid_a = r["sid"]
    print("A registered:", sid_a, "other_members:", len(r["other_members"]))
    assert r["other_members"] == []

    # Discover from another perspective
    d = server.handle_discover({})
    assert len(d["members"]) == 1
    assert d["members"][0]["alias"] == "coder"

    # --- Session B registers as 'reviewer' (simulate 2nd session by swapping CTX) ---
    ctx_a = server.CTX
    server.CTX = server.SessionCtx()
    # We can't have 2 sids from same pid — override
    import xtalk.storage as st

    orig_session_id = st.session_id
    try:
        st.session_id = lambda: "sid-fakeB"
        server.storage.session_id = st.session_id  # keep aligned
        r_b = server.handle_register({"alias": "reviewer"})
        sid_b = r_b["sid"]
        ctx_b = server.CTX
        print("B registered:", sid_b, "sees other:", [m["alias"] for m in r_b["other_members"]])
        assert any(m["alias"] == "coder" for m in r_b["other_members"])

        # --- A asks B ---
        server.CTX = ctx_a
        ask = server.handle_ask({"to": "reviewer", "body": "Review giúp crypto.rs xem đúng không"})
        tid = ask["thread_id"]
        msg_ask = ask["msg_id"]
        print("A asked:", msg_ask, "wait_cmd contains msg_id:", msg_ask in ask["wait_command"])
        assert msg_ask in ask["wait_command"]

        # --- B reads inbox (simulated by reading thread) ---
        server.CTX = ctx_b
        read = server.handle_read({"thread": tid})
        assert len(read["messages"]) == 1
        assert read["messages"][0]["kind"] == "ask"
        assert read["messages"][0]["body"].startswith("Review")

        # --- B replies ---
        reply = server.handle_reply({
            "thread": tid,
            "body": "Đọc xong. Có 2 issue: nonce reuse ở line 42, và AAD thiếu type binding.",
            "in_reply_to": msg_ask,
        })
        print("B replied:", reply["msg_id"], "to:", reply["to"])
        assert reply["to"] == [sid_a]

        # --- A reads full thread ---
        server.CTX = ctx_a
        full = server.handle_read({"thread": tid, "count": 10})
        assert len(full["messages"]) == 2
        kinds = [m["kind"] for m in full["messages"]]
        assert kinds == ["ask", "reply"]
        print("A sees:", kinds)

        # --- A asks follow-up ---
        ask2 = server.handle_ask({
            "to": "reviewer",
            "body": "Line 42 cụ thể là biến nào?",
            "thread": tid,
        })
        assert ask2["thread_id"] == tid

        # --- B replies to follow-up ---
        server.CTX = ctx_b
        server.handle_reply({
            "thread": tid,
            "body": "Biến `page_nonce`, thiếu unique constraint.",
            "in_reply_to": ask2["msg_id"],
        })

        # --- B closes thread ---
        close = server.handle_close({
            "thread": tid,
            "summary": "2 issue xác nhận: nonce reuse + AAD missing type. Coder sẽ fix.",
            "report_to": "coder",
        })
        print("B closed, report_to:", close["report_to"])
        assert close["report_to"] == sid_a

        # --- A reads final state ---
        server.CTX = ctx_a
        final = server.handle_read({"thread": tid, "count": 100})
        assert len(final["messages"]) == 5
        assert final["messages"][-1]["kind"] == "done"
        print("Final thread:", [m["kind"] for m in final["messages"]])

        # --- Both leave ---
        server.CTX = ctx_b
        server.handle_leave({})
        server.CTX = ctx_a
        server.handle_leave({})

        # --- Discover shows empty ---
        server.CTX = server.SessionCtx()
        d2 = server.handle_discover({})
        assert d2["members"] == []
        print("After leave: room empty ✓")
    finally:
        # Cleanup
        st.session_id = orig_session_id
        if cleanup:
            shutil.rmtree(tmp, ignore_errors=True)
        print("\n=== ALL PASS ===")


if __name__ == "__main__":
    test_two_session_flow()
