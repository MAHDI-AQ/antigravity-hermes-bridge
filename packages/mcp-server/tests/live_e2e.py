"""Live MCP stdio + HTTP + SSE + board_cli + observer contract E2E verification in an isolated temp dir."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import httpx
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import store


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "e2e@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "E2E"], cwd=str(repo_dir), check=True)
    (repo_dir / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(repo_dir),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _assert_observer_contract(snap: dict, expected_swarm: str, *, expected_member: str | None = None) -> None:
    assert "swarms" not in snap, f"Unexpected multi-swarm 'swarms' key in 1:1 snapshot: {list(snap.keys())}"
    for key in (
        "swarm",
        "id",
        "title",
        "dir",
        "etag",
        "head",
        "meta",
        "plan",
        "channels",
        "members",
        "messages",
        "frames",
        "raw",
        "builtinOverview",
        "graphTab",
        "rawTab",
    ):
        assert key in snap, f"Missing observer key: {key}"
    assert snap["swarm"] == expected_swarm
    assert snap["id"] == expected_swarm
    if expected_member is not None:
        assert snap.get("member") == expected_member, f"Expected member={expected_member!r}, got {snap.get('member')!r}"
    assert isinstance(snap["meta"], dict)
    for mkey in ("swarmId", "title", "goal", "mission", "workspace", "primaryConversationId", "coordinatorConversationId"):
        assert mkey in snap["meta"], f"Missing meta key: {mkey}"
    assert isinstance(snap["raw"], dict)
    for rkey in ("board", "meta", "members", "view"):
        assert rkey in snap["raw"], f"Missing raw key: {rkey}"
    assert isinstance(snap["members"], list) and len(snap["members"]) >= 1
    for mem in snap["members"]:
        for mk in ("name", "role", "status", "killed", "wait_since", "last_read_seq", "unread", "posts", "steps", "channels", "subagents"):
            assert mk in mem, f"Missing member key: {mk}"
    assert isinstance(snap["messages"], list) and len(snap["messages"]) >= 1
    for msg in snap["messages"]:
        for msg_k in ("seq", "agent", "sender", "type", "text", "time", "ts", "mentions", "all"):
            assert msg_k in msg, f"Missing message key: {msg_k}"
    assert isinstance(snap["frames"], list) and len(snap["frames"]) >= 1
    for fr in snap["frames"]:
        for fk in ("path", "seq", "caption"):
            assert fk in fr, f"Missing frame key: {fk}"


async def run_e2e() -> None:
    root = Path(__file__).resolve().parent.parent
    py = sys.executable
    srv = str(root / "server.py")
    cli = str(root / "board_cli.py")

    with tempfile.TemporaryDirectory(prefix="hermes-v2-e2e-") as tmp:
        tmp_path = Path(tmp)
        temp_repo = tmp_path / "sample-git-project"
        temp_state = tmp_path / "state"
        _init_temp_git_repo(temp_repo)

        async def _list_roots(_context: object) -> types.ListRootsResult:
            return types.ListRootsResult(
                roots=[types.Root(uri=temp_repo.as_uri(), name="sample-git-project")]
            )

        env = dict(os.environ)
        env["HERMES_A2A_STATE_DIR"] = str(temp_state)
        env["HERMES_A2A_HTTP_PORT"] = "0"
        env.pop("HERMES_A2A_HTTP_URL", None)
        env.pop("HERMES_A2A_SWARM_ID", None)

        params = StdioServerParameters(command=py, args=[srv, "--with-http", "--port", "0"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, list_roots_callback=_list_roots) as session:
                init = await session.initialize()
                print("1. INIT OK:", init.server_info.name, init.server_info.version)

                # Verify exact 7-tool surface
                tools = await session.list_tools()
                tool_names = {t.name for t in tools.tools}
                expected_tools = {
                    "swarm_catalog",
                    "swarm_workspace",
                    "swarm_manage",
                    "swarm_worker",
                    "swarm_board_post",
                    "swarm_board_read",
                    "swarm_wait",
                }
                assert tool_names == expected_tools, f"Unexpected tools: {tool_names}"
                print("2. EXACT 7 TOOLS VERIFIED:", sorted(tool_names))

                # Verify active workspace from MCP roots
                wd_info = json.loads(
                    (await session.call_tool("swarm_workspace", {"action": "get"})).content[0].text
                )
                assert wd_info["source"] == "mcp_roots"
                assert Path(wd_info["workdir"]) == temp_repo.resolve()
                print("3. WORKSPACE MCP ROOTS OK:", wd_info["workdir"])

                # Discover Hermes catalog (live CLI if installed)
                cat = json.loads(
                    (await session.call_tool("swarm_catalog", {})).content[0].text
                )
                assert "providers" in cat and "profiles" in cat
                base_url = cat.get("http_url")
                assert base_url and base_url.startswith("http://"), f"Missing embedded HTTP URL: {cat}"
                hermes_installed = store.default_hermes_exe().is_file()
                if hermes_installed:
                    assert cat.get("hermes_version"), "Expected live hermes_version when hermes CLI is installed"
                print("4. CATALOG + HTTP URL OK:", base_url, "hermes_version=", cat.get("hermes_version"))

                prim_conv = "conv-primary-e2e"
                coord_conv = "conv-coord-e2e"
                other_conv = "conv-other-e2e"
                sid = prim_conv

                # Pre-swarm checks: /board/swarms is 404, unbound /board returns swarm: null,
                # and SSE /board/events?conversationId=<conv> connects cleanly BEFORE swarm exists.
                async with httpx.AsyncClient(timeout=10.0) as client:
                    assert (await client.get(f"{base_url}/board/swarms")).status_code == 404
                    r_no_conv = await client.get(f"{base_url}/board")
                    assert r_no_conv.status_code == 200
                    assert r_no_conv.json() == {"conversationId": None, "swarm": None, "member": None}
                    r_pre_conv = await client.get(f"{base_url}/board?conversationId={prim_conv}")
                    assert r_pre_conv.status_code == 200
                    assert r_pre_conv.json() == {"conversationId": prim_conv, "swarm": None, "member": None}

                sse_events: list[tuple[str, dict]] = []
                sse_null_ready = asyncio.Event()
                sse_swarm_ready = asyncio.Event()
                sse_done = asyncio.Event()

                async def _consume_sse() -> None:
                    async with httpx.AsyncClient(timeout=20.0) as client:
                        async with client.stream("GET", f"{base_url}/board/events?conversationId={prim_conv}") as resp:
                            assert resp.status_code == 200
                            cur_event = ""
                            async for line in resp.aiter_lines():
                                if line.startswith("event: "):
                                    cur_event = line[7:].strip()
                                elif line.startswith("data: "):
                                    payload = json.loads(line[6:])
                                    sse_events.append((cur_event, payload))
                                    if cur_event == "snapshot" and payload.get("swarm") is None:
                                        sse_null_ready.set()
                                    elif cur_event == "snapshot" and payload.get("swarm") == sid:
                                        sse_swarm_ready.set()
                                    if cur_event == "primary_wake":
                                        sse_done.set()
                                        return

                sse_task = asyncio.create_task(_consume_sse())
                await asyncio.wait_for(sse_null_ready.wait(), timeout=8.0)

                # Initialize 1:1 swarm (without explicit swarm_id -> derives from primary_conversation_id)
                created = json.loads(
                    (
                        await session.call_tool(
                            "swarm_manage",
                            {
                                "action": "init",
                                "title": "E2E 1:1 Swarm",
                                "mission": "Verify 1:1 session-bound HTTP, SSE, board_cli, observer contract, and frames",
                                "workspace": str(temp_repo),
                                "primary_conversation_id": prim_conv,
                            },
                        )
                    ).content[0].text
                )
                assert created["swarm_id"] == sid, f"Expected derived swarm_id={sid!r}, got {created!r}"
                assert "sidecar://hermes-swarm/swarm_board/" in created["open_board"]

                await asyncio.wait_for(sse_swarm_ready.wait(), timeout=8.0)

                renamed = json.loads(
                    (
                        await session.call_tool(
                            "swarm_manage",
                            {
                                "action": "title",
                                "title": "E2E 1:1 Swarm Renamed",
                                "primary_conversation_id": prim_conv,
                            },
                        )
                    ).content[0].text
                )
                assert renamed["meta"]["title"] == "E2E 1:1 Swarm Renamed"

                bound = json.loads(
                    (
                        await session.call_tool(
                            "swarm_manage",
                            {
                                "action": "bind_coordinator",
                                "coordinator_conversation_id": coord_conv,
                            },
                        )
                    ).content[0].text
                )
                assert bound["coordinator_conversation_id"] == coord_conv

                await session.call_tool(
                    "swarm_manage",
                    {
                        "action": "set_plan",
                        "plan": {
                            "goal": {"title": "E2E 1:1 Swarm Renamed", "line": "Full stack check"},
                            "threads": [{"id": "t-1", "title": "Backend", "state": "active", "tasks": ["worker-e2e"]}],
                            "tasks": [],
                        },
                    },
                )
                await session.call_tool(
                    "swarm_manage",
                    {
                        "action": "channel",
                        "op": "create",
                        "channel": "backend",
                        "topic": "Backend lane",
                        "members": ["worker-e2e"],
                    },
                )
                st_res = json.loads((await session.call_tool("swarm_manage", {"action": "status"})).content[0].text)
                assert st_res["swarm_id"] == sid
                cf_res = json.loads((await session.call_tool("swarm_manage", {"action": "clear_finished"})).content[0].text)
                assert cf_res["swarm_id"] == sid

                # Removed actions (select / archive) must fail cleanly
                sel_res = await session.call_tool("swarm_manage", {"action": "select", "swarm_id": sid})
                assert sel_res.is_error, "Expected swarm_manage(action='select') to be rejected"
                arch_res = await session.call_tool("swarm_manage", {"action": "archive", "swarm_id": sid})
                assert arch_res.is_error, "Expected swarm_manage(action='archive') to be rejected"
                print("5. 1:1 SWARM INIT / TITLE / BIND / PLAN / CHANNEL / REJECTS OK:", sid)

                # Prepare a valid PNG frame inside swarm_dir/frames, view.json, and an outside file for containment testing
                swarm_dir = temp_state / "swarms" / sid
                frames_dir = swarm_dir / "frames"
                frames_dir.mkdir(parents=True, exist_ok=True)
                (swarm_dir / "view.json").write_text(
                    json.dumps({"headline": "E2E View Active", "threads": []}),
                    encoding="utf-8",
                )
                frame_png = frames_dir / "e2e_shot.png"
                frame_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
                outside_png = tmp_path / "outside_secret.png"
                outside_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
                token_file = temp_state / "internal_token"
                assert token_file.is_file(), "Expected internal_token file created by EmbeddedA2AHttpController"

                # Post worker -> @coordinator. via board_cli.py using --conversation-id (also verifies trailing punctuation stripping)
                cli_proc1 = subprocess.run(
                    [
                        py,
                        cli,
                        "--board-dir",
                        str(swarm_dir),
                        "--conversation-id",
                        coord_conv,
                        "--agent",
                        "worker-e2e",
                        "--role",
                        "worker",
                        "--http-url",
                        base_url,
                        "--token-file",
                        str(token_file),
                        "post",
                        "--channel",
                        "backend",
                        "--kind",
                        "handoff",
                        "--summary",
                        "@coordinator. [done] CLI HTTP post + screenshot",
                        "--details",
                        f"Screenshot: {frame_png} | E2E rendered frame",
                    ],
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                )
                post1 = json.loads(cli_proc1.stdout)
                assert post1["seq"] >= 1
                assert post1["mentions"] == ["coordinator"], f"Trailing punctuation leaked into mentions: {post1['mentions']}"

                # Verify swarm_wait wakes up for coordinator
                wait_res = json.loads(
                    (
                        await session.call_tool(
                            "swarm_wait",
                            {
                                "conversation_id": coord_conv,
                                "since": post1["seq"] - 1,
                                "mentions_only": True,
                                "timeout_seconds": 5.0,
                            },
                        )
                    ).content[0].text
                )
                assert wait_res["woken"] is True
                assert wait_res["messages"][0]["seq"] == post1["seq"]

                # Post Coordinator -> @primary escalation via MCP swarm_board_post -> emits primary_wake on SSE
                await session.call_tool(
                    "swarm_board_post",
                    {
                        "conversation_id": coord_conv,
                        "sender": "coordinator",
                        "kind": "escalation",
                        "summary": "@primary Ready for final approval",
                    },
                )
                await asyncio.wait_for(sse_done.wait(), timeout=8.0)
                await sse_task

                event_names = [ev for ev, _ in sse_events]
                assert sse_events[0][0] == "snapshot" and sse_events[0][1]["swarm"] is None
                assert any(ev == "snapshot" and p.get("swarm") == sid for ev, p in sse_events)
                assert "update" in event_names
                assert "coordinator_wake" in event_names
                assert "primary_wake" in event_names
                print("6. PRE-SWARM SSE -> LIVE WAKEUPS + BOARD_CLI(--conversation-id) + SWARM_WAIT OK:", event_names)

                # Parallel second conversation sharing the SAME workspace must stay strictly isolated
                created_other = json.loads(
                    (
                        await session.call_tool(
                            "swarm_manage",
                            {
                                "action": "init",
                                "title": "E2E Other Session",
                                "workspace": str(temp_repo),
                                "primary_conversation_id": other_conv,
                            },
                        )
                    ).content[0].text
                )
                assert created_other["swarm_id"] == other_conv
                await session.call_tool(
                    "swarm_board_post",
                    {
                        "conversation_id": other_conv,
                        "sender": "worker-other",
                        "summary": "Isolated session 2 post",
                    },
                )

                # Verify HTTP observer contract, ETag caching, strict 1:1 isolation, frame containment, and registry hygiene
                async with httpx.AsyncClient(timeout=10.0) as client:
                    assert (await client.get(f"{base_url}/board/swarms?conversationId={coord_conv}")).status_code == 404

                    r_empty = await client.get(f"{base_url}/board?conversationId=conv-unscoped-none")
                    assert r_empty.status_code == 200
                    assert r_empty.json() == {"conversationId": "conv-unscoped-none", "swarm": None, "member": None}

                    # ?swarm= without conversationId must return swarm: null
                    r_swarm_only = await client.get(f"{base_url}/board?swarm={sid}")
                    assert r_swarm_only.status_code == 200
                    assert r_swarm_only.json() == {"conversationId": None, "swarm": None, "member": None}

                    # ?swarm= override ignored when conversationId is present; no leakage from other_conv
                    r_board = await client.get(f"{base_url}/board?conversationId={coord_conv}&swarm={other_conv}")
                    assert r_board.status_code == 200
                    snap = r_board.json()
                    _assert_observer_contract(snap, sid, expected_member="coordinator")
                    assert isinstance(snap["plan"]["tasks"], dict) and "worker-e2e" in snap["plan"]["tasks"]
                    assert snap["plan"]["tasks"]["worker-e2e"]["status"] == "queued"
                    assert snap["view"]["exists"] is True
                    assert snap["view"]["json"]["headline"] == "E2E View Active"
                    assert snap["narratorState"] == "absent"
                    assert not any(m["summary"] == "Isolated session 2 post" for m in snap["messages"])

                    r_other_board = await client.get(f"{base_url}/board?conversationId={other_conv}")
                    assert r_other_board.status_code == 200
                    other_snap = r_other_board.json()
                    assert other_snap["swarm"] == other_conv
                    assert "swarms" not in other_snap
                    assert [m["summary"] for m in other_snap["messages"]] == ["Isolated session 2 post"]

                    r_etag = await client.get(f"{base_url}/board?conversationId={coord_conv}&etag={snap['etag']}")
                    assert r_etag.status_code == 200
                    assert r_etag.json()["unchanged"] is True

                    r_frame_ok = await client.get(f"{base_url}/board/frame?conversationId={coord_conv}&path={frame_png}")
                    assert r_frame_ok.status_code == 200
                    assert r_frame_ok.headers["content-type"] == "image/png"

                    # Cross-conversation frame access blocked
                    r_frame_cross = await client.get(f"{base_url}/board/frame?conversationId={other_conv}&path={frame_png}")
                    assert r_frame_cross.status_code == 403

                    r_frame_trav = await client.get(f"{base_url}/board/frame?conversationId={coord_conv}&path=../outside_secret.png")
                    assert r_frame_trav.status_code == 403

                    r_frame_ws = await client.get(
                        f"{base_url}/board/frame?conversationId={coord_conv}&workspace={tmp_path}&path={outside_png}"
                    )
                    assert r_frame_ws.status_code == 403

                    r_post_reject = await client.post(f"{base_url}/board/post", json={"summary": "nope"})
                    assert r_post_reject.status_code in (404, 405)

                reg_data = store.get_registry(state_dir=temp_state)
                assert "activeSwarmId" not in reg_data, f"Unexpected activeSwarmId in registry: {reg_data}"

                print("7. 1:1 OBSERVER CONTRACT + ISOLATION + FRAME CONTAINMENT + READ-ONLY HTTP OK")

    await run_wakeup_all_hops_e2e()
    print("LIVE E2E PASSED ALL CHECKS")


def _setup_fake_hermes_home(hermes_home: Path) -> None:
    bin_dir = hermes_home / "bin"
    cli_dir = hermes_home / "hermes-agent" / "hermes_cli"
    bin_dir.mkdir(parents=True, exist_ok=True)
    cli_dir.mkdir(parents=True, exist_ok=True)

    (bin_dir / "hermes.cmd").write_text(f'@echo off\r\n"{sys.executable}" %*\r\n', encoding="utf-8")
    sh_bin = bin_dir / "hermes"
    sh_bin.write_text(f"#!/usr/bin/env sh\nexec \"{sys.executable}\" \"$@\"\n", encoding="utf-8")
    try:
        os.chmod(sh_bin, 0o755)
    except OSError:
        pass

    (hermes_home / "hermes-agent" / "hermes_bootstrap.py").write_text("# test bootstrap\n", encoding="utf-8")
    (hermes_home / "hermes-agent" / "hermes_constants.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "def get_default_hermes_root():\n"
        "    return Path(os.environ.get('HERMES_HOME', '') or '.')\n",
        encoding="utf-8",
    )
    (cli_dir / "__init__.py").write_text("# test package\n", encoding="utf-8")
    (cli_dir / "main.py").write_text(
        "\n".join([
            "import json, os, sys, time",
            "from pathlib import Path",
            "def main():",
            "    args = sys.argv[1:]",
            "    if args and args[0] == 'version':",
            "        print('hermes-test 0.1.0'); return",
            "    query_file = ''",
            "    resume_sid = ''",
            "    i = 0",
            "    while i < len(args):",
            "        if args[i] == '--query-file' and i + 1 < len(args):",
            "            query_file = args[i + 1]; i += 2; continue",
            "        if args[i] == '--resume' and i + 1 < len(args):",
            "            resume_sid = args[i + 1]; i += 2; continue",
            "        i += 1",
            "    prompt = Path(query_file).read_text(encoding='utf-8') if query_file and Path(query_file).is_file() else ''",
            "    wname = os.environ.get('HERMES_A2A_WORKER_NAME', 'worker')",
            "    trace_raw = os.environ.get('E2E_WORKER_TRACE_FILE', '')",
            "    swarm_env = {k: os.environ.get(k) for k in ('HERMES_SWARM_ID', 'HERMES_SWARM_AGENT', 'HERMES_SWARM_ROLE', 'HERMES_SWARM_BOARD_DIR', 'HERMES_SWARM_HTTP_URL', 'HERMES_SWARM_TOKEN_FILE', 'HERMES_SWARM_CONVERSATION_ID')}",
            "    if trace_raw:",
            "        with Path(trace_raw).open('a', encoding='utf-8') as fh:",
            "            fh.write(json.dumps({'worker': wname, 'resume': resume_sid, 'prompt': prompt, 'swarm_env': swarm_env}) + '\\n')",
            "    sid = resume_sid or f'e2e-hs-{wname}-1'",
            "    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'e2e-m', 'session_id': sid}), flush=True)",
            "    if '[TELEMETRY_BURST]' in prompt:",
            "        for idx in range(14):",
            "            tc = f'tc-e2e-{idx}'",
            "            print(json.dumps({'type': 'tool_use', 'name': 'grep_search', 'tool_call_id': tc, 'input': {'query': f'q{idx}'}}), flush=True)",
            "            print(json.dumps({'type': 'tool_result', 'name': 'grep_search', 'tool_call_id': tc, 'is_error': False, 'output': 'ok', 'duration_ms': 4}), flush=True)",
            "        print(json.dumps({'type': 'text', 'text': f'{wname}_TEL_OK'}), flush=True)",
            "        print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0, 'duration_ms': 1000, 'cost_usd': 0.01, 'text': f'{wname}_TEL_OK', 'tokens': {'input': 800, 'output': 200, 'total': 1000}}), flush=True)",
            "        return",
            "    print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'e2e-m', 'session_id': sid}), flush=True)",
            "    if '[BUSY_GATE]' in prompt and not resume_sid:",
            "        gate_raw = os.environ.get('E2E_BUSY_GATE_FILE', '')",
            "        if gate_raw:",
            "            gate = Path(gate_raw)",
            "            deadline = time.monotonic() + 12.0",
            "            while not gate.exists() and time.monotonic() < deadline:",
            "                time.sleep(0.02)",
            "    if '[DELEGATE_SUBAGENT]' in prompt:",
            "        print(json.dumps({'type': 'tool_use', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-e2e', 'input': {'goal': 'leaf e2e check', 'model': 'leaf-m'}}), flush=True)",
            "        print(json.dumps({'type': 'tool_result', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-e2e', 'is_error': False, 'output': 'LEAF_E2E_OK', 'duration_ms': 10}), flush=True)",
            "    if '[TRIGGER_INPUT_REQUIRED]' in prompt:",
            "        out = '[INPUT_REQUIRED] E2E input needed'",
            "        print(json.dumps({'type': 'text', 'text': out}), flush=True)",
            "        print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 3, 'text': out}), flush=True)",
            "        sys.exit(3)",
            "    if '[TRIGGER_FAILED]' in prompt:",
            "        print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 1, 'error': 'e2e simulated failure'}), flush=True)",
            "        sys.exit(1)",
            "    print(json.dumps({'type': 'text', 'text': f'{wname}_E2E_OK'}), flush=True)",
            "    print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0, 'text': f'{wname}_E2E_OK'}), flush=True)",
            "if __name__ == '__main__':",
            "    main()",
        ]),
        encoding="utf-8",
    )


async def run_wakeup_all_hops_e2e() -> None:
    root = Path(__file__).resolve().parent.parent
    py = sys.executable
    srv = str(root / "server.py")
    cli = str(root / "board_cli.py")

    with tempfile.TemporaryDirectory(prefix="hermes-v2-e2e-hops-") as tmp:
        tmp_path = Path(tmp)
        temp_repo = tmp_path / "repo"
        temp_state = tmp_path / "state"
        fake_hermes = tmp_path / "hermes_home"
        trace_file = tmp_path / "worker_trace.jsonl"
        busy_gate = tmp_path / "busy_gate.flag"
        keeper_gate = tmp_path / "keeper_gate.flag"

        _init_temp_git_repo(temp_repo)
        _setup_fake_hermes_home(fake_hermes)

        async def _list_roots(_context: object) -> types.ListRootsResult:
            return types.ListRootsResult(
                roots=[types.Root(uri=temp_repo.as_uri(), name="repo")]
            )

        wake_token = "e2e-wake-secret-99"
        env = dict(os.environ)
        env["HERMES_A2A_STATE_DIR"] = str(temp_state)
        env["HERMES_A2A_HTTP_PORT"] = "0"
        env["HERMES_A2A_HTTP_TOKEN"] = wake_token
        env["HERMES_HOME"] = str(fake_hermes)
        env["HERMES_EXE"] = str(fake_hermes / "bin" / ("hermes.cmd" if sys.platform == "win32" else "hermes"))
        env["E2E_WORKER_TRACE_FILE"] = str(trace_file)
        env["E2E_BUSY_GATE_FILE"] = str(busy_gate)
        env.pop("HERMES_A2A_HTTP_URL", None)
        env.pop("HERMES_A2A_SWARM_ID", None)

        def _read_trace() -> list[dict]:
            if not trace_file.is_file():
                return []
            return [json.loads(ln) for ln in trace_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

        params = StdioServerParameters(command=py, args=[srv, "--with-http", "--port", "0"], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write, list_roots_callback=_list_roots) as session:
                await session.initialize()
                cat = json.loads((await session.call_tool("swarm_catalog", {})).content[0].text)
                base_url = cat["http_url"]
                auth_hdr = {"Authorization": f"Bearer {wake_token}"}

                # Assertion 7: Endpoint security & 1:1 boundary before & after swarm creation
                async with httpx.AsyncClient(timeout=10.0) as hc:
                    assert (await hc.get(f"{base_url}/board/swarms", headers=auth_hdr)).status_code == 404
                    assert (await hc.get(f"{base_url}/board", headers=auth_hdr)).json() == {
                        "conversationId": None,
                        "swarm": None,
                        "member": None,
                    }
                    for ep in ("/internal/wake/swarms", "/internal/wake/events"):
                        assert (await hc.get(f"{base_url}{ep}")).status_code == 401
                        assert (await hc.get(f"{base_url}{ep}", headers={"Authorization": "Bearer wrong"})).status_code == 401
                        assert (
                            await hc.get(f"{base_url}{ep}", headers={"Host": "evil.example:9910", **auth_hdr})
                        ).status_code == 403
                    r_sw = await hc.get(f"{base_url}/internal/wake/swarms", headers=auth_hdr)
                    assert r_sw.status_code == 200 and r_sw.json() == {"swarms": []}

                prim_conv = "conv-e2e-h-p"
                coord_conv = "conv-e2e-h-c"

                wake_events: list[tuple[str, dict]] = []
                coord_board_events: list[tuple[str, dict]] = []
                prim_board_events: list[tuple[str, dict]] = []
                wake_ready = asyncio.Event()
                coord_null_ready = asyncio.Event()
                coord_swarm_ready = asyncio.Event()
                prim_null_ready = asyncio.Event()
                prim_swarm_ready = asyncio.Event()

                async def _consume_wake_sse() -> None:
                    async with httpx.AsyncClient(timeout=60.0) as hc:
                        async with hc.stream("GET", f"{base_url}/internal/wake/events", headers=auth_hdr) as resp:
                            assert resp.status_code == 200
                            ev = ""
                            async for ln in resp.aiter_lines():
                                if ln.startswith("event: "):
                                    ev = ln[7:].strip()
                                elif ln.startswith("data: "):
                                    wake_events.append((ev, json.loads(ln[6:])))
                                    if ev == "ready":
                                        wake_ready.set()

                async def _consume_board_sse(
                    conv: str,
                    out: list[tuple[str, dict]],
                    null_ev: asyncio.Event,
                    swarm_ev: asyncio.Event,
                ) -> None:
                    async with httpx.AsyncClient(timeout=60.0) as hc:
                        async with hc.stream("GET", f"{base_url}/board/events?conversationId={conv}", headers=auth_hdr) as resp:
                            assert resp.status_code == 200
                            ev = ""
                            async for ln in resp.aiter_lines():
                                if ln.startswith("event: "):
                                    ev = ln[7:].strip()
                                elif ln.startswith("data: "):
                                    payload = json.loads(ln[6:])
                                    out.append((ev, payload))
                                    if ev == "snapshot" and payload.get("swarm") is None:
                                        null_ev.set()
                                    elif ev == "snapshot" and payload.get("swarm") == prim_conv:
                                        swarm_ev.set()

                t_wake = asyncio.create_task(_consume_wake_sse())
                t_cboard = asyncio.create_task(_consume_board_sse(coord_conv, coord_board_events, coord_null_ready, coord_swarm_ready))
                t_pboard = asyncio.create_task(_consume_board_sse(prim_conv, prim_board_events, prim_null_ready, prim_swarm_ready))

                try:
                    await asyncio.wait_for(
                        asyncio.gather(wake_ready.wait(), coord_null_ready.wait(), prim_null_ready.wait()),
                        timeout=8.0,
                    )

                    # Create brand-new swarm AFTER SSE connect
                    await session.call_tool(
                        "swarm_manage",
                        {
                            "action": "init",
                            "title": "All Hops E2E",
                            "workspace": str(temp_repo),
                            "primary_conversation_id": prim_conv,
                        },
                    )
                    await session.call_tool(
                        "swarm_manage",
                        {"action": "bind_coordinator", "coordinator_conversation_id": coord_conv},
                    )
                    await session.call_tool(
                        "swarm_manage",
                        {"action": "channel", "op": "create", "channel": "backend", "topic": "Backend"},
                    )
                    await asyncio.wait_for(
                        asyncio.gather(coord_swarm_ready.wait(), prim_swarm_ready.wait()),
                        timeout=8.0,
                    )

                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        sw_list = (await hc.get(f"{base_url}/internal/wake/swarms", headers=auth_hdr)).json()["swarms"]
                        assert [s["swarmId"] for s in sw_list] == [prim_conv]

                    # Hop 1: Primary -> @coordinator
                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "primary", "summary": "@coordinator Begin all-hops check"},
                    )
                    for _ in range(60):
                        if any(
                            e == "coordinator_wake" and p.get("message", {}).get("summary") == "@coordinator Begin all-hops check"
                            for e, p in wake_events
                        ) and any(e == "coordinator_wake" for e, _ in coord_board_events):
                            break
                        await asyncio.sleep(0.05)
                    assert any(e == "coordinator_wake" for e, _ in coord_board_events), "Hop 1 missing on /board/events"
                    assert any(e == "coordinator_wake" for e, _ in wake_events), "Hop 1 missing on /internal/wake/events"
                    assert not any(e == "primary_wake" for e, _ in wake_events), "Hop 1 unexpectedly emitted primary_wake"

                    # Hop 2: Coordinator -> Hermes Worker (@worker, @#channel, @all, busy pendingMentions replay)
                    await session.call_tool(
                        "swarm_worker",
                        {
                            "action": "spawn",
                            "name": "w-alpha",
                            "prompt": "Boot alpha [BUSY_GATE]",
                            "channels": ["backend"],
                            "return_immediately": True,
                        },
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {
                            "action": "spawn",
                            "name": "w-beta",
                            "prompt": "Boot beta",
                            "channels": ["backend"],
                            "return_immediately": False,
                        },
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {
                            "action": "spawn",
                            "name": "w-ui",
                            "prompt": "Boot ui",
                            "channels": ["general"],
                            "return_immediately": False,
                        },
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {
                            "action": "spawn",
                            "name": "narrator",
                            "role": "narrator",
                            "prompt": "Boot narrator",
                            "return_immediately": False,
                        },
                    )
                    await asyncio.sleep(0.2)
                    # Queue @w-alpha while busy
                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "coordinator", "summary": "@w-alpha Queued while busy"},
                    )
                    await asyncio.sleep(0.2)
                    busy_gate.write_text("go", encoding="utf-8")
                    for _ in range(100):
                        tr = _read_trace()
                        if any(e["worker"] == "w-alpha" and e["resume"] and "Queued while busy" in e["prompt"] for e in tr):
                            break
                        await asyncio.sleep(0.05)
                    assert any(
                        e["worker"] == "w-alpha" and e["resume"] == "e2e-hs-w-alpha-1" and "Queued while busy" in e["prompt"]
                        for e in _read_trace()
                    ), "Hop 2 busy pendingMentions replay failed"

                    # Wait for w-alpha to become idle before channel & @all tests
                    for _ in range(60):
                        st_a = json.loads((await session.call_tool("swarm_worker", {"action": "status", "name": "w-alpha"})).content[0].text)
                        if st_a["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
                            break
                        await asyncio.sleep(0.05)

                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "coordinator", "summary": "Sync @#backend endpoints"},
                    )
                    for _ in range(80):
                        tr = _read_trace()
                        if (
                            any(e["worker"] == "w-alpha" and "Sync @#backend endpoints" in e["prompt"] for e in tr)
                            and any(e["worker"] == "w-beta" and "Sync @#backend endpoints" in e["prompt"] for e in tr)
                        ):
                            break
                        await asyncio.sleep(0.05)
                    tr_ch = _read_trace()
                    assert any(e["worker"] == "w-alpha" and "Sync @#backend endpoints" in e["prompt"] for e in tr_ch)
                    assert any(e["worker"] == "w-beta" and "Sync @#backend endpoints" in e["prompt"] for e in tr_ch)
                    assert not any(e["worker"] == "w-ui" and "Sync @#backend endpoints" in e["prompt"] for e in tr_ch)

                    for wname in ("w-alpha", "w-beta", "narrator"):
                        for _ in range(60):
                            st_w = json.loads((await session.call_tool("swarm_worker", {"action": "status", "name": wname})).content[0].text)
                            if st_w["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
                                break
                            await asyncio.sleep(0.05)

                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "coordinator", "summary": "@all E2E checkpoint"},
                    )
                    for _ in range(80):
                        got = {e["worker"] for e in _read_trace() if "@all E2E checkpoint" in e["prompt"]}
                        if got == {"w-alpha", "w-beta", "w-ui"}:
                            break
                        await asyncio.sleep(0.05)
                    tr_all = _read_trace()
                    assert {e["worker"] for e in tr_all if "@all E2E checkpoint" in e["prompt"]} == {"w-alpha", "w-beta", "w-ui"}
                    assert not any(e["worker"] == "narrator" and "@all E2E checkpoint" in e["prompt"] for e in tr_all)

                    for wname in ("w-alpha", "w-beta", "w-ui"):
                        for _ in range(60):
                            st_w = json.loads((await session.call_tool("swarm_worker", {"action": "status", "name": wname})).content[0].text)
                            if st_w["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
                                break
                            await asyncio.sleep(0.05)

                    # Hop 3: Worker <-> Worker via HTTP POST /internal/board/post AND disk-fallback board_cli.py
                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        r_hp = await hc.post(
                            f"{base_url}/internal/board/post",
                            headers=auth_hdr,
                            json={
                                "conversationId": coord_conv,
                                "sender": "w-alpha",
                                "kind": "handoff",
                                "summary": "@w-beta E2E peer HTTP wakeup",
                            },
                        )
                        assert r_hp.status_code == 200
                    for _ in range(80):
                        if any(e["worker"] == "w-beta" and "E2E peer HTTP wakeup" in e["prompt"] for e in _read_trace()):
                            break
                        await asyncio.sleep(0.05)
                    assert any(e["worker"] == "w-beta" and "E2E peer HTTP wakeup" in e["prompt"] for e in _read_trace())

                    swarm_dir = temp_state / "swarms" / prim_conv
                    subprocess.run(
                        [
                            py,
                            cli,
                            "--board-dir",
                            str(swarm_dir),
                            "--swarm",
                            prim_conv,
                            "--agent",
                            "w-beta",
                            "--role",
                            "worker",
                            "--http-url",
                            "http://127.0.0.1:1",
                            "post",
                            "--channel",
                            "main",
                            "--kind",
                            "handoff",
                            "--summary",
                            "@w-alpha E2E peer disk-fallback wakeup",
                        ],
                        env=env,
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    for _ in range(80):
                        if any(e["worker"] == "w-alpha" and "E2E peer disk-fallback wakeup" in e["prompt"] for e in _read_trace()):
                            break
                        await asyncio.sleep(0.05)
                    assert any(
                        e["worker"] == "w-alpha" and "E2E peer disk-fallback wakeup" in e["prompt"] for e in _read_trace()
                    ), "Hop 3 disk-fallback MentionRouter watcher failed"

                    # Hop 4: Worker -> Coordinator (all 7 post triggers + synthetic INPUT_REQUIRED / FAILED / drop-to-0)
                    h4_posts = [
                        ("post", "@coordinator E2E ping"),
                        ("handoff", "E2E handoff without mention"),
                        ("question", "E2E question without mention"),
                        ("blocker", "E2E blocker without mention"),
                        ("approval_request", "E2E approval_request without mention"),
                        ("post", "[done] E2E done tag"),
                        ("post", "[stall] E2E stall tag"),
                    ]
                    for kind, summary in h4_posts:
                        await session.call_tool(
                            "swarm_board_post",
                            {"sender": "w-alpha", "kind": kind, "summary": summary},
                        )
                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "w-alpha", "kind": "post", "summary": "@primary blocked bypass"},
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {"action": "spawn", "name": "w-ir-e2e", "prompt": "[TRIGGER_INPUT_REQUIRED]", "return_immediately": False},
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {"action": "spawn", "name": "w-fail-e2e", "prompt": "[TRIGGER_FAILED]", "return_immediately": False},
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {"action": "spawn", "name": "w-last-e2e", "prompt": "Finish last", "return_immediately": False},
                    )

                    for _ in range(80):
                        cw = [p for e, p in wake_events if e == "coordinator_wake"]
                        synth_st = {(p.get("task") or {}).get("state") for p in cw if p.get("synthetic")}
                        if {"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_FAILED", "TASK_STATE_COMPLETED"}.issubset(synth_st):
                            break
                        await asyncio.sleep(0.05)

                    cw_all = [p for e, p in wake_events if e == "coordinator_wake"]
                    cw_summaries = [p["message"]["summary"] for p in cw_all if not p.get("synthetic")]
                    for _, s in h4_posts:
                        assert s in cw_summaries, f"Hop 4 missing coordinator_wake for {s!r}"
                    synth_pairs = {((p.get("task") or {}).get("state"), (p.get("task") or {}).get("activeWorkerCount")) for p in cw_all if p.get("synthetic")}
                    assert ("TASK_STATE_INPUT_REQUIRED", 0) in synth_pairs
                    assert ("TASK_STATE_FAILED", 0) in synth_pairs
                    assert ("TASK_STATE_COMPLETED", 0) in synth_pairs
                    assert not any(e == "primary_wake" for e, _ in wake_events), "Hop 4 worker emitted primary_wake"

                    # Hop 5: Coordinator -> Primary (@primary and kind='escalation')
                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "coordinator", "kind": "post", "summary": "@primary E2E mention wake"},
                    )
                    await session.call_tool(
                        "swarm_board_post",
                        {"sender": "coordinator", "kind": "escalation", "summary": "E2E escalation wake"},
                    )
                    for _ in range(80):
                        if (
                            sum(1 for e, _ in wake_events if e == "primary_wake") >= 2
                            and sum(1 for e, _ in prim_board_events if e == "primary_wake") >= 2
                        ):
                            break
                        await asyncio.sleep(0.05)
                    assert sum(1 for e, _ in wake_events if e == "primary_wake") == 2
                    assert sum(1 for e, _ in prim_board_events if e == "primary_wake") == 2

                    # Hop 6: Tier-3 delegate_task subagent tracked under subagents, never wakes Coordinator/Primary
                    wake_count_before_h6 = sum(1 for e, _ in wake_events if e in ("coordinator_wake", "primary_wake"))
                    env_keeper_env = keeper_gate  # keep w-keeper active using busy_gate_file? Use a separate gate via prompt?
                    # Use busy_gate after removing it so w-keeper stays busy while w-sub completes!
                    busy_gate.unlink(missing_ok=True)
                    await session.call_tool(
                        "swarm_worker",
                        {"action": "spawn", "name": "w-keeper-e2e", "prompt": "Keep active [BUSY_GATE]", "return_immediately": True},
                    )
                    await session.call_tool(
                        "swarm_worker",
                        {"action": "spawn", "name": "w-sub-e2e", "prompt": "Run [DELEGATE_SUBAGENT]", "return_immediately": False},
                    )
                    await asyncio.sleep(0.25)
                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        snap_h6 = (await hc.get(f"{base_url}/board?conversationId={coord_conv}", headers=auth_hdr)).json()
                    sub_mem = next(m for m in snap_h6["members"] if m["name"] == "w-sub-e2e")
                    assert len(sub_mem["subagents"]) == 1
                    assert sub_mem["subagents"][0]["id"] == "tc-sub-e2e"
                    assert sub_mem["subagents"][0]["status"] == "done"
                    wake_count_after_h6 = sum(1 for e, _ in wake_events if e in ("coordinator_wake", "primary_wake"))
                    assert wake_count_after_h6 == wake_count_before_h6, "Hop 6 delegate_task unexpectedly emitted wake!"

                    busy_gate.write_text("go", encoding="utf-8")
                    for _ in range(60):
                        st_k = json.loads((await session.call_tool("swarm_worker", {"action": "status", "name": "w-keeper-e2e"})).content[0].text)
                        if st_k["task"]["status"]["state"] == "TASK_STATE_COMPLETED":
                            break
                        await asyncio.sleep(0.05)

                    print("8. ALL 6 WAKEUP HOPS + ENDPOINT SECURITY E2E OK")

                    # Step 9: Upgrade Contract (HERMES_SWARM_* env, telemetry rollup, prose/code mention suppression, worktree workspace, kill suppress)
                    tel_res = json.loads(
                        (
                            await session.call_tool(
                                "swarm_worker",
                                {
                                    "action": "spawn",
                                    "name": "w-tel-e2e",
                                    "prompt": "Run [TELEMETRY_BURST]",
                                    "worktree": True,
                                    "return_immediately": False,
                                },
                            )
                        ).content[0].text
                    )
                    assert tel_res["tasks"][0]["status"]["state"] == "TASK_STATE_COMPLETED"

                    tel_traces = [e for e in _read_trace() if e["worker"] == "w-tel-e2e"]
                    assert tel_traces and tel_traces[0]["swarm_env"]["HERMES_SWARM_ID"] == prim_conv
                    assert tel_traces[0]["swarm_env"]["HERMES_SWARM_AGENT"] == "w-tel-e2e"
                    assert tel_traces[0]["swarm_env"]["HERMES_SWARM_ROLE"] == "worker"
                    assert Path(tel_traces[0]["swarm_env"]["HERMES_SWARM_BOARD_DIR"]) == swarm_dir
                    assert tel_traces[0]["swarm_env"]["HERMES_SWARM_HTTP_URL"] == base_url
                    assert tel_traces[0]["swarm_env"]["HERMES_SWARM_CONVERSATION_ID"] == prim_conv
                    tel_prompt = tel_traces[0]["prompt"]
                    assert f'swarm_id="{prim_conv}"' in tel_prompt
                    assert 'agent="w-tel-e2e"' in tel_prompt
                    assert f'http_url="{base_url}"' in tel_prompt
                    assert f'conversation_id="{prim_conv}"' in tel_prompt
                    assert "Emergency fallback ONLY" in tel_prompt
                    assert -1 < tel_prompt.find("swarm_post") < tel_prompt.find("board_cli.py")

                    # Native hermes-swarm-bridge plugin works WITHOUT HERMES_SWARM_ID via explicit args against live server,
                    # AND preserves raw kind ('handoff_ready', 'blocker') + control dict in board.json & /board snapshot
                    import importlib.util

                    real_bridge_dir = Path.home() / "AppData" / "Local" / "hermes" / "plugins" / "hermes-swarm-bridge"
                    if (real_bridge_dir / "__init__.py").is_file():
                        bspec = importlib.util.spec_from_file_location(
                            "hermes_swarm_bridge_e2e_check",
                            real_bridge_dir / "__init__.py",
                            submodule_search_locations=[str(real_bridge_dir)],
                        )
                        assert bspec is not None and bspec.loader is not None
                        bmod = importlib.util.module_from_spec(bspec)
                        sys.modules["hermes_swarm_bridge_e2e_check"] = bmod
                        bspec.loader.exec_module(bmod)

                        class _BridgeCtx:
                            def __init__(self) -> None:
                                self.tools: dict[str, dict] = {}
                                self.sections: dict[str, object] = {}

                            def register_tool(self, *, name, toolset, schema, handler, description="", **kwargs):
                                self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler}

                            def register_system_prompt_section(self, sid_sec, content, **kwargs):
                                self.sections[sid_sec] = content

                        saved_env = {k: os.environ.pop(k, None) for k in ("HERMES_SWARM_ID", "HERMES_SWARM_AGENT", "HERMES_A2A_SWARM_ID")}
                        explicit_tok_file = tmp_path / "e2e_explicit_token"
                        explicit_tok_file.write_text(wake_token, encoding="utf-8")
                        try:
                            bctx = _BridgeCtx()
                            bmod.register(bctx)
                            assert len(bctx.tools) == 7, f"Expected 7 tools without HERMES_SWARM_ID, got {list(bctx.tools)}"
                            assert "swarm_channel" in bctx.tools
                            assert bctx.sections == {}, f"Prompt section leaked without HERMES_SWARM_ID: {bctx.sections}"
                            exp_args = {
                                "swarm_id": prim_conv,
                                "agent": "w-tel-e2e",
                                "http_url": base_url,
                                "board_dir": str(swarm_dir),
                                "token_file": str(explicit_tok_file),
                                "conversation_id": prim_conv,
                            }
                            bpost = json.loads(bctx.tools["swarm_post"]["handler"]({
                                **exp_args,
                                "kind": "handoff_ready",
                                "summary": "@coordinator [done] E2E native plugin without env",
                            }))
                            assert bpost["ok"] and bpost["source"] == "http", f"Unexpected bpost: {bpost}"
                            assert bpost["message"]["kind"] == "handoff_ready"
                            bread = json.loads(bctx.tools["swarm_read"]["handler"]({**exp_args, "since": 0}))
                            assert bread["ok"] and bread["source"] == "http", f"Unexpected bread: {bread}"
                            bclaim = json.loads(bctx.tools["swarm_claim"]["handler"]({**exp_args, "scope": "e2e/claim.py"}))
                            assert bclaim["ok"] and bclaim["source"] == "http", f"Unexpected bclaim: {bclaim}"
                            brel = json.loads(bctx.tools["swarm_release"]["handler"]({**exp_args, "scope": "e2e/claim.py"}))
                            assert brel["ok"] and brel["source"] == "http", f"Unexpected brel: {brel}"
                            bchan = json.loads(bctx.tools["swarm_channel"]["handler"]({**exp_args, "action": "list"}))
                            assert bchan["ok"] and bchan["source"] == "http", f"Unexpected bchan: {bchan}"
                        finally:
                            for ek, ev in saved_env.items():
                                if ev is not None:
                                    os.environ[ek] = ev

                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        r_blk = await hc.post(
                            f"{base_url}/internal/board/post",
                            headers=auth_hdr,
                            json={
                                "conversationId": coord_conv,
                                "sender": "w-tel-e2e",
                                "kind": "blocker",
                                "summary": "E2E blocker with control",
                                "control": {"action": "hold", "lane": "e2e"},
                            },
                        )
                        assert r_blk.status_code == 200
                        assert r_blk.json()["kind"] == "blocker"
                        assert r_blk.json()["control"] == {"action": "hold", "lane": "e2e"}

                        disk_msgs = {
                            m["summary"]: m
                            for m in json.loads((swarm_dir / "board.json").read_text(encoding="utf-8"))["messages"]
                        }
                        assert disk_msgs["E2E blocker with control"]["kind"] == "blocker"
                        assert disk_msgs["E2E blocker with control"]["control"] == {"action": "hold", "lane": "e2e"}

                        snap_ctl = (await hc.get(f"{base_url}/board?conversationId={coord_conv}", headers=auth_hdr)).json()
                        snap_msgs = {m["summary"]: m for m in snap_ctl["messages"]}
                        assert snap_msgs["E2E blocker with control"]["kind"] == "blocker"
                        assert snap_msgs["E2E blocker with control"]["control"] == {"action": "hold", "lane": "e2e"}

                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        snap_tel = (await hc.get(f"{base_url}/board?conversationId={coord_conv}", headers=auth_hdr)).json()
                    assert ".worktrees" not in snap_tel["meta"]["workspace"]
                    assert Path(snap_tel["meta"]["workspace"]) == temp_repo.resolve()
                    tel_mem = next(m for m in snap_tel["members"] if m["name"] == "w-tel-e2e")
                    assert tel_mem["toolCalls"] == 14
                    assert len(tel_mem["recentTools"]) == 12
                    assert tel_mem["tokens"]["total"] == 1000
                    assert tel_mem["tps"] == 200.0
                    assert tel_mem["elapsedMs"] == 1000
                    assert tel_mem["costUsd"] == 0.01

                    m_st = json.loads((await session.call_tool("swarm_manage", {"action": "status"})).content[0].text)
                    w_st = json.loads((await session.call_tool("swarm_worker", {"action": "status", "name": "w-tel-e2e"})).content[0].text)
                    assert m_st["telemetry"] == snap_tel["telemetry"]
                    assert w_st["telemetry"] == snap_tel["telemetry"]
                    assert w_st["telemetry"]["totalToolCalls"] >= 14
                    assert w_st["telemetry"]["totalTokens"] >= 1000

                    # Prose details & backticked summary mentions must not populate mentions
                    p_sup = json.loads(
                        (
                            await session.call_tool(
                                "swarm_board_post",
                                {
                                    "sender": "w-tel-e2e",
                                    "summary": "Noop check `@w-alpha` and ```@w-beta```",
                                    "details": "Should ignore @w-alpha @coordinator in details",
                                },
                            )
                        ).content[0].text
                    )
                    assert p_sup["mentions"] == []

                    # Kill ALL 5/5 active workers must not emit spurious "All active" coordinator_wake
                    wake_idx_before_kill = len(wake_events)
                    busy_gate.unlink(missing_ok=True)
                    kill_names = [f"w-kill-e2e-{i}" for i in range(1, 6)]
                    for kname in kill_names:
                        await session.call_tool(
                            "swarm_worker",
                            {"action": "spawn", "name": kname, "prompt": f"Hold {kname} [BUSY_GATE]", "return_immediately": True},
                        )
                    await asyncio.sleep(0.25)
                    for kname in kill_names:
                        k_res = json.loads(
                            (await session.call_tool("swarm_worker", {"action": "kill", "name": kname})).content[0].text
                        )
                        assert k_res["killed"] is True
                    await asyncio.sleep(0.25)
                    new_wakes = wake_events[wake_idx_before_kill:]
                    assert not any(
                        "All active" in str((p or {}).get("summary") or ((p or {}).get("message") or {}).get("summary") or "")
                        for _, p in new_wakes
                    ), (
                        f"Spurious All active wake after 5/5 kill: {new_wakes}"
                    )

                    print("9. UPGRADE CONTRACT (HERMES_SWARM_*, TELEMETRY, BUG FIXES) E2E OK")
                finally:
                    t_wake.cancel()
                    t_cboard.cancel()
                    t_pboard.cancel()
                    await asyncio.gather(t_wake, t_cboard, t_pboard, return_exceptions=True)


if __name__ == "__main__":
    asyncio.run(run_e2e())
