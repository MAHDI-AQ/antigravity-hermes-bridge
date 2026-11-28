"""Unit and integration tests for the rebuilt v2 Hermes A2A Swarm backend."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("HERMES_SWARM_ROLE_POLICY", "default")

from starlette.testclient import TestClient

import a2a_http_client
import board as board_mod
import board_cli
import catalog as catalog_mod
import http_server
import models
import runtime as runtime_mod
import server as mcp_srv
import store
import wakeup as wakeup_mod
import win_launcher
import worktree as worktree_mod


def _init_temp_git_repo(repo_dir: Path) -> None:
    repo_dir.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_dir), check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(repo_dir), check=True)
    subprocess.run(["git", "config", "user.name", "TestRunner"], cwd=str(repo_dir), check=True)
    (repo_dir / "app.py").write_text("def greet():\n    return 'hello'\n", encoding="utf-8")
    subprocess.run(["git", "add", "app.py"], cwd=str(repo_dir), check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial commit"],
        cwd=str(repo_dir),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


class ModelsAndStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-store-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("HERMES_A2A_STATE_DIR", "HERMES_A2A_SWARM_ID", "HERMES_HOME", "HERMES_EXE")
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("HERMES_A2A_SWARM_ID", None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_v2_models_roles_and_task_view(self) -> None:
        self.assertEqual(models.SCHEMA_VERSION, 2)
        self.assertEqual(models.DEFAULT_CHANNEL, "#general")
        self.assertEqual(
            set(models.ALL_ROLES),
            {"primary", "coordinator", "worker", "verifier", "researcher", "monitor", "narrator", "subagent"},
        )
        self.assertEqual(models.validate_worker_role("verifier"), "verifier")
        self.assertEqual(models.validate_worker_role(None), "worker")
        with self.assertRaises(ValueError):
            models.validate_worker_role("coordinator")
        with self.assertRaises(ValueError):
            models.validate_worker_role("primary")

        cfg = models.WorkerConfig.from_dict({
            "model": "qwen/qwen3-coder",
            "provider": "openrouter",
            "role": "researcher",
            "workerName": "res-1",
            "channels": ["design", "#Backend", "main"],
            "worktree": True,
            "parentTaskId": "task-p1",
            "forkFrom": "worker-1",
        })
        self.assertEqual(cfg.worker_name, "res-1")
        self.assertEqual(cfg.role, "researcher")
        self.assertEqual(cfg.channels, ["#design", "#backend", "#general"])
        self.assertEqual(cfg.parent_task_id, "task-p1")
        self.assertEqual(cfg.fork_from, "worker-1")
        overrides = cfg.to_resume_overrides()
        self.assertIn("model", overrides)
        self.assertIn("worker_name", overrides)

        task = {
            "id": "task-1",
            "contextId": "ctx-1",
            "status": {"state": models.TaskState.COMPLETED.value, "timestamp": models.now_iso()},
            "artifacts": [models.new_artifact("response", [models.text_part("done")])],
            "history": [
                models.new_message("ROLE_USER", [models.text_part("q1")], context_id="ctx-1", task_id="task-1"),
                models.new_message("ROLE_AGENT", [models.text_part("a1")], context_id="ctx-1", task_id="task-1"),
            ],
        }
        v1 = models.format_task_view(task, history_length=1, include_artifacts=False)
        self.assertNotIn("artifacts", v1)
        self.assertEqual(len(v1["history"]), 1)

        v0 = models.format_task_view(task, history_length=0, include_artifacts=True)
        self.assertNotIn("history", v0)
        self.assertEqual(len(v0["artifacts"]), 1)

    def test_atomic_file_lock_contention_and_stale_released_recovery(self) -> None:
        counter_file = self.tmp_root / "counter.json"
        lock_file = self.tmp_root / "counter.lock"
        counter_file.write_text(json.dumps({"n": 0}), encoding="utf-8")

        def _worker(_: int) -> None:
            for _ in range(10):
                with store.atomic_file_lock(lock_file, timeout=5.0):
                    cur = json.loads(counter_file.read_text(encoding="utf-8"))
                    cur["n"] += 1
                    store.atomic_write_json(counter_file, cur)

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_worker, range(8)))

        final = json.loads(counter_file.read_text(encoding="utf-8"))
        self.assertEqual(final["n"], 80)
        self.assertFalse(lock_file.exists())

        # Stale dead-PID lock is reclaimed cleanly
        lock_file.write_text(
            json.dumps({"pid": 9999999, "ts": time.time() - 120.0, "token": "stale", "released": False}),
            encoding="utf-8",
        )
        with store.atomic_file_lock(lock_file, timeout=1.0, stale_seconds=10.0):
            self.assertTrue(lock_file.exists())
        self.assertFalse(lock_file.exists())

        # Released lock (when unlink had transient PermissionError) can be taken over
        lock_file.write_text(
            json.dumps({"pid": 0, "ts": 0.0, "token": "prev", "released": True}),
            encoding="utf-8",
        )
        with store.atomic_file_lock(lock_file, timeout=1.0):
            pass
        self.assertFalse(lock_file.exists())

    def test_swarm_store_migration_isolation_and_registry(self) -> None:
        st = store.SwarmStore(self.state_dir)
        ws_a = self.tmp_root / "repo-alpha"
        ws_b = self.tmp_root / "repo-beta"
        ws_a.mkdir()
        ws_b.mkdir()

        # v1 -> v2 meta migration (in-memory normalization)
        migrated_meta = store.migrate_meta_v1_to_v2(
            {
                "workspacePath": str(ws_a),
                "conversationId": "conv-primary-a",
                "coordinatorConversationId": "conv-coord-a",
                "goal": "Ship v2",
            },
            "alpha",
        )
        self.assertEqual(migrated_meta["schemaVersion"], 2)
        self.assertEqual(Path(migrated_meta["workspace"]), ws_a.resolve())
        self.assertEqual(migrated_meta["primaryConversationId"], "conv-primary-a")
        self.assertEqual(migrated_meta["coordinatorConversationId"], "conv-coord-a")
        self.assertEqual(migrated_meta["conversationIds"], ["conv-primary-a", "conv-coord-a"])

        # v1 -> v2 board migration
        migrated_board = store.migrate_board_v1_to_v2(
            {
                "title": "Legacy Board",
                "channels": {"design": {"topic": "UI"}},
                "claims": {"api/": {"member": "worker-1", "notes": "editing"}},
                "messages": [{"seq": 3, "agent": "worker-1", "text": "Ready", "kind": "handoff"}],
            },
            "alpha",
        )
        self.assertEqual(migrated_board["schemaVersion"], 2)
        self.assertIn("#general", migrated_board["channels"])
        self.assertIn("#design", migrated_board["channels"])
        self.assertEqual(migrated_board["claims"]["api/"]["holder"], "worker-1")
        self.assertEqual(migrated_board["nextSeq"], 4)
        self.assertEqual(migrated_board["messages"][0]["type"], "handoff")

        # Strict 1:1 session isolation (including two conversations sharing the SAME workspace)
        st.upsert_meta(
            "sw-a",
            title="Alpha",
            workspace=str(ws_a),
            primary_conversation_id="conv-p-a",
            coordinator_conversation_id="conv-c-a",
        )
        st.upsert_meta(
            "sw-a2",
            title="Alpha Second Session",
            workspace=str(ws_a),
            primary_conversation_id="conv-p-a2",
            coordinator_conversation_id="conv-c-a2",
        )
        st.upsert_meta(
            "sw-b",
            title="Beta",
            workspace=str(ws_b),
            primary_conversation_id="conv-p-b",
            coordinator_conversation_id="conv-c-b",
        )

        self.assertEqual(st.resolve_swarm_for_conversation("conv-p-a"), "sw-a")
        self.assertEqual(st.resolve_swarm_for_conversation("conv-c-a"), "sw-a")
        self.assertEqual(st.resolve_swarm_for_conversation("conv-p-a2"), "sw-a2")
        self.assertEqual(st.resolve_swarm_for_conversation("conv-c-a2"), "sw-a2")
        self.assertEqual(st.resolve_swarm_for_conversation("conv-p-b"), "sw-b")
        self.assertIsNone(st.resolve_swarm_for_conversation("conv-unknown"))
        self.assertIsNone(st.resolve_swarm_for_conversation(""))
        self.assertIsNone(st.resolve_swarm_for_conversation(None))
        self.assertEqual(store.derive_swarm_id(conversation_id="conv-p-a", workspace=str(ws_a)), "conv-p-a")

        scoped_a = st.resolve_scoped_swarms(conversation_id="conv-c-a")
        self.assertEqual([s["swarmId"] for s in scoped_a], ["sw-a"])
        scoped_ws = st.resolve_scoped_swarms(workspace=str(ws_b))
        self.assertEqual([s["swarmId"] for s in scoped_ws], ["sw-b"])
        self.assertEqual(st.resolve_scoped_swarms(conversation_id="conv-unknown"), [])

        # Removal of multi-swarm switching artifacts in store & SwarmManager
        self.assertFalse(hasattr(store, "get_active_swarm_id"))
        self.assertFalse(hasattr(store, "set_active_swarm_id"))
        self.assertFalse(hasattr(runtime_mod.SwarmManager, "set_active_swarm"))
        self.assertFalse(hasattr(runtime_mod.SwarmManager, "get_active_swarm"))
        tmp_mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        self.assertFalse(hasattr(tmp_mgr, "active_swarm_id"))

        # Rename, archive, and default protection
        renamed = st.rename_swarm("sw-b", "Beta Renamed")
        self.assertEqual(renamed["title"], "Beta Renamed")
        st.archive_swarm("sw-b", archived=True)
        self.assertEqual(st.resolve_scoped_swarms(workspace=str(ws_b), include_archived=False), [])
        self.assertEqual(len(st.resolve_scoped_swarms(workspace=str(ws_b), include_archived=True)), 1)
        with self.assertRaises(ValueError):
            st.archive_swarm("default", archived=True)

        # Path containment guard (case-insensitive on Windows + traversal blocked)
        inside = ws_a / "sub" / "shot.png"
        inside.parent.mkdir(parents=True)
        inside.write_bytes(b"png")
        outside = self.tmp_root / "outside.png"
        outside.write_bytes(b"png")
        self.assertTrue(store.is_path_within(inside, [ws_a]))
        self.assertFalse(store.is_path_within(outside, [ws_a]))

        # Registry registration, dead PID pruning & activeSwarmId removal
        store.register_server_instance(
            http_url="http://127.0.0.1:19999",
            swarm_id="sw-a",
            workspace=str(ws_a),
            conversation_id="conv-p-a",
            pid=os.getpid(),
            state_dir=self.state_dir,
        )
        reg_file = store.get_registry_file()
        raw_reg = json.loads(reg_file.read_text(encoding="utf-8"))
        self.assertNotIn("activeSwarmId", raw_reg)
        raw_reg["activeSwarmId"] = "legacy-stale-swarm"
        raw_reg["servers"]["9999998"] = {"pid": 9999998, "swarmId": "ghost", "httpUrl": "http://127.0.0.1:9"}
        reg_file.write_text(json.dumps(raw_reg), encoding="utf-8")
        pruned = store.get_registry(state_dir=self.state_dir)
        self.assertNotIn("activeSwarmId", pruned)
        self.assertIn(str(os.getpid()), pruned["servers"])
        self.assertNotIn("9999998", pruned["servers"])
        store.unregister_server_instance(pid=os.getpid(), state_dir=self.state_dir)


class CatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-cat-")
        self.tmp_root = Path(self._tmp.name)
        self.hermes_home = self.tmp_root / "hermes_home"
        self.state_dir = self.tmp_root / "state"
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("HERMES_HOME", "HERMES_EXE", "HERMES_A2A_STATE_DIR", "HERMES_A2A_CATALOG_TTL_S")
        }
        os.environ["HERMES_HOME"] = str(self.hermes_home)
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ["HERMES_EXE"] = str(self.tmp_root / "missing_hermes_exe")
        catalog_mod.clear_catalog_cache()

        self.hermes_home.mkdir(parents=True, exist_ok=True)
        (self.hermes_home / "config.yaml").write_text(
            "\n".join([
                "model:",
                "  default: test-model-1",
                "  provider: test-provider",
                "  aliases:",
                "    fast: test-model-1",
                "providers:",
                "  test-provider:",
                "    name: Test Provider",
                "    base_url: https://api.example.invalid/v1",
                "    api_key: SECRET_SHOULD_NOT_LEAK",
                "    models:",
                "      test-model-1: {}",
                "a2a_agents:",
                "  peer-one:",
                "    url: http://127.0.0.1:9988",
                "    auth:",
                "      type: bearer",
                "      token: SECRET_PEER_TOKEN",
            ]),
            encoding="utf-8",
        )
        (self.hermes_home / "provider_models_cache.json").write_text(
            json.dumps({"test-provider": {"models": ["test-model-1", "test-model-2"]}}),
            encoding="utf-8",
        )
        skill_dir = self.hermes_home / "skills" / "sample-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Sample\n", encoding="utf-8")
        (self.hermes_home / "profiles" / "coding").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        catalog_mod.clear_catalog_cache()
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_catalog_discovery_secret_redaction_and_ttl_cache(self) -> None:
        cat1 = catalog_mod.get_hermes_catalog(ttl_seconds=60.0)
        self.assertEqual(cat1["default_model"], "test-model-1")
        self.assertEqual(cat1["default_provider"], "test-provider")
        self.assertIn("test-model-2", cat1["providers"]["test-provider"]["models"])
        self.assertIn("coding", cat1["profiles"])
        self.assertIn("sample-skill", cat1["enabled_skills"])

        serialized = json.dumps(cat1)
        self.assertNotIn("SECRET_SHOULD_NOT_LEAK", serialized)
        self.assertNotIn("SECRET_PEER_TOKEN", serialized)
        self.assertTrue(cat1["a2a_peers"]["peer-one"]["auth"]["configured"])

        # Mutate config on disk -> cached response returned until refresh=True
        (self.hermes_home / "config.yaml").write_text(
            "model:\n  default: test-model-updated\n  provider: test-provider\n",
            encoding="utf-8",
        )
        cat_cached = catalog_mod.get_hermes_catalog(ttl_seconds=60.0)
        self.assertEqual(cat_cached["default_model"], "test-model-1")

        cat_refreshed = catalog_mod.get_hermes_catalog(refresh=True, ttl_seconds=60.0)
        self.assertEqual(cat_refreshed["default_model"], "test-model-updated")

    def test_catalog_discovers_project_local_skills_and_respects_disabled(self) -> None:
        (self.hermes_home / "config.yaml").write_text(
            "\n".join([
                "model:",
                "  default: test-model-1",
                "  provider: test-provider",
                "skills:",
                "  disabled:",
                "    - blocked-proj-skill",
            ]),
            encoding="utf-8",
        )
        # Git repo with subdir + .agents/skills and .hermes/skills
        repo = self.tmp_root / "proj-git"
        _init_temp_git_repo(repo)
        (repo / ".agents" / "skills" / "proj-ag-skill").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "skills" / "proj-ag-skill" / "SKILL.md").write_text("# AG\n", encoding="utf-8")
        (repo / ".hermes" / "skills" / "proj-hm-skill").mkdir(parents=True, exist_ok=True)
        (repo / ".hermes" / "skills" / "proj-hm-skill" / "SKILL.md").write_text("# HM\n", encoding="utf-8")
        (repo / ".agents" / "skills" / "blocked-proj-skill").mkdir(parents=True, exist_ok=True)
        (repo / ".agents" / "skills" / "blocked-proj-skill" / "SKILL.md").write_text("# Blocked\n", encoding="utf-8")
        sub = repo / "packages" / "api"
        sub.mkdir(parents=True, exist_ok=True)

        cat_sub = catalog_mod.get_hermes_catalog(workdir=str(sub), ttl_seconds=60.0)
        self.assertIn("sample-skill", cat_sub["enabled_skills"])
        self.assertIn("proj-ag-skill", cat_sub["project_skills"])
        self.assertIn("proj-hm-skill", cat_sub["project_skills"])
        self.assertNotIn("blocked-proj-skill", cat_sub["project_skills"])
        self.assertNotIn("blocked-proj-skill", cat_sub["enabled_skills"])
        self.assertIn("proj-ag-skill", cat_sub["enabled_skills"])

        # Non-git umbrella workspace (like clash-of-kings) still resolves its own .agents/skills
        umbrella = self.tmp_root / "umbrella"
        (umbrella / ".agents" / "skills" / "umbrella-skill").mkdir(parents=True, exist_ok=True)
        (umbrella / ".agents" / "skills" / "umbrella-skill" / "SKILL.md").write_text("# U\n", encoding="utf-8")

        cat_umb = catalog_mod.get_hermes_catalog(workdir=str(umbrella), ttl_seconds=60.0)
        self.assertEqual(cat_umb["project_skills"], ["umbrella-skill"])
        self.assertIn("umbrella-skill", cat_umb["enabled_skills"])
        self.assertNotIn("proj-ag-skill", cat_umb["enabled_skills"])

    def test_discover_cli_and_timeout_tree_kill(self) -> None:
        async def _run() -> None:
            # Mock _hermes_exe to python.exe? Instead test run_hermes_cli timeout with python sleeper
            with patch.object(catalog_mod, "_hermes_exe", return_value=Path(sys.executable)):
                res = await catalog_mod.run_hermes_cli(
                    ["-c", "import time; time.sleep(10)"],
                    timeout=0.35,
                )
            self.assertFalse(res["ok"])
            self.assertEqual(res["exit_code"], -1)
            self.assertIn("Timed out", res["stderr"])

            # Git inspect timeout tree-kill
            repo = self.tmp_root / "git-repo"
            _init_temp_git_repo(repo)
            res_ok = await catalog_mod.run_git_inspect(str(repo), action="status", timeout=5.0)
            self.assertTrue(res_ok["ok"])

        asyncio.run(_run())


class BoardWakeupAndCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-board-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_A2A_HTTP_URL",
                "HERMES_A2A_TOKEN_FILE",
                "HERMES_A2A_HTTP_TOKEN",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        for k in ("HERMES_A2A_SWARM_ID", "HERMES_A2A_HTTP_URL", "HERMES_A2A_TOKEN_FILE", "HERMES_A2A_HTTP_TOKEN"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_channels_mention_routing_pending_queue_debounce_and_watcher(self) -> None:
        svc = board_mod.BoardService(self.state_dir)
        sid = "route-test"
        svc.upsert_member("backend-1", role="worker", state="waiting", channels=["#general"], swarm_id=sid)
        svc.upsert_member("backend-2", role="worker", state="waiting", channels=["#general"], swarm_id=sid)
        svc.upsert_member("ui-1", role="worker", state="waiting", channels=["#general"], swarm_id=sid)
        svc.upsert_member("monitor-1", role="monitor", state="waiting", channels=["*"], swarm_id=sid)

        # Create channel & subscribe backend-1, backend-2
        svc.channel_action("create", channel="api", topic="API lane", members=["backend-1", "backend-2"], swarm_id=sid)
        ch_list = svc.channel_action("list", swarm_id=sid)["channels"]
        self.assertTrue(any(c["displayName"] == "api" for c in ch_list))

        busy_workers: set[str] = set()
        woken_calls: list[tuple[str, str, str]] = []
        emitted_events: list[tuple[str, str, dict]] = []

        async def _wake(s_id: str, worker: str, prompt: str) -> bool:
            woken_calls.append((s_id, worker, prompt))
            return True

        def _emit(s_id: str, ev_type: str, payload: dict) -> None:
            emitted_events.append((s_id, ev_type, payload))

        async def _run() -> None:
            router = wakeup_mod.MentionRouter(
                svc,
                is_worker_busy=lambda _s, w: w.lower() in busy_workers,
                wake_worker=_wake,
                emit_event=_emit,
                poll_interval=0.1,
                debounce_seconds=0.06,
            )

            # 1) @#api wakes backend-1 & backend-2, NOT ui-1, NOT monitor-1? Wait: monitor has channels=['*']!
            m_chan = svc.post_message(
                sender="ui-1",
                summary="Check @#api contract",
                channel="api",
                swarm_id=sid,
            )
            res_chan = await router.process_message(sid, m_chan)
            self.assertEqual(res_chan["wakeEvent"], None)
            await asyncio.sleep(0.12)
            woken_names = {w for _, w, _ in woken_calls}
            self.assertIn("backend-1", woken_names)
            self.assertIn("backend-2", woken_names)
            self.assertNotIn("ui-1", woken_names)
            woken_calls.clear()

            # 2) @all excludes monitor/narrator unless explicitly mentioned
            m_all = svc.post_message(sender="coordinator", sender_role="coordinator", summary="@all sync up", swarm_id=sid)
            await router.process_message(sid, m_all)
            await asyncio.sleep(0.12)
            woken_names = {w for _, w, _ in woken_calls}
            self.assertEqual(woken_names, {"backend-1", "backend-2", "ui-1"})
            woken_calls.clear()

            # 3) Debounce coalescing for rapid @ui-1 mentions
            m_d1 = svc.post_message(sender="backend-1", summary="@ui-1 first", swarm_id=sid)
            m_d2 = svc.post_message(sender="backend-2", summary="@ui-1 second", swarm_id=sid)
            await router.process_message(sid, m_d1)
            await router.process_message(sid, m_d2)
            await asyncio.sleep(0.14)
            ui_wakes = [p for _, w, p in woken_calls if w == "ui-1"]
            self.assertEqual(len(ui_wakes), 1)
            self.assertIn("first", ui_wakes[0])
            self.assertIn("second", ui_wakes[0])
            woken_calls.clear()

            # 4) Busy worker -> queued in pendingMentions -> replayed on working->waiting transition
            busy_workers.add("backend-1")
            m_busy1 = svc.post_message(sender="ui-1", summary="@backend-1 fix schema", swarm_id=sid)
            m_busy2 = svc.post_message(sender="ui-1", summary="@backend also check tests", swarm_id=sid)
            res_b1 = await router.process_message(sid, m_busy1)
            res_b2 = await router.process_message(sid, m_busy2)
            self.assertIn("backend-1", res_b1["queued"])
            self.assertIn("backend-1", res_b2["queued"])
            self.assertEqual(len([w for _, w, _ in woken_calls if w == "backend-1"]), 0)

            busy_workers.discard("backend-1")
            flushed = await router.flush_pending_for_worker(sid, "backend-1")
            self.assertTrue(flushed)
            b1_wakes = [p for _, w, p in woken_calls if w == "backend-1"]
            self.assertEqual(len(b1_wakes), 1)
            self.assertIn("fix schema", b1_wakes[0])
            self.assertIn("also check tests", b1_wakes[0])
            woken_calls.clear()

            # 5) 500ms background disk watcher catches out-of-band board.json write
            router.start_watcher(asyncio.get_running_loop())
            try:
                await asyncio.sleep(0.05)
                # Write directly via separate BoardService instance (simulating CLI disk fallback)
                external_svc = board_mod.BoardService(self.state_dir)
                external_svc.post_message(
                    sender="backend-1",
                    summary="@coordinator [done] finished external write",
                    swarm_id=sid,
                )
                for _ in range(20):
                    if any(et == "coordinator_wake" for _, et, _ in emitted_events):
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(any(et == "coordinator_wake" for _, et, _ in emitted_events))
                self.assertFalse(any(et == "primary_wake" for _, et, _ in emitted_events))
            finally:
                await router.stop_watcher()

        asyncio.run(_run())

    def test_strict_3_tier_wakeup_classification(self) -> None:
        svc = board_mod.BoardService(self.state_dir)
        sid = "tier-law"
        svc.upsert_member("worker-a", role="worker", state="waiting", swarm_id=sid)

        # Tier-2 worker mentions @coordinator or @primary -> NEVER emits primary_wake
        m_worker_coord = svc.post_message(sender="worker-a", summary="@coordinator ready for review", msg_type="handoff", swarm_id=sid)
        self.assertEqual(wakeup_mod.classify_wake_event(m_worker_coord), "coordinator_wake")

        m_worker_escalate = svc.post_message(sender="worker-a", summary="Blocked on auth", msg_type="escalation", swarm_id=sid)
        self.assertEqual(wakeup_mod.classify_wake_event(m_worker_escalate), "coordinator_wake")

        m_worker_primary_attempt = svc.post_message(sender="worker-a", summary="@primary hello directly", msg_type="post", swarm_id=sid)
        self.assertIsNone(wakeup_mod.classify_wake_event(m_worker_primary_attempt))

        # Tier-2 Coordinator @primary or escalation -> emits primary_wake
        m_coord_esc = svc.post_message(
            sender="coordinator",
            sender_role="coordinator",
            summary="@primary Need decision on port",
            msg_type="escalation",
            swarm_id=sid,
        )
        self.assertEqual(wakeup_mod.classify_wake_event(m_coord_esc), "primary_wake")

        # Tier-3 subagent records & messages NEVER emit wake events
        events: list[tuple[str, str, dict]] = []
        svc.add_listener(lambda s, et, p: events.append((s, et, p)))
        svc.upsert_subagents(
            "worker-a",
            [models.new_subagent_record("deleg_1", parent_worker="worker-a", label="sub check")],
            swarm_id=sid,
        )
        sub_msg = models.new_board_message(
            seq=99,
            sender="deleg_1",
            summary="@coordinator @primary subagent leak test",
            msg_type="escalation",
            sender_role="subagent",
            sender_tier=3,
            mentions=["coordinator", "primary"],
        )
        self.assertIsNone(wakeup_mod.classify_wake_event(sub_msg))
        self.assertEqual(wakeup_mod.resolve_worker_targets(sub_msg, svc.read_board(sid)), [])
        self.assertFalse(any(et in ("coordinator_wake", "primary_wake") for _, et, _ in events))

    def test_board_cli_commands_http_atomic_vs_file_fallback(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "cli-test"
        mgr.store.upsert_meta(sid, title="CLI Test", primary_conversation_id="conv-p", coordinator_conversation_id="conv-c")
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        url = ctrl.start(host="127.0.0.1", port=0)
        tok_file = mgr.get_internal_token_file()
        try:
            import io
            from contextlib import redirect_stdout

            with redirect_stdout(io.StringIO()) as buf1:
                rc = board_cli.main([
                    "--swarm", sid,
                    "--agent", "worker-1",
                    "--http-url", url,
                    "--token-file", str(tok_file),
                    "post", "--channel", "main", "--kind", "handoff", "--summary", "@coordinator [done] via http",
                ])
            self.assertEqual(rc, 0)
            self.assertEqual(json.loads(buf1.getvalue())["summary"], "@coordinator [done] via http")

            # 2) File fallback when HTTP URL is unreachable (channel must exist first)
            mgr.board_service.channel_action("create", channel="ops", actor="coordinator", swarm_id=sid)
            with redirect_stdout(io.StringIO()) as buf2:
                rc_fb = board_cli.main([
                    "--swarm", sid,
                    "--agent", "worker-2",
                    "--http-url", "http://127.0.0.1:1",
                    "--token-file", str(tok_file),
                    "post", "--channel", "ops", "--kind", "post", "--summary", "fallback disk post",
                ])
            self.assertEqual(rc_fb, 0)
            self.assertEqual(json.loads(buf2.getvalue())["summary"], "fallback disk post")

            # 3) channels, claim, release, read, status
            with redirect_stdout(io.StringIO()):
                self.assertEqual(board_cli.main(["--swarm", sid, "--agent", "coordinator", "channels", "--op", "create", "--channel", "qa", "--members", "worker-1"]), 0)
                self.assertEqual(board_cli.main(["--swarm", sid, "--agent", "worker-1", "claim", "--scope", "src/api.py", "--notes", "editing"]), 0)
                st_doc = mgr.board_service.get_status(swarm_id=sid)
                self.assertIn("src/api.py", st_doc["claims"])
                self.assertEqual(board_cli.main(["--swarm", sid, "--agent", "worker-1", "release", "--scope", "src/api.py"]), 0)
                self.assertEqual(board_cli.main(["--swarm", sid, "--agent", "worker-1", "read", "--since", "0", "--mark-read"]), 0)
                self.assertEqual(board_cli.main(["--swarm", sid, "status"]), 0)

            msgs = mgr.board_service.read_messages(swarm_id=sid)["messages"]
            summaries = [m["summary"] for m in msgs]
            self.assertIn("@coordinator [done] via http", summaries)
            self.assertIn("fallback disk post", summaries)

            # 4) --conversation-id resolution (with coordinator conversationId -> primary swarmId & registry URL discovery)
            conv_sid = "conv-cli-primary"
            mgr.store.upsert_meta(
                conv_sid,
                title="CLI Conv Swarm",
                primary_conversation_id="conv-cli-primary",
                coordinator_conversation_id="conv-cli-coord",
            )
            store.register_server_instance(
                http_url=url,
                swarm_id=conv_sid,
                conversation_id="conv-cli-primary",
                pid=os.getpid(),
                state_dir=self.state_dir,
            )
            try:
                with redirect_stdout(io.StringIO()) as buf_conv:
                    rc_conv = board_cli.main([
                        "--conversation-id", "conv-cli-coord",
                        "--agent", "worker-conv",
                        "--token-file", str(tok_file),
                        "post", "--channel", "main", "--kind", "handoff", "--summary", "@coordinator via conversation-id",
                    ])
                self.assertEqual(rc_conv, 0)
                self.assertEqual(json.loads(buf_conv.getvalue())["summary"], "@coordinator via conversation-id")

                with redirect_stdout(io.StringIO()) as buf_read_conv:
                    rc_read_conv = board_cli.main([
                        "--conversation-id", "conv-cli-coord",
                        "read", "--since", "0",
                    ])
                self.assertEqual(rc_read_conv, 0)
                conv_read_payload = json.loads(buf_read_conv.getvalue())
                self.assertTrue(any(m["summary"] == "@coordinator via conversation-id" for m in conv_read_payload["messages"]))
            finally:
                store.unregister_server_instance(pid=os.getpid(), state_dir=self.state_dir)
        finally:
            ctrl.stop()

    def test_mention_regex_strips_trailing_punctuation_and_preserves_emails(self) -> None:
        svc = board_mod.BoardService(self.state_dir)
        sid = "mention-punct"
        svc.upsert_member("verifier", role="verifier", state="waiting", swarm_id=sid)
        svc.upsert_member("backend-1", role="worker", state="waiting", swarm_id=sid)

        parsed = board_mod.extract_mentions(
            "@coordinator. Please check @verifier, and @backend-1! Ignore foo@example.com."
        )
        self.assertEqual(parsed, ["coordinator", "verifier", "backend-1"])

        msg = svc.post_message(
            sender="worker-a",
            summary="@coordinator. Handing off to @verifier,",
            msg_type="handoff",
            swarm_id=sid,
        )
        self.assertEqual(msg["mentions"], ["coordinator", "verifier"])
        self.assertEqual(wakeup_mod.classify_wake_event(msg), "coordinator_wake")
        self.assertEqual(wakeup_mod.resolve_worker_targets(msg, svc.read_board(sid)), ["verifier"])

    def test_duplicate_wakeup_suppression_advances_last_read_seq_and_anti_ping_pong(self) -> None:
        svc = board_mod.BoardService(self.state_dir)
        sid = "dedupe-lastread"
        svc.upsert_member("worker-1", role="worker", state="waiting", swarm_id=sid)
        svc.upsert_member("worker-2", role="worker", state="waiting", swarm_id=sid)

        busy_workers: set[str] = set()
        woken_calls: list[tuple[str, str, str]] = []

        async def _wake(s_id: str, worker: str, prompt: str) -> bool:
            woken_calls.append((s_id, worker, prompt))
            return True

        async def _run() -> None:
            router = wakeup_mod.MentionRouter(
                svc,
                is_worker_busy=lambda _s, w: w.lower() in busy_workers,
                wake_worker=_wake,
                debounce_seconds=0.02,
            )

            # 1) @all wakes both waiting workers and advances their lastReadSeq to 1
            m1 = svc.post_message(sender="coordinator", sender_role="coordinator", summary="@all Kickoff", swarm_id=sid)
            await router.process_message(sid, m1)
            await asyncio.sleep(0.08)
            self.assertEqual({w for _, w, _ in woken_calls}, {"worker-1", "worker-2"})
            b1 = svc.read_board(sid)
            self.assertEqual(b1["members"]["worker-1"]["lastReadSeq"], 1)
            self.assertEqual(b1["members"]["worker-2"]["lastReadSeq"], 1)
            woken_calls.clear()

            # 2) Replaying m1 on a fresh MentionRouter must NEVER re-wake either worker
            fresh_router = wakeup_mod.MentionRouter(
                svc,
                is_worker_busy=lambda _s, w: w.lower() in busy_workers,
                wake_worker=_wake,
                debounce_seconds=0.02,
            )
            await fresh_router.process_message(sid, m1)
            await asyncio.sleep(0.08)
            self.assertEqual(woken_calls, [])

            # 3) Busy worker queues pendingMentions; flush replays only unread seqs and advances lastReadSeq
            busy_workers.add("worker-1")
            m2 = svc.post_message(sender="coordinator", sender_role="coordinator", summary="@worker-1 fix api", swarm_id=sid)
            m3 = svc.post_message(sender="coordinator", sender_role="coordinator", summary="@all check lint", swarm_id=sid)
            await router.process_message(sid, m2)
            await router.process_message(sid, m3)
            await asyncio.sleep(0.08)

            # worker-2 woke for m3 only (not m1/m2)
            w2_prompts = [p for _, w, p in woken_calls if w == "worker-2"]
            self.assertEqual(len(w2_prompts), 1)
            self.assertIn("check lint", w2_prompts[0])
            self.assertNotIn("Kickoff", w2_prompts[0])
            self.assertEqual(svc.read_board(sid)["members"]["worker-2"]["lastReadSeq"], m3["seq"])
            woken_calls.clear()

            busy_workers.discard("worker-1")
            flushed = await router.flush_pending_for_worker(sid, "worker-1")
            self.assertTrue(flushed)
            w1_prompts = [p for _, w, p in woken_calls if w == "worker-1"]
            self.assertEqual(len(w1_prompts), 1)
            self.assertIn("fix api", w1_prompts[0])
            self.assertIn("check lint", w1_prompts[0])
            self.assertNotIn("Kickoff", w1_prompts[0])
            self.assertEqual(svc.read_board(sid)["members"]["worker-1"]["lastReadSeq"], m3["seq"])
            woken_calls.clear()

            # 4) Subsequent unrelated @worker-2 post does NOT re-deliver m1/m2/m3 to worker-1
            m4 = svc.post_message(sender="coordinator", sender_role="coordinator", summary="@worker-2 next", swarm_id=sid)
            await router.process_message(sid, m4)
            await fresh_router.process_message(sid, m2)
            await fresh_router.process_message(sid, m3)
            await asyncio.sleep(0.08)
            self.assertEqual([w for _, w, _ in woken_calls], ["worker-2"])

        asyncio.run(_run())

        # Anti-Ping-Pong Rule present in both runtime preamble and SKILL.md
        preamble = runtime_mod.build_worker_preamble(
            models.WorkerConfig(role="worker", worker_name="w1"),
            swarm_id=sid,
            task_id="t-1",
            context_id="c-1",
            board_dir=svc.state_dir / "swarms" / sid,
        )
        self.assertIn("Anti-Ping-Pong Rule", preamble)
        self.assertIn("wakeup doorbell", preamble)

        skill_path = Path.home() / ".gemini" / "config" / "plugins" / "hermes-swarm" / "skills" / "hermes-swarm" / "SKILL.md"
        if skill_path.is_file():
            skill_txt = skill_path.read_text(encoding="utf-8")
            self.assertIn("Anti-Ping-Pong Rule", skill_txt)
            self.assertIn("wakeup doorbell", skill_txt)

    def test_mention_extraction_summary_only_and_strips_code_blocks(self) -> None:
        svc = board_mod.BoardService(self.state_dir)
        sid = "mention-code-details"
        svc.upsert_member("verifier", role="verifier", state="waiting", channels=["#general"], swarm_id=sid)
        svc.upsert_member("backend-1", role="worker", state="waiting", channels=["#general", "#backend"], swarm_id=sid)
        svc.upsert_member("worker-real", role="worker", state="waiting", channels=["#general"], swarm_id=sid)
        svc.channel_action("create", channel="backend", topic="Backend", members=["backend-1"], swarm_id=sid)

        # 1) Prose @mentions and @#channel in `details` ONLY must NEVER populate mentions or trigger wakeups
        m_details = svc.post_message(
            sender="worker-a",
            summary="Status update on API refactor",
            details="Reviewed @verifier @coordinator @all and @#backend in prose details.",
            swarm_id=sid,
        )
        self.assertEqual(m_details["mentions"], [])
        self.assertEqual(m_details["channelMentions"], [])
        self.assertIsNone(wakeup_mod.classify_wake_event(m_details))
        self.assertEqual(wakeup_mod.resolve_worker_targets(m_details, svc.read_board(sid)), [])
        self.assertFalse(svc.message_matches_target(m_details, target="verifier", board=svc.read_board(sid)))
        self.assertFalse(svc.message_matches_target(m_details, target="coordinator", board=svc.read_board(sid)))

        # 2) Inline backticks and fenced code blocks in `summary` must be stripped before mention extraction
        summary_with_code = (
            "Check `@verifier` and ``@coordinator`` and `@#backend` plus\n"
            "```python\n"
            "@verifier @all @#backend\n"
            "```\n"
            "~~~\n"
            "@coordinator\n"
            "~~~\n"
            "only @worker-real should wake."
        )
        self.assertEqual(board_mod.extract_mentions(summary_with_code), ["worker-real"])
        self.assertEqual(board_mod.extract_channel_mentions(summary_with_code), [])

        m_code = svc.post_message(
            sender="worker-a",
            summary=summary_with_code,
            details="@verifier @coordinator @#backend in details too",
            swarm_id=sid,
        )
        self.assertEqual(m_code["mentions"], ["worker-real"])
        self.assertEqual(m_code["channelMentions"], [])
        self.assertIsNone(wakeup_mod.classify_wake_event(m_code))
        self.assertEqual(wakeup_mod.resolve_worker_targets(m_code, svc.read_board(sid)), ["worker-real"])

        # 3) Explicit `to` still works alongside real summary mentions while code-quoted ones remain ignored
        m_explicit = svc.post_message(
            sender="worker-a",
            summary="Ignoring `@worker-real`, pinging @#backend",
            to=["@verifier"],
            swarm_id=sid,
        )
        self.assertEqual(m_explicit["mentions"], ["verifier"])
        self.assertEqual(m_explicit["channelMentions"], ["#backend"])
        self.assertEqual(wakeup_mod.resolve_worker_targets(m_explicit, svc.read_board(sid)), ["verifier", "backend-1"])


class HttpAndMcpSurfaceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-http-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)

        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_A2A_HTTP_URL",
                "HERMES_A2A_HTTP_TOKEN",
                "HERMES_A2A_DEFAULT_WORKDIR",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        for k in ("HERMES_A2A_SWARM_ID", "HERMES_A2A_HTTP_URL", "HERMES_A2A_HTTP_TOKEN", "HERMES_A2A_DEFAULT_WORKDIR"):
            os.environ.pop(k, None)

        self._orig_session_wd = mcp_srv._session_workdir
        self._orig_session_conv = mcp_srv._session_conversation_id
        self._orig_session_swarm = mcp_srv._session_swarm_id
        self._orig_mgr = mcp_srv._swarm_mgr
        self._orig_ctrl = mcp_srv._http_ctrl
        mcp_srv._session_workdir = None
        mcp_srv._session_conversation_id = None
        mcp_srv._session_swarm_id = None
        mcp_srv._swarm_mgr = runtime_mod.SwarmManager(
            state_dir=self.state_dir,
            default_workdir_provider=lambda: mcp_srv._session_workdir,
        )
        mcp_srv._http_ctrl = http_server.EmbeddedA2AHttpController(mcp_srv._swarm_mgr)

    def tearDown(self) -> None:
        try:
            mcp_srv._http_ctrl.stop()
        except Exception:
            pass
        mcp_srv._session_workdir = self._orig_session_wd
        mcp_srv._session_conversation_id = self._orig_session_conv
        mcp_srv._session_swarm_id = self._orig_session_swarm
        mcp_srv._swarm_mgr = self._orig_mgr
        mcp_srv._http_ctrl = self._orig_ctrl
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_http_observer_readonly_sse_frame_security_and_internal_auth(self) -> None:
        mgr = mcp_srv._swarm_mgr
        sid = "obs-swarm"
        mgr.store.upsert_meta(
            sid,
            title="Observer Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id="conv-primary-1",
            coordinator_conversation_id="conv-coord-1",
        )
        # Parallel second conversation sharing the SAME workspace must stay strictly isolated
        sid2 = "obs-swarm-2"
        mgr.store.upsert_meta(
            sid2,
            title="Observer Swarm 2",
            workspace=str(self.repo_dir),
            primary_conversation_id="conv-primary-2",
            coordinator_conversation_id="conv-coord-2",
        )
        mgr.board_service.post_message(
            sender="worker-two",
            summary="Session 2 private message",
            swarm_id=sid2,
        )

        swarm_dir = mgr.store.paths(sid).swarm_dir
        frames_dir = swarm_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)
        good_png = frames_dir / "shot.png"
        good_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)
        unreferenced_png = frames_dir / "unreferenced.png"
        unreferenced_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        bad_txt = frames_dir / "secret.txt"
        bad_txt.write_text("nope", encoding="utf-8")
        outside_png = self.tmp_root / "outside.png"
        outside_png.write_bytes(b"\x89PNG\r\n\x1a\n")

        mgr.board_service.post_message(
            sender="worker-ui",
            summary="Rendered screenshot",
            details=f"Screenshot: {good_png} | Main frame",
            swarm_id=sid,
        )

        app = http_server.create_a2a_app(mgr, http_url="http://127.0.0.1:19920")
        client = TestClient(app, base_url="http://127.0.0.1:19920")

        # 1) GET /board/swarms is removed -> 404
        self.assertEqual(client.get("/board/swarms").status_code, 404)
        self.assertEqual(client.get("/board/swarms?conversationId=conv-coord-1").status_code, 404)

        # 2) GET /board strict 1:1 conversationId scoping + ETag fast-path (no swarms list, ignores ?swarm=)
        r_missing = client.get("/board")
        self.assertEqual(r_missing.status_code, 200)
        self.assertEqual(r_missing.json(), {"conversationId": None, "swarm": None, "member": None})

        # Passing ?swarm= without conversationId does NOT leak a swarm
        r_swarm_only = client.get(f"/board?swarm={sid}")
        self.assertEqual(r_swarm_only.status_code, 200)
        self.assertEqual(r_swarm_only.json(), {"conversationId": None, "swarm": None, "member": None})

        r_empty_scope = client.get("/board?conversationId=conv-unassigned-999")
        self.assertEqual(r_empty_scope.status_code, 200)
        empty_data = r_empty_scope.json()
        self.assertEqual(empty_data, {"conversationId": "conv-unassigned-999", "swarm": None, "member": None})
        self.assertNotIn("swarms", empty_data)

        # Scoping by conversationId ignores ?swarm= override and enforces strict isolation on same workspace
        r_b1 = client.get(f"/board?conversationId=conv-coord-1&swarm={sid2}")
        self.assertEqual(r_b1.status_code, 200)
        snap = r_b1.json()
        etag = snap["etag"]
        self.assertEqual(snap["swarm"], sid)
        self.assertEqual(snap["member"], "coordinator")
        self.assertNotIn("swarms", snap)
        self.assertEqual(len(snap["frames"]), 1)
        self.assertFalse(any(m["summary"] == "Session 2 private message" for m in snap["messages"]))

        r_b2_other = client.get("/board?conversationId=conv-primary-2")
        self.assertEqual(r_b2_other.status_code, 200)
        snap2 = r_b2_other.json()
        self.assertEqual(snap2["swarm"], sid2)
        self.assertNotIn("swarms", snap2)
        self.assertEqual([m["summary"] for m in snap2["messages"]], ["Session 2 private message"])

        r_b2 = client.get(f"/board?conversationId=conv-coord-1&etag={etag}")
        self.assertEqual(r_b2.status_code, 200)
        self.assertTrue(r_b2.json()["unchanged"])

        # 3) GET /board/frame containment & strict conversationId guards
        r_ok = client.get(f"/board/frame?conversationId=conv-coord-1&path={good_png}")
        self.assertEqual(r_ok.status_code, 200)
        self.assertEqual(r_ok.headers["content-type"], "image/png")

        # Cross-conversation frame isolation & missing/unknown conversationId checks
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-2&path={good_png}").status_code, 403)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-unknown&path={good_png}").status_code, 404)
        self.assertEqual(client.get(f"/board/frame?path={good_png}").status_code, 400)

        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path=../outside.png").status_code, 403)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path=%2e%2e/outside.png").status_code, 403)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path=frames/shot%252epng").status_code, 400)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path=frames/shot%00.png").status_code, 400)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path={bad_txt}").status_code, 403)
        self.assertEqual(client.get(f"/board/frame?conversationId=conv-coord-1&path={unreferenced_png}").status_code, 403)
        # Passing ?workspace= must NOT expand allowed roots
        self.assertEqual(
            client.get(f"/board/frame?conversationId=conv-coord-1&workspace={self.tmp_root}&path={outside_png}").status_code,
            403,
        )

        # 4) Reject all legacy POST /board/* admin mutations with 404/405
        for route in (
            "/board/spawn",
            "/board/channels",
            "/board/claim",
            "/board/release",
            "/board/pause",
            "/board/digest",
            "/board/member",
            "/board/resume",
            "/board/clear_finished",
            "/board/post",
            "/board/reply",
            "/board/mission",
            "/board/plan",
            "/board/cancel",
        ):
            r_mut = client.post(route, json={})
            self.assertIn(r_mut.status_code, (404, 405), f"Expected 404/405 for POST {route}, got {r_mut.status_code}")

        # 5) POST /internal/board/post Bearer auth gating + conversationId resolution
        r_no_auth = client.post("/internal/board/post", json={"swarmId": sid, "sender": "w", "summary": "hi"})
        self.assertEqual(r_no_auth.status_code, 401)
        r_bad_auth = client.post(
            "/internal/board/post",
            headers={"Authorization": "Bearer wrong"},
            json={"swarmId": sid, "sender": "w", "summary": "hi"},
        )
        self.assertEqual(r_bad_auth.status_code, 401)
        r_bad_host = client.post(
            "/internal/board/post",
            headers={"Host": "evil.example:19920", "Authorization": f"Bearer {mgr.get_internal_token()}"},
            json={"swarmId": sid, "sender": "w", "summary": "hi"},
        )
        self.assertEqual(r_bad_host.status_code, 403)
        r_good_post = client.post(
            "/internal/board/post",
            headers={"Authorization": f"Bearer {mgr.get_internal_token()}"},
            json={"conversationId": "conv-coord-1", "sender": "worker-ui", "kind": "handoff", "summary": "@coordinator ready"},
        )
        self.assertEqual(r_good_post.status_code, 200)
        self.assertEqual(r_good_post.json()["summary"], "@coordinator ready")

        # 6) SSE /board/events requires scope + initial snapshot + wake replay via ?since=
        self.assertEqual(client.get("/board/events").status_code, 400)
        mgr._emit_board_event(sid, "coordinator_wake", {"seq": 5, "summary": "wake replay"})
        with client.stream("GET", "/board/events?conversationId=conv-coord-1&since=1") as resp:
            self.assertEqual(resp.status_code, 200)
            lines: list[str] = []
            for raw_ln in resp.iter_lines():
                if raw_ln:
                    lines.append(raw_ln)
                if any("event: coordinator_wake" in l for l in lines) and len(lines) >= 6:
                    break
            joined = "\n".join(lines)
            self.assertIn("event: snapshot", joined)
            self.assertIn("event: coordinator_wake", joined)

        # 7) Pre-swarm SSE /board/events?conversationId=<late-conv> connects cleanly BEFORE swarm exists,
        #    emits swarm: null snapshot, stays alive, and transitions seamlessly once the swarm is created.
        base_url = mcp_srv._http_ctrl.start(host="127.0.0.1", port=0)
        try:
            import httpx

            async def _test_late_sse() -> None:
                late_conv = "conv-late-sse-1"
                late_coord = "conv-late-coord-1"
                seen: list[tuple[str, dict]] = []
                got_null = asyncio.Event()
                got_populated = asyncio.Event()
                got_all = asyncio.Event()

                async def _reader() -> None:
                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        async with hc.stream("GET", f"{base_url}/board/events?conversationId={late_conv}") as r_sse:
                            assert r_sse.status_code == 200
                            ev_name = ""
                            async for ln in r_sse.aiter_lines():
                                if ln.startswith("event: "):
                                    ev_name = ln[7:].strip()
                                elif ln.startswith("data: "):
                                    payload = json.loads(ln[6:])
                                    seen.append((ev_name, payload))
                                    if ev_name == "snapshot" and payload.get("swarm") is None:
                                        got_null.set()
                                    elif ev_name == "snapshot" and payload.get("swarm") == late_conv:
                                        got_populated.set()
                                    if (
                                        any(e == "update" for e, _ in seen)
                                        and any(e == "coordinator_wake" for e, _ in seen)
                                        and any(e == "primary_wake" for e, _ in seen)
                                    ):
                                        got_all.set()
                                        return

                reader_task = asyncio.create_task(_reader())
                await asyncio.wait_for(got_null.wait(), timeout=5.0)
                null_snap = next(p for e, p in seen if e == "snapshot" and p.get("swarm") is None)
                self.assertEqual(null_snap, {"conversationId": late_conv, "swarm": None, "member": None})

                # Now create the swarm for late_conv while SSE is open
                mgr.store.upsert_meta(
                    late_conv,
                    title="Late SSE Swarm",
                    workspace=str(self.repo_dir),
                    primary_conversation_id=late_conv,
                    coordinator_conversation_id=late_coord,
                )
                await asyncio.wait_for(got_populated.wait(), timeout=5.0)

                # Trigger update + coordinator_wake + primary_wake
                mgr.board_service.post_message(
                    sender="worker-late",
                    summary="@coordinator [done] late sse check",
                    swarm_id=late_conv,
                )
                mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    summary="@primary Escalate late sse",
                    msg_type="escalation",
                    swarm_id=late_conv,
                )
                await asyncio.wait_for(got_all.wait(), timeout=5.0)
                await reader_task
                ev_types = [e for e, _ in seen]
                self.assertIn("snapshot", ev_types)
                self.assertIn("update", ev_types)
                self.assertIn("coordinator_wake", ev_types)
                self.assertIn("primary_wake", ev_types)

            asyncio.run(_test_late_sse())
        finally:
            mcp_srv._http_ctrl.stop()

    def test_exact_7_tool_mcp_surface_and_workflow(self) -> None:
        async def _run() -> None:
            tools = await mcp_srv.mcp.list_tools()
            tool_names = {t.name for t in tools}
            self.assertEqual(
                tool_names,
                {
                    "swarm_catalog",
                    "swarm_workspace",
                    "swarm_manage",
                    "swarm_worker",
                    "swarm_board_post",
                    "swarm_board_read",
                    "swarm_wait",
                },
            )
            self.assertEqual(len(tools), 7)

            # Catalog
            cat = json.loads(await mcp_srv.swarm_catalog())
            self.assertIn("providers", cat)

            # Workspace + project-local catalog discovery
            (self.repo_dir / ".agents" / "skills" / "mcp-proj-skill").mkdir(parents=True, exist_ok=True)
            (self.repo_dir / ".agents" / "skills" / "mcp-proj-skill" / "SKILL.md").write_text("# MCP Proj\n", encoding="utf-8")
            ws_set = json.loads(await mcp_srv.swarm_workspace("set", workdir=str(self.repo_dir)))
            self.assertEqual(Path(ws_set["workdir"]), self.repo_dir.resolve())
            cat_ws = json.loads(await mcp_srv.swarm_catalog())
            self.assertIn("mcp-proj-skill", cat_ws["project_skills"])
            self.assertIn("mcp-proj-skill", cat_ws["enabled_skills"])
            gs = json.loads(await mcp_srv.swarm_workspace("git_status"))
            self.assertTrue(gs["ok"])

            # Swarm manage init -> title -> bind_coordinator -> set_plan -> channel -> status -> clear_finished
            created = json.loads(await mcp_srv.swarm_manage(
                "init",
                title="MCP Swarm",
                mission="Verify 7 tools",
                primary_conversation_id="conv-p-mcp",
            ))
            self.assertEqual(created["swarm_id"], "conv-p-mcp")
            self.assertIn("sidecar://hermes-swarm/swarm_board/", created["open_board"])

            bound = json.loads(await mcp_srv.swarm_manage(
                "bind_coordinator",
                coordinator_conversation_id="conv-c-mcp",
            ))
            self.assertEqual(bound["coordinator_conversation_id"], "conv-c-mcp")

            planned = json.loads(await mcp_srv.swarm_manage(
                "set_plan",
                plan={"goal": {"title": "MCP Swarm", "line": "Verify 7 tools"}, "threads": [], "tasks": {}},
            ))
            self.assertEqual(planned["plan"]["goal"]["title"], "MCP Swarm")

            renamed = json.loads(await mcp_srv.swarm_manage(
                "title",
                title="MCP Swarm Renamed",
                primary_conversation_id="conv-p-mcp",
            ))
            self.assertEqual(renamed["meta"]["title"], "MCP Swarm Renamed")

            ch_res = json.loads(await mcp_srv.swarm_manage(
                "channel",
                op="create",
                channel="verify",
                topic="Verification",
            ))
            self.assertEqual(ch_res["channel"]["name"], "#verify")

            st_res = json.loads(await mcp_srv.swarm_manage("status"))
            self.assertEqual(st_res["swarm_id"], "conv-p-mcp")
            self.assertEqual(st_res["meta"]["title"], "MCP Swarm Renamed")

            cf_res = json.loads(await mcp_srv.swarm_manage("clear_finished"))
            self.assertEqual(cf_res["swarm_id"], "conv-p-mcp")

            # Removed multi-swarm switching actions must be rejected
            with self.assertRaises(ValueError):
                await mcp_srv.swarm_manage("select", swarm_id="conv-p-mcp")
            with self.assertRaises(ValueError):
                await mcp_srv.swarm_manage("archive", swarm_id="conv-p-mcp")

            # Scope claim/release + board post/read/wait
            claimed = json.loads(await mcp_srv.swarm_board_read(
                scope_action="claim",
                scope="app.py",
                holder="worker-1",
            ))
            self.assertEqual(claimed["claimed"]["holder"], "worker-1")

            posted = json.loads(await mcp_srv.swarm_board_post(
                sender="worker-1",
                kind="handoff",
                summary="@coordinator [done] Verified app.py",
            ))
            waited = json.loads(await mcp_srv.swarm_wait(
                since=posted["seq"] - 1,
                mentions_only=True,
                timeout_seconds=1.0,
            ))
            self.assertTrue(waited["woken"])
            self.assertEqual(waited["messages"][0]["seq"], posted["seq"])

            # Parallel session on same workspace stays isolated
            created2 = json.loads(await mcp_srv.swarm_manage(
                "init",
                title="MCP Swarm 2",
                primary_conversation_id="conv-p-mcp-2",
            ))
            self.assertEqual(created2["swarm_id"], "conv-p-mcp-2")
            await mcp_srv.swarm_board_post(
                conversation_id="conv-p-mcp-2",
                sender="worker-2",
                summary="Only in conv-p-mcp-2",
            )
            read1 = json.loads(await mcp_srv.swarm_board_read(conversation_id="conv-p-mcp"))
            read2 = json.loads(await mcp_srv.swarm_board_read(conversation_id="conv-p-mcp-2"))
            self.assertFalse(any(m["summary"] == "Only in conv-p-mcp-2" for m in read1["messages"]))
            self.assertEqual([m["summary"] for m in read2["messages"]], ["Only in conv-p-mcp-2"])

        asyncio.run(_run())

    def test_build_observer_snapshot_plan_synthesis_and_narrator_view(self) -> None:
        mgr = mcp_srv._swarm_mgr
        sid = "snap-synth"
        mgr.store.upsert_meta(
            sid,
            title="Snapshot Synth",
            workspace=str(self.repo_dir),
            primary_conversation_id="conv-p-synth",
            coordinator_conversation_id="conv-c-synth",
        )
        mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", swarm_id=sid)
        mgr.board_service.upsert_member(
            "w-done",
            role="worker",
            state="waiting",
            assignment="[TASK]\n**Implement** API schema.\nExtra instructions.",
            swarm_id=sid,
        )
        mgr.board_service.upsert_member("w-killed", role="worker", state="killed", killed=True, assignment="Old lane", swarm_id=sid)
        mgr.board_service.upsert_member("w-working", role="worker", state="working", assignment="Build UI components", swarm_id=sid)
        mgr.board_service.upsert_member("w-failed", role="worker", state="failed", assignment="Run integration tests", swarm_id=sid)

        mgr.board_service.post_message(
            sender="w-done",
            summary="@coordinator [done] API schema ready",
            swarm_id=sid,
        )

        rt = mgr.get_runtime(sid)
        with rt._state_rlock:
            rt._tasks["task-working"] = {
                "id": "task-working",
                "contextId": "ctx-w",
                "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                "metadata": {
                    "workerConfig": {"worker_name": "w-working", "role": "worker"},
                    "liveActivity": {"toolCalls": 3, "lastTool": "write_file", "lastTextPreview": "editing"},
                },
                "artifacts": [],
                "history": [],
            }
            rt._tasks["task-failed"] = {
                "id": "task-failed",
                "contextId": "ctx-f",
                "status": {"state": models.TaskState.FAILED.value, "timestamp": models.now_iso()},
                "metadata": {
                    "workerConfig": {"worker_name": "w-failed", "role": "worker"},
                    "liveActivity": {"toolCalls": 1, "lastError": "Timeout on DB"},
                },
                "artifacts": [],
                "history": [],
            }

        # Empty list plan.tasks + threads referencing worker names -> synthesized dict
        mgr.board_service.set_plan(
            {
                "goal": {"title": "Ship", "line": "Verify synthesis"},
                "threads": [{"id": "t1", "title": "Core", "tasks": ["w-done", "w-killed", "w-working", "w-failed"]}],
                "tasks": [],
            },
            swarm_id=sid,
        )

        # No narrator yet -> narratorState == "absent", no view.json -> view.exists is False
        snap0 = http_server.build_observer_snapshot(mgr, sid)
        self.assertEqual(snap0["narratorState"], "absent")
        self.assertFalse(snap0["view"]["exists"])
        tasks_map = snap0["plan"]["tasks"]
        self.assertIsInstance(tasks_map, dict)
        self.assertEqual(set(tasks_map.keys()), {"w-done", "w-killed", "w-working", "w-failed"})
        self.assertEqual(tasks_map["w-done"]["status"], "done")
        self.assertEqual(tasks_map["w-done"]["assignee"], "w-done")
        self.assertEqual(tasks_map["w-done"]["title"], "Implement API schema")
        self.assertEqual(tasks_map["w-killed"]["status"], "done")
        self.assertEqual(tasks_map["w-working"]["status"], "active")
        self.assertEqual(tasks_map["w-working"]["note"], "Running write_file")
        self.assertEqual(tasks_map["w-failed"]["status"], "failed")
        self.assertEqual(tasks_map["w-failed"]["note"], "Timeout on DB")

        # Write view.json + add narrator member -> view.exists True & narratorState reflects waiting/working/paused/killed
        swarm_dir = mgr.store.paths(sid).swarm_dir
        swarm_dir.mkdir(parents=True, exist_ok=True)
        (swarm_dir / "view.json").write_text(json.dumps({"headline": "All lanes moving", "threads": []}), encoding="utf-8")

        mgr.board_service.upsert_member("narrator", role="narrator", state="waiting", swarm_id=sid)
        snap_wait = http_server.build_observer_snapshot(mgr, sid)
        self.assertTrue(snap_wait["view"]["exists"])
        self.assertEqual(snap_wait["view"]["json"]["headline"], "All lanes moving")
        self.assertGreater(snap_wait["view"]["jsonMtime"], 0)
        self.assertEqual(snap_wait["narratorState"], "waiting")

        with rt._state_rlock:
            rt._tasks["task-narr"] = {
                "id": "task-narr",
                "contextId": "ctx-n",
                "status": {"state": models.TaskState.FAILED.value, "timestamp": models.now_iso()},
                "metadata": {
                    "workerConfig": {"worker_name": "narrator", "role": "narrator"},
                    "liveActivity": {"toolCalls": 0, "lastError": "Quota exceeded", "endedAt": models.now_iso()},
                },
                "artifacts": [],
                "history": [],
            }
        snap_paused = http_server.build_observer_snapshot(mgr, sid)
        self.assertEqual(snap_paused["narratorState"], "paused")
        self.assertEqual(snap_paused["narrator"]["reason"], "quota")

        mgr.board_service.upsert_member("narrator", role="narrator", state="killed", killed=True, swarm_id=sid)
        snap_killed = http_server.build_observer_snapshot(mgr, sid)
        self.assertEqual(snap_killed["narratorState"], "killed")

    def test_live_telemetry_rollup_subagents_and_mcp_status(self) -> None:
        import sqlite3

        hermes_home = self.tmp_root / "hermes_home_tel"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "context_length_cache.yaml").write_text(
            "context_lengths:\n  tel-model-1: 100000\n",
            encoding="utf-8",
        )
        db_path = hermes_home / "state.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT)"
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "hs-tel-1",
                None,
                "cli",
                1200,
                300,
                400,
                50,
                80,
                0.012,
                0.015,
                "tel-model-1",
                json.dumps({"_usage_anchor": {"prompt_tokens": 1200, "completion_tokens": 300}}),
                4,
                16,
                1000.0,
                1005.0,
                "test-prov",
                "https://api.example.invalid/v1",
            ),
        )
        con.commit()
        con.close()

        gate_file = self.tmp_root / "tel_gate.flag"
        tel_script = self.tmp_root / "fake_tel_worker.py"
        tel_script.write_text(
            "\n".join([
                "import json, os, sys, time",
                "from pathlib import Path",
                "gate = Path(os.environ['TEL_GATE_FILE'])",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'tel-model-1', 'session_id': 'hs-tel-1'}), flush=True)",
                "print(json.dumps({'type': 'tool_use', 'name': 'read_file', 'tool_call_id': 'tc-live-1', 'input': {'path': 'src/main.py'}}), flush=True)",
                "deadline = time.monotonic() + 8.0",
                "while not gate.exists() and time.monotonic() < deadline:",
                "    time.sleep(0.02)",
                "print(json.dumps({'type': 'tool_result', 'name': 'read_file', 'tool_call_id': 'tc-live-1', 'is_error': False, 'output': 'ok', 'duration_ms': 15}), flush=True)",
                "for idx in range(2, 16):",
                "    tc = f'tc-live-{idx}'",
                "    print(json.dumps({'type': 'tool_use', 'name': 'grep_search', 'tool_call_id': tc, 'input': {'query': f'q{idx}'}}), flush=True)",
                "    print(json.dumps({'type': 'tool_result', 'name': 'grep_search', 'tool_call_id': tc, 'is_error': False, 'output': 'hit', 'duration_ms': 5}), flush=True)",
                "sub_trace = [{'tool': f'sub_tool_{j}', 'input_summary': {'path': f'f{j}.py'}, 'status': 'ok', 'duration_ms': 10 + j} for j in range(7)]",
                "deleg_out = json.dumps({'results': [",
                "    {'task_index': 0, 'status': 'completed', 'model': 'leaf-m1', 'api_calls': 3, 'duration_seconds': 2.0, 'tokens': {'input': 200, 'output': 100, 'total': 300}, 'tool_trace': sub_trace, 'summary': 'leaf 1 ok'},",
                "    {'task_index': 1, 'status': 'completed', 'model': 'leaf-m2', 'api_calls': 2, 'duration_seconds': 1.0, 'tokens': {'input': 100, 'output': 50, 'total': 150}, 'tool_trace': sub_trace[:3], 'summary': 'leaf 2 ok'},",
                "]})",
                "print(json.dumps({'type': 'tool_use', 'name': 'delegate_task', 'tool_call_id': 'tc-deleg', 'input': {'tasks': [{'goal': 'sub A', 'model': 'leaf-m1'}, {'goal': 'sub B', 'model': 'leaf-m2'}]}}), flush=True)",
                "print(json.dumps({'type': 'tool_result', 'name': 'delegate_task', 'tool_call_id': 'tc-deleg', 'is_error': False, 'output': deleg_out, 'duration_ms': 3000}), flush=True)",
                "print(json.dumps({'type': 'text', 'text': 'TELEMETRY_DONE'}), flush=True)",
                "print(json.dumps({'type': 'result', 'session_id': 'hs-tel-1', 'exit_code': 0, 'duration_ms': 2000, 'cost_usd': 0.025, 'text': 'TELEMETRY_DONE', 'tokens': {'input': 1500, 'output': 500, 'total': 2000}}), flush=True)",
            ]),
            encoding="utf-8",
        )

        mgr = mcp_srv._swarm_mgr
        sid = "conv-tel-p"
        coord_conv = "conv-tel-c"
        mgr.store.upsert_meta(
            sid,
            title="Telemetry Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", swarm_id=sid)
        mgr.board_service.upsert_member("killed-w", role="worker", state="killed", killed=True, swarm_id=sid)
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)

        async def _run() -> None:
            import httpx

            rt = mgr.get_runtime(sid)
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home), "TEL_GATE_FILE": str(gate_file)}):
                with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(tel_script)], str(self.repo_dir))):
                    await rt.spawn_workers(
                        name="tel-w",
                        prompt="Run telemetry tools",
                        workdir=str(self.repo_dir),
                        model="tel-model-1",
                        return_immediately=True,
                    )

                    # Poll GET /board while worker is paused mid-tool -> verify live currentTool & activeWorkers
                    live_snap: dict | None = None
                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        for _ in range(100):
                            r = await hc.get(f"{base_url}/board?conversationId={coord_conv}")
                            cand = r.json()
                            mem = next((m for m in cand.get("members", []) if m["name"] == "tel-w"), None)
                            if mem and mem.get("currentTool"):
                                live_snap = cand
                                break
                            await asyncio.sleep(0.05)

                        self.assertIsNotNone(live_snap, "Expected live currentTool in GET /board while worker active")
                        assert live_snap is not None
                        live_mem = next(m for m in live_snap["members"] if m["name"] == "tel-w")
                        self.assertEqual(live_mem["status"], "working")
                        self.assertEqual(live_mem["currentTool"]["name"], "read_file")
                        self.assertIn("main.py", live_mem["currentTool"]["preview"])
                        self.assertTrue(live_mem["currentTool"]["startedAt"])
                        self.assertEqual(live_snap["telemetry"]["activeWorkers"], 1)
                        self.assertEqual(live_snap["telemetry"]["killedWorkers"], 1)
                        live_etag = live_snap["etag"]

                        # Release gate and wait for worker completion
                        gate_file.write_text("go", encoding="utf-8")
                        for _ in range(120):
                            if not rt.is_worker_busy("tel-w"):
                                break
                            await asyncio.sleep(0.05)
                        self.assertFalse(rt.is_worker_busy("tel-w"))

                        final_snap = (await hc.get(f"{base_url}/board?conversationId={coord_conv}")).json()

                    self.assertNotEqual(final_snap["etag"], live_etag)
                    fin_mem = next(m for m in final_snap["members"] if m["name"] == "tel-w")
                    self.assertEqual(fin_mem["status"], "waiting")
                    self.assertIsNone(fin_mem["currentTool"])
                    self.assertGreaterEqual(fin_mem["steps"], 2)
                    self.assertEqual(fin_mem["toolCalls"], 16)
                    # Ring buffer <= 12
                    self.assertEqual(len(fin_mem["recentTools"]), 12)
                    self.assertEqual(fin_mem["recentTools"][-1]["name"], "delegate_task")
                    self.assertEqual(fin_mem["recentTools"][0]["name"], "grep_search")

                    # Token & context fields (result + state.db merge)
                    tok = fin_mem["tokens"]
                    for tkey in ("input", "output", "cacheRead", "cacheWrite", "reasoning", "total", "contextUsed", "contextLimit", "contextPct"):
                        self.assertIn(tkey, tok)
                    self.assertEqual(tok["input"], 1500)
                    self.assertEqual(tok["output"], 500)
                    self.assertEqual(tok["cacheRead"], 400)
                    self.assertEqual(tok["cacheWrite"], 50)
                    self.assertEqual(tok["reasoning"], 80)
                    self.assertEqual(tok["total"], 2450)
                    self.assertEqual(tok["new"], 2000)
                    self.assertEqual(tok["contextUsed"], 1500)
                    self.assertEqual(tok["contextLimit"], 100000)
                    self.assertAlmostEqual(tok["contextPct"], 1.5, places=1)
                    self.assertEqual(fin_mem["elapsedMs"], 2000)
                    self.assertAlmostEqual(fin_mem["tps"], 250.0, places=1)
                    self.assertAlmostEqual(fin_mem["costUsd"], 0.025, places=5)

                    # Tier-3 subagents telemetry (recentTools <= 5)
                    subs = fin_mem["subagents"]
                    self.assertEqual(len(subs), 2)
                    self.assertEqual(subs[0]["steps"], 3)
                    self.assertEqual(subs[0]["toolCalls"], 7)
                    self.assertEqual(len(subs[0]["recentTools"]), 5)
                    self.assertEqual(subs[0]["tokens"]["input"], 200)
                    self.assertEqual(subs[0]["tokens"]["output"], 100)
                    self.assertEqual(subs[0]["tokens"]["total"], 300)
                    self.assertEqual(subs[0]["tokens"]["totalProcessed"], 300)
                    self.assertAlmostEqual(subs[0]["tps"], 50.0, places=1)
                    self.assertEqual(subs[0]["elapsedMs"], 2000)
                    self.assertEqual(subs[0]["summary"], "leaf 1 ok")

                    # Swarm telemetry rollup in GET /board
                    tel = final_snap["telemetry"]
                    for rkey in (
                        "activeWorkers",
                        "waitingWorkers",
                        "killedWorkers",
                        "subagentCount",
                        "totalSteps",
                        "totalToolCalls",
                        "totalTokens",
                        "avgTps",
                        "liveTps",
                        "totalCostUsd",
                    ):
                        self.assertIn(rkey, tel)
                    self.assertEqual(tel["activeWorkers"], 0)
                    self.assertEqual(tel["waitingWorkers"], 1)
                    self.assertEqual(tel["killedWorkers"], 1)
                    self.assertEqual(tel["subagentCount"], 2)
                    self.assertEqual(tel["totalToolCalls"], 16)
                    self.assertEqual(tel["totalTokens"], 2900)
                    self.assertEqual(tel["totalProcessedTokens"], 2900)
                    self.assertAlmostEqual(tel["avgTps"], 130.0, places=1)
                    self.assertEqual(tel["liveTps"], 0.0)
                    self.assertAlmostEqual(tel["totalCostUsd"], 0.025, places=5)

                    # Identical telemetry rollup in MCP swarm_manage(status) and swarm_worker(status)
                    m_status = json.loads(await mcp_srv.swarm_manage("status", swarm_id=sid))
                    self.assertEqual(m_status["telemetry"], tel)
                    w_status_all = json.loads(await mcp_srv.swarm_worker("status", swarm_id=sid))
                    self.assertEqual(w_status_all["telemetry"], tel)
                    w_status_one = json.loads(await mcp_srv.swarm_worker("status", name="tel-w", swarm_id=sid))
                    self.assertEqual(w_status_one["telemetry"], tel)
                    self.assertEqual(w_status_one["member"]["toolCalls"], 16)
                    self.assertEqual(len(w_status_one["member"]["subagents"]), 2)

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()


class RuntimeWorktreeAndClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-rt-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)
        self._orig_state = os.environ.get("HERMES_A2A_STATE_DIR")
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)

    def tearDown(self) -> None:
        if self._orig_state is None:
            os.environ.pop("HERMES_A2A_STATE_DIR", None)
        else:
            os.environ["HERMES_A2A_STATE_DIR"] = self._orig_state
        self._tmp.cleanup()

    def test_worktree_lifecycle_and_runtime_contracts(self) -> None:
        async def _run() -> None:
            wt = await worktree_mod.create_worktree(self.repo_dir, "worker-alpha")
            wt_path = Path(wt.worktree_path)
            self.assertTrue(wt_path.is_dir())
            (wt_path / "feature.txt").write_text("ok\n", encoding="utf-8")
            diff = await worktree_mod.inspect_git_diff(wt_path)
            self.assertTrue(diff["ok"])
            self.assertIn("feature.txt", diff["untracked"])
            pruned = await worktree_mod.prune_worktrees(self.repo_dir, active_ids=set())
            self.assertTrue(pruned["pruned"])
            self.assertFalse(wt_path.exists())

            # Runtime sharded spawn, switch_model contextId preservation, kill_worker
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            rt = mgr.get_runtime("rt-swarm")

            async def _fake_exec(task_id: str, full_prompt: str, cfg: models.WorkerConfig, resume_sid: str | None) -> None:
                with rt._state_rlock:
                    t = rt._tasks[task_id]
                    t["status"] = {"state": models.TaskState.COMPLETED.value, "timestamp": models.now_iso()}
                    t["artifacts"] = [models.new_artifact("response", [models.text_part(f"ok:{cfg.model}")])]
                    rt._context_sessions[t["contextId"]] = f"hermes-sess-{task_id[-4:]}"
                    ev = rt._events.pop(task_id, None)
                    if ev:
                        ev.set()

            with patch.object(rt, "_execute_worker", side_effect=_fake_exec):
                spawned = await rt.spawn_workers(
                    name="impl",
                    prompt="Implement slice",
                    tasks=["slice A", "slice B"],
                    workdir=str(self.repo_dir),
                    model="model-a",
                    return_immediately=False,
                )
                self.assertEqual(spawned["count"], 2)
                self.assertEqual([w["name"] for w in spawned["workers"]], ["impl-1", "impl-2"])
                orig_ctx = spawned["workers"][0]["contextId"]

                sw = await rt.switch_model(
                    "impl-1",
                    model="model-b",
                    provider="alt-prov",
                    return_immediately=False,
                )
                self.assertTrue(sw["switched"])
                self.assertEqual(sw["contextId"], orig_ctx)
                self.assertEqual(sw["model"], "model-b")

                mgr.board_service.claim_scope(scope="slice-a/", holder="impl-1", swarm_id="rt-swarm")
                killed = await rt.kill_worker("impl-1", reason="done")
                self.assertTrue(killed["killed"])
                self.assertIn("slice-a/", killed["releasedScopes"])

            # Preamble hygiene & pre-bound CLI
            preamble = runtime_mod.build_worker_preamble(
                models.WorkerConfig(role="worker", worker_name="w1"),
                swarm_id="rt-swarm",
                task_id="t-1",
                context_id="c-1",
                board_dir=mgr.store.paths("rt-swarm").swarm_dir,
                http_url="http://127.0.0.1:9910",
            )
            self.assertIn("board_cli.py", preamble)
            self.assertIn("@coordinator", preamble)

            # A2A client helpers
            headers = a2a_http_client.build_auth_headers({"auth": {"type": "bearer", "token": "abc"}})
            self.assertEqual(headers["Authorization"], "Bearer abc")
            self.assertEqual(
                a2a_http_client.select_jsonrpc_url(
                    {"supportedInterfaces": [{"protocolBinding": "JSONRPC", "url": "http://x/rpc"}]},
                    "http://fallback",
                ),
                "http://x/rpc",
            )
            self.assertIn("creationflags" if sys.platform == "win32" else "start_new_session", win_launcher.hidden_subprocess_kwargs())

            # Fake subprocess stream-json execution + Tier-3 delegate_task subagent tracking (no wake events)
            fake_script = self.tmp_root / "fake_hermes_worker.py"
            fake_script.write_text(
                "\n".join([
                    "import json, sys",
                    "events = [",
                    "    {'type': 'system', 'subtype': 'init', 'model': 'mock-m', 'session_id': 'hs-123'},",
                    "    {'type': 'tool_use', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-1', 'input': {'goal': 'check leaf', 'model': 'leaf-m'}},",
                    "    {'type': 'tool_result', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-1', 'is_error': False, 'output': 'LEAF_OK', 'duration_ms': 12},",
                    "    {'type': 'text', 'text': 'WORKER_DONE'},",
                    "    {'type': 'result', 'session_id': 'hs-123', 'exit_code': 0, 'text': 'WORKER_DONE', 'tokens': {'input_tokens': 10, 'output_tokens': 5}},",
                    "]",
                    "for ev in events:",
                    "    print(json.dumps(ev), flush=True)",
                ]),
                encoding="utf-8",
            )
            mgr2 = runtime_mod.SwarmManager(state_dir=self.state_dir)
            wake_events_seen: list[str] = []
            mgr2.board_service.add_listener(lambda _s, et, _p: wake_events_seen.append(et))
            rt2 = mgr2.get_runtime("subagent-trace")

            with patch.object(rt2, "_build_hermes_command", return_value=([sys.executable, str(fake_script)], str(self.repo_dir))):
                res = await rt2.spawn_workers(
                    name="lead-w",
                    prompt="Run delegate task",
                    workdir=str(self.repo_dir),
                    return_immediately=False,
                    wait_seconds=15.0,
                )
            self.assertEqual(res["tasks"][0]["status"]["state"], models.TaskState.COMPLETED.value)
            b_doc = mgr2.board_service.read_board("subagent-trace")
            subs = b_doc["members"]["lead-w"]["subagents"]
            self.assertEqual(len(subs), 1)
            self.assertEqual(subs[0]["role"], "subagent")
            self.assertEqual(subs[0]["tier"], 3)
            self.assertEqual(subs[0]["state"], "done")
            self.assertFalse(any(et in ("coordinator_wake", "primary_wake") for et in wake_events_seen))

        asyncio.run(_run())

    def test_resume_preserves_assignment_and_kill_worker_sticky_across_roles(self) -> None:
        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "assign-and-kill"
            rt = mgr.get_runtime(sid)

            async def _fake_exec_done(task_id: str, full_prompt: str, cfg: models.WorkerConfig, resume_sid: str | None) -> None:
                with rt._state_rlock:
                    t = rt._tasks[task_id]
                    t["status"] = {"state": models.TaskState.COMPLETED.value, "timestamp": models.now_iso()}
                    t["artifacts"] = [models.new_artifact("response", [models.text_part("ok")])]
                    rt._context_sessions[t["contextId"]] = f"hs-{task_id[-4:]}"
                    ev = rt._events.pop(task_id, None)
                    if ev:
                        ev.set()
                mgr.board_service.upsert_member(
                    cfg.worker_name or "worker",
                    role=cfg.role,
                    state=models.MemberState.WAITING.value,
                    task_id=task_id,
                    context_id=rt._tasks[task_id]["contextId"],
                    swarm_id=sid,
                )

            with patch.object(rt, "_execute_worker", side_effect=_fake_exec_done):
                await rt.spawn_workers(
                    name="backend-dev",
                    prompt="Implement auth middleware and token refresh",
                    workdir=str(self.repo_dir),
                    model="model-a",
                    return_immediately=False,
                )
                board_init = mgr.board_service.read_board(sid)
                orig_assign = board_init["members"]["backend-dev"]["assignment"]
                self.assertEqual(orig_assign, "Implement auth middleware and token refresh")

                # Follow-up @mention resume, switch_model, and wake_worker_from_board must NEVER overwrite assignment
                await rt.resume_worker(
                    "backend-dev",
                    "[Board @mention wakeup #2] Fix edge case in token refresh",
                    return_immediately=False,
                )
                self.assertEqual(mgr.board_service.read_board(sid)["members"]["backend-dev"]["assignment"], orig_assign)

                await rt.switch_model(
                    "backend-dev",
                    model="model-b",
                    prompt="Switch model wakeup",
                    return_immediately=False,
                )
                self.assertEqual(mgr.board_service.read_board(sid)["members"]["backend-dev"]["assignment"], orig_assign)

                woke = await rt.wake_worker_from_board("backend-dev", "[Board @mention wakeup #3] Second ping")
                self.assertTrue(woke)
                await asyncio.sleep(0.05)
                self.assertEqual(mgr.board_service.read_board(sid)["members"]["backend-dev"]["assignment"], orig_assign)

            # Role workers (narrator, monitor) + worker: kill_worker sets state=='killed' and killed==True
            # and sticky protection prevents late wait/upsert from reverting to working/waiting.
            block_ev = asyncio.Event()

            async def _fake_exec_blocking(task_id: str, full_prompt: str, cfg: models.WorkerConfig, resume_sid: str | None) -> None:
                try:
                    await block_ev.wait()
                except asyncio.CancelledError:
                    raise

            with patch.object(rt, "_execute_worker", side_effect=_fake_exec_blocking):
                await rt.spawn_workers(name="narrator", role="narrator", prompt="Maintain view.json", workdir=str(self.repo_dir), return_immediately=True)
                await rt.spawn_workers(name="monitor", role="monitor", prompt="Watch stalls", workdir=str(self.repo_dir), return_immediately=True)
                await rt.spawn_workers(name="worker-z", role="worker", prompt="Build feature Z", workdir=str(self.repo_dir), return_immediately=True)
                await asyncio.sleep(0.05)

                for role_name in ("narrator", "monitor", "worker-z"):
                    res_k = await rt.kill_worker(role_name, reason="shutdown")
                    self.assertTrue(res_k["killed"])

                # Simulate late board_cli wait / upsert_member updates after kill
                mgr.board_service.set_member_wait("narrator", waiting=False, swarm_id=sid)
                mgr.board_service.set_member_wait("monitor", waiting=True, swarm_id=sid)
                mgr.board_service.upsert_member("worker-z", state="working", swarm_id=sid)
                mgr.board_service.upsert_member("narrator", state="waiting", swarm_id=sid)

                board_on_disk = json.loads(mgr.store.paths(sid).board_file.read_text(encoding="utf-8"))
                for role_name in ("narrator", "monitor", "worker-z"):
                    mem = board_on_disk["members"][role_name]
                    self.assertEqual(mem["state"], "killed")
                    self.assertIs(mem["killed"], True)
                    self.assertIsNone(mem["waitSince"])

                with self.assertRaises(RuntimeError):
                    await rt.resume_worker("narrator", "wake")
                with self.assertRaises(RuntimeError):
                    await rt.switch_model("monitor", model="model-c")
                self.assertFalse(await rt.wake_worker_from_board("worker-z", "wake"))

                snap = http_server.build_observer_snapshot(mgr, sid)
                self.assertEqual(snap["narratorState"], "killed")
                by_name = {m["name"]: m for m in snap["members"]}
                for role_name in ("narrator", "monitor", "worker-z"):
                    self.assertEqual(by_name[role_name]["status"], "killed")
                    self.assertIs(by_name[role_name]["killed"], True)

        asyncio.run(_run())

    def test_cleanup_leftover_worktree_dir_on_remove_and_prune(self) -> None:
        async def _run() -> None:
            # 1) Direct retry test when Windows file handle transiently fails os.rmdir twice
            leftover = self.repo_dir / ".worktrees" / "a2a-transient-empty"
            leftover.mkdir(parents=True, exist_ok=True)
            real_rmdir = os.rmdir
            attempts_seen = {"n": 0}

            def _flaky_rmdir(path: Path | str) -> None:
                attempts_seen["n"] += 1
                if attempts_seen["n"] <= 2:
                    raise PermissionError("simulated transient Windows handle lock")
                real_rmdir(path)

            with patch("worktree.os.rmdir", side_effect=_flaky_rmdir):
                cleaned = await worktree_mod._cleanup_leftover_worktree_dir(leftover, attempts=5, base_delay=0.01)
            self.assertTrue(cleaned)
            self.assertFalse(leftover.exists())
            self.assertEqual(attempts_seen["n"], 3)

            # 2) remove_worktree cleans up leftover empty directory left after git worktree remove
            wt = await worktree_mod.create_worktree(self.repo_dir, "worker-rem")
            wt_path = Path(wt.worktree_path)
            self.assertTrue(wt_path.is_dir())
            orig_run_git = worktree_mod._run_git

            async def _run_git_leaving_empty(cwd: str | Path, *git_args: str, timeout: float = 20.0) -> tuple[int, bytes, bytes]:
                res = await orig_run_git(cwd, *git_args, timeout=timeout)
                if len(git_args) >= 2 and git_args[0] == "worktree" and git_args[1] == "remove":
                    wt_path.mkdir(parents=True, exist_ok=True)
                return res

            with patch.object(worktree_mod, "_run_git", side_effect=_run_git_leaving_empty):
                rm_res = await worktree_mod.remove_worktree(self.repo_dir, wt_path, branch=wt.branch)
            self.assertTrue(rm_res["ok"])
            self.assertFalse(wt_path.exists())

            # 3) prune_worktrees removes leftover unlinked empty .worktrees/a2a-* directory
            orphan_dir = self.repo_dir / ".worktrees" / "a2a-orphan-empty"
            orphan_dir.mkdir(parents=True, exist_ok=True)
            pruned = await worktree_mod.prune_worktrees(self.repo_dir, active_ids=set())
            self.assertTrue(pruned["ok"])
            self.assertFalse(orphan_dir.exists())

        asyncio.run(_run())

    def test_process_tree_kill_orphan_board_cli_sweep_and_worktree_removal(self) -> None:
        def _pid_running(pid: int) -> bool:
            if pid <= 0:
                return False
            if sys.platform == "win32":
                import ctypes

                k32 = ctypes.windll.kernel32
                h = k32.OpenProcess(0x1000, False, int(pid))
                if not h:
                    return k32.GetLastError() == 5
                try:
                    code = ctypes.c_ulong()
                    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
                    return bool(ok and code.value == 259)
                finally:
                    k32.CloseHandle(h)
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False

        async def _run() -> None:
            # 1) Real parent + child process tree termination (async & sync)
            child_pid_file = self.tmp_root / "child_pid.txt"
            parent_script = self.tmp_root / "parent_spawner.py"
            parent_script.write_text(
                "\n".join([
                    "import subprocess, sys, time",
                    "from pathlib import Path",
                    "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])",
                    f"Path({str(child_pid_file)!r}).write_text(str(child.pid), encoding='utf-8')",
                    "time.sleep(60)",
                ]),
                encoding="utf-8",
            )
            proc = await win_launcher.spawn_hermes_subprocess([sys.executable, str(parent_script)])
            child_pid = 0
            for _ in range(100):
                if child_pid_file.is_file() and child_pid_file.read_text(encoding="utf-8").strip():
                    child_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                    break
                await asyncio.sleep(0.03)
            self.assertGreater(child_pid, 0)
            self.assertTrue(_pid_running(proc.pid))
            self.assertTrue(_pid_running(child_pid))

            await win_launcher.kill_process_tree(proc)
            await asyncio.sleep(0.15)
            self.assertFalse(_pid_running(proc.pid))
            self.assertFalse(_pid_running(child_pid))

            # Sync variant
            child_pid_file.unlink(missing_ok=True)
            proc_sync = subprocess.Popen(
                [sys.executable, str(parent_script)],
                **win_launcher.hidden_subprocess_kwargs(new_process_group=True),
            )
            child_sync_pid = 0
            for _ in range(100):
                if child_pid_file.is_file() and child_pid_file.read_text(encoding="utf-8").strip():
                    child_sync_pid = int(child_pid_file.read_text(encoding="utf-8").strip())
                    break
                await asyncio.sleep(0.03)
            self.assertGreater(child_sync_pid, 0)
            win_launcher.kill_process_tree_sync(proc_sync.pid)
            proc_sync.wait(timeout=3.0)
            await asyncio.sleep(0.15)
            self.assertFalse(_pid_running(child_sync_pid))

            # 2) kill_worker sweeps orphan `board_cli.py wait` subprocess and unlocks `.worktrees/<name>` for removal
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "orphan-sweep"
            rt = mgr.get_runtime(sid)
            swarm_dir = mgr.store.paths(sid).swarm_dir
            swarm_dir.mkdir(parents=True, exist_ok=True)

            blocking_worker = self.tmp_root / "blocking_wt_worker.py"
            blocking_worker.write_text(
                "import json, sys, time\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-orph'}), flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )
            orphan_cli = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parent.parent / "board_cli.py"),
                    "--board-dir",
                    str(swarm_dir),
                    "--swarm",
                    sid,
                    "--agent",
                    "orphan-w",
                    "wait",
                    "--since",
                    "0",
                    "--timeout",
                    "60",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                **win_launcher.hidden_subprocess_kwargs(new_process_group=True),
            )
            try:
                with patch.object(
                    rt,
                    "_build_hermes_command",
                    side_effect=lambda cfg, **kw: ([sys.executable, str(blocking_worker)], kw.get("effective_workdir") or str(self.repo_dir)),
                ):
                    await rt.spawn_workers(
                        name="orphan-w",
                        prompt="Hold worktree",
                        workdir=str(self.repo_dir),
                        worktree=True,
                        return_immediately=True,
                    )
                    wt_dir = self.repo_dir / ".worktrees" / "a2a-orphan-w"
                    for _ in range(80):
                        if wt_dir.is_dir() and rt.is_worker_busy("orphan-w"):
                            break
                        await asyncio.sleep(0.05)
                    self.assertTrue(wt_dir.is_dir())
                    self.assertIsNone(orphan_cli.poll())

                    kill_res = await rt.kill_worker("orphan-w", reason="test sweep")
                    self.assertTrue(kill_res["killed"])
                    self.assertIn(orphan_cli.pid, kill_res["killedOrphanPids"])
                    try:
                        orphan_cli.wait(timeout=2.0)
                    except Exception:
                        pass
                    self.assertIsNotNone(orphan_cli.poll())
                    self.assertFalse(_pid_running(orphan_cli.pid))

                    rm_res = await worktree_mod.remove_worktree(self.repo_dir, wt_dir, branch="a2a/orphan-w")
                    self.assertTrue(rm_res["ok"])
                    self.assertFalse(wt_dir.exists())
            finally:
                if orphan_cli.poll() is None:
                    win_launcher.kill_process_tree_sync(orphan_cli.pid)
                try:
                    orphan_cli.wait(timeout=2.0)
                except Exception:
                    pass
                for stream in (orphan_cli.stdout, orphan_cli.stderr):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass
                await mgr.shutdown()

        asyncio.run(_run())

    def test_canonical_workspace_never_mutated_to_worktree(self) -> None:
        self.assertEqual(
            Path(runtime_mod._canonical_workspace_path(str(self.repo_dir / ".worktrees" / "a2a-coder")) or "").resolve(),
            self.repo_dir.resolve(),
        )
        sample_root = "C:/proj/repo" if os.name == "nt" else "/proj/repo"
        self.assertEqual(
            runtime_mod._canonical_workspace_path(f"{sample_root}/.worktrees/a2a-coder/sub"),
            str(Path(sample_root)),
        )

        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "ws-canon-swarm"
            mgr.store.upsert_meta(
                sid,
                title="Workspace Canonical",
                workspace=str(self.repo_dir),
                primary_conversation_id="conv-ws-p",
            )
            rt = mgr.get_runtime(sid)

            async def _fake_exec(task_id: str, full_prompt: str, cfg: models.WorkerConfig, resume_sid: str | None) -> None:
                with rt._state_rlock:
                    t = rt._tasks[task_id]
                    t["status"] = {"state": models.TaskState.COMPLETED.value, "timestamp": models.now_iso()}
                    rt._context_sessions[t["contextId"]] = "hs-ws-1"
                    ev = rt._events.pop(task_id, None)
                    if ev:
                        ev.set()

            with patch.object(rt, "_execute_worker", side_effect=_fake_exec):
                await rt.spawn_workers(
                    name="wt-coder",
                    prompt="Work in worktree",
                    workdir=str(self.repo_dir),
                    worktree=True,
                    return_immediately=False,
                )
                wt_path = str(self.repo_dir / ".worktrees" / "a2a-wt-coder")
                await rt.resume_worker("wt-coder", "Continue in worktree", return_immediately=False)

                meta_after = mgr.store.read_meta(sid)
                self.assertEqual(Path(meta_after["workspace"]).resolve(), self.repo_dir.resolve())
                self.assertNotIn(".worktrees", meta_after["workspace"])

                # Even if meta.workspace was polluted with a .worktrees path, send_message heals it
                mgr.store.upsert_meta(sid, workspace=wt_path)
                await rt.send_message(
                    message_parts=[models.text_part("Heal workspace")],
                    config=models.WorkerConfig(worker_name="wt-healer", workdir=wt_path),
                    return_immediately=False,
                )
                healed = mgr.store.read_meta(sid)
                self.assertEqual(Path(healed["workspace"]).resolve(), self.repo_dir.resolve())

            # MCP server tools also canonicalize worktree paths
            orig_mgr = mcp_srv._swarm_mgr
            orig_wd = mcp_srv._session_workdir
            try:
                mcp_srv._swarm_mgr = mgr
                wt_sub = self.repo_dir / ".worktrees" / "a2a-wt-coder"
                wt_sub.mkdir(parents=True, exist_ok=True)
                ws_res = json.loads(await mcp_srv.swarm_workspace("set", workdir=str(wt_sub), swarm_id=sid))
                self.assertEqual(Path(ws_res["workdir"]).resolve(), self.repo_dir.resolve())
                self.assertEqual(Path(ws_res["meta"]["workspace"]).resolve(), self.repo_dir.resolve())
            finally:
                mcp_srv._swarm_mgr = orig_mgr
                mcp_srv._session_workdir = orig_wd
                await worktree_mod.prune_worktrees(self.repo_dir, active_ids=set())
                await mgr.shutdown()

        asyncio.run(_run())

    def test_kill_worker_suppresses_spurious_all_finished_and_narrator_last_read_seq(self) -> None:
        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "kill-suppress-narrator"
            mgr.store.upsert_meta(
                sid,
                title="Kill Suppress & Narrator",
                workspace=str(self.repo_dir),
                primary_conversation_id="conv-ks-p",
                coordinator_conversation_id="conv-ks-c",
            )
            rt = mgr.get_runtime(sid)
            wake_q = mgr.subscribe_wake_events()
            observed_events: list[dict] = []

            async def _drain_wakes() -> None:
                while not wake_q.empty():
                    observed_events.append(wake_q.get_nowait())

            blocking_worker = self.tmp_root / "kill_suppress_worker.py"
            blocking_worker.write_text(
                "import json, sys, time\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-ks'}), flush=True)\n"
                "if '--quick' in sys.argv:\n"
                "    print(json.dumps({'type': 'result', 'session_id': 'hs-ks', 'exit_code': 0, 'text': 'DONE'}), flush=True)\n"
                "else:\n"
                "    time.sleep(60)\n",
                encoding="utf-8",
            )

            try:
                # 1) Explicit kill_worker MUST NOT emit "All active Tier-2 workers ... finished"
                with patch.object(
                    rt,
                    "_build_hermes_command",
                    return_value=([sys.executable, str(blocking_worker)], str(self.repo_dir)),
                ):
                    await rt.spawn_workers(
                        name="w-killed-solo",
                        prompt="Long task",
                        workdir=str(self.repo_dir),
                        return_immediately=True,
                    )
                    for _ in range(50):
                        if rt.is_worker_busy("w-killed-solo"):
                            break
                        await asyncio.sleep(0.03)
                    await rt.kill_worker("w-killed-solo", reason="explicit stop")
                    await asyncio.sleep(0.15)
                    await _drain_wakes()

                self.assertFalse(
                    any(
                        "All active" in str(ev.get("summary") or (ev.get("message") or {}).get("summary") or "")
                        for ev in observed_events
                    ),
                    msg=f"Spurious active->0 coordinator_wake emitted after kill_worker: {observed_events}",
                )

                # Contrast: normal completion DOES emit active->0 coordinator_wake
                observed_events.clear()
                with patch.object(
                    rt,
                    "_build_hermes_command",
                    return_value=([sys.executable, str(blocking_worker), "--quick"], str(self.repo_dir)),
                ):
                    await rt.spawn_workers(
                        name="w-normal-solo",
                        prompt="Quick task",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    await asyncio.sleep(0.1)
                    await _drain_wakes()

                self.assertTrue(
                    any(
                        ev.get("type") == "coordinator_wake"
                        and "All active" in str(ev.get("summary") or (ev.get("message") or {}).get("summary") or "")
                        for ev in observed_events
                    ),
                    msg=f"Expected normal active->0 coordinator_wake, got: {observed_events}",
                )

                # 2) Narrator lastReadSeq advances on view.json update (500ms watcher + snapshot) & turn completion
                mgr.board_service.upsert_member("narrator", role="narrator", state="waiting", swarm_id=sid)
                m1 = mgr.board_service.post_message(sender="w-normal-solo", summary="Msg 1", swarm_id=sid)
                m2 = mgr.board_service.post_message(sender="w-normal-solo", summary="Msg 2", swarm_id=sid)
                self.assertEqual(mgr.board_service.read_board(sid)["members"]["narrator"]["lastReadSeq"], 0)

                swarm_dir = mgr.store.paths(sid).swarm_dir
                view_file = swarm_dir / "view.json"
                view_file.write_text(json.dumps({"headline": "After msg 2", "threads": []}), encoding="utf-8")
                await mgr.mention_router.poll_disk_once([sid])
                self.assertGreaterEqual(
                    mgr.board_service.read_board(sid)["members"]["narrator"]["lastReadSeq"],
                    m2["seq"],
                )

                m3 = mgr.board_service.post_message(sender="w-normal-solo", summary="Msg 3", swarm_id=sid)
                with patch.object(
                    rt,
                    "_build_hermes_command",
                    return_value=([sys.executable, str(blocking_worker), "--quick"], str(self.repo_dir)),
                ):
                    await rt.spawn_workers(
                        name="narrator",
                        role="narrator",
                        prompt="Refresh view",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                self.assertGreaterEqual(
                    mgr.board_service.read_board(sid)["members"]["narrator"]["lastReadSeq"],
                    m3["seq"],
                )

                await asyncio.sleep(0.03)
                m4 = mgr.board_service.post_message(sender="w-normal-solo", summary="Msg 4", swarm_id=sid)
                await asyncio.sleep(0.03)
                view_file.write_text(json.dumps({"headline": "After msg 4", "threads": []}), encoding="utf-8")
                snap = http_server.build_observer_snapshot(mgr, sid)
                self.assertEqual(snap["narrator"]["lastReadSeq"], m4["seq"])
                self.assertEqual(snap["narrator"]["unread"], 0)
            finally:
                mgr.unsubscribe_wake_events(wake_q)
                await mgr.shutdown()

        asyncio.run(_run())

    def test_hermes_swarm_env_injection_and_live_bridge_plugin_e2e(self) -> None:
        import importlib.util

        sibling_plugin_dir = Path(__file__).resolve().parents[2] / "hermes-bridge"
        env_hermes_home = Path(os.environ["HERMES_HOME"]) / "plugins" / "hermes-swarm-bridge" if os.environ.get("HERMES_HOME") else None
        candidates = [
            sibling_plugin_dir,
            *( [env_hermes_home] if env_hermes_home is not None else [] ),
            Path.home() / ".hermes" / "plugins" / "hermes-swarm-bridge",
            Path.home() / "AppData" / "Local" / "hermes" / "plugins" / "hermes-swarm-bridge",
        ]
        plugin_dir = next((cand for cand in candidates if (cand / "__init__.py").is_file()), sibling_plugin_dir)
        self.assertTrue((plugin_dir / "__init__.py").is_file(), f"Missing hermes-swarm-bridge plugin in {candidates}")

        spec = importlib.util.spec_from_file_location(
            "hermes_swarm_bridge_live_test",
            plugin_dir / "__init__.py",
            submodule_search_locations=[str(plugin_dir)],
        )
        assert spec is not None and spec.loader is not None
        plugin_mod = importlib.util.module_from_spec(spec)
        sys.modules["hermes_swarm_bridge_live_test"] = plugin_mod
        spec.loader.exec_module(plugin_mod)

        env_capture_file = self.tmp_root / "captured_swarm_env.json"
        env_script = self.tmp_root / "capture_env_worker.py"
        env_script.write_text(
            "\n".join([
                "import json, os, sys",
                "from pathlib import Path",
                "keys = ['HERMES_SWARM_ID', 'HERMES_SWARM_AGENT', 'HERMES_SWARM_ROLE', 'HERMES_SWARM_BOARD_DIR', 'HERMES_SWARM_HTTP_URL', 'HERMES_SWARM_TOKEN_FILE', 'HERMES_SWARM_CONVERSATION_ID']",
                f"Path({str(env_capture_file)!r}).write_text(json.dumps({{k: os.environ.get(k) for k in keys}}), encoding='utf-8')",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-env-1'}), flush=True)",
                "print(json.dumps({'type': 'result', 'session_id': 'hs-env-1', 'exit_code': 0, 'text': 'ENV_OK'}), flush=True)",
            ]),
            encoding="utf-8",
        )

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-bridge-p"
        coord_conv = "conv-bridge-c"
        mgr.store.upsert_meta(
            sid,
            title="Bridge Integration Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)

        class _Ctx:
            def __init__(self) -> None:
                self.tools: dict[str, dict] = {}
                self.sections: dict[str, object] = {}

            def register_tool(self, *, name, toolset, schema, handler, description="", **kwargs):
                self.tools[name] = {"toolset": toolset, "schema": schema, "handler": handler, "description": description}

            def register_system_prompt_section(self, sid_sec, content, **kwargs):
                self.sections[sid_sec] = content

        async def _run() -> None:
            rt = mgr.get_runtime(sid)
            with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(env_script)], str(self.repo_dir))):
                await rt.spawn_workers(
                    name="bridge-w",
                    role="verifier",
                    prompt="Verify injected env",
                    workdir=str(self.repo_dir),
                    return_immediately=False,
                    wait_seconds=10.0,
                )

            captured = json.loads(env_capture_file.read_text(encoding="utf-8"))
            self.assertEqual(captured["HERMES_SWARM_ID"], sid)
            self.assertEqual(captured["HERMES_SWARM_AGENT"], "bridge-w")
            self.assertEqual(captured["HERMES_SWARM_ROLE"], "verifier")
            self.assertEqual(Path(captured["HERMES_SWARM_BOARD_DIR"]), mgr.store.paths(sid).swarm_dir)
            self.assertEqual(captured["HERMES_SWARM_HTTP_URL"], base_url)
            self.assertTrue(Path(captured["HERMES_SWARM_TOKEN_FILE"]).is_file())
            self.assertEqual(captured["HERMES_SWARM_CONVERSATION_ID"], sid)

            # 1) Plugin STILL registers all 6 native tools without HERMES_SWARM_ID (hermes-gateway compatibility),
            #    while keeping register_system_prompt_section strictly gated on HERMES_SWARM_ID.
            clean_env_keys = (
                "HERMES_SWARM_ID",
                "HERMES_SWARM_AGENT",
                "HERMES_SWARM_ROLE",
                "HERMES_SWARM_BOARD_DIR",
                "HERMES_SWARM_HTTP_URL",
                "HERMES_SWARM_TOKEN_FILE",
                "HERMES_SWARM_HTTP_TOKEN",
                "HERMES_SWARM_CONVERSATION_ID",
                "HERMES_A2A_SWARM_ID",
                "HERMES_A2A_WORKER_NAME",
                "HERMES_A2A_WORKER_ROLE",
                "HERMES_A2A_SWARM_BOARD_DIR",
                "HERMES_A2A_HTTP_URL",
                "HERMES_A2A_TOKEN_FILE",
                "HERMES_A2A_HTTP_TOKEN",
                "HERMES_A2A_CONVERSATION_ID",
            )
            with patch.dict(os.environ, {}, clear=False):
                for ek in clean_env_keys:
                    os.environ.pop(ek, None)
                ctx_off = _Ctx()
                plugin_mod.register(ctx_off)
                self.assertEqual(
                    set(ctx_off.tools.keys()),
                    {"swarm_post", "swarm_read", "swarm_claim", "swarm_release", "swarm_status", "swarm_wait", "swarm_channel"},
                )
                self.assertEqual(ctx_off.sections, {})

                # Without env vars or explicit args: registry.json fills swarm_id but still rejects missing agent;
                # with an empty state dir (no registry), it rejects missing swarm_id.
                err_no_agent = json.loads(ctx_off.tools["swarm_post"]["handler"]({"summary": "missing agent"}))
                self.assertFalse(err_no_agent["ok"])
                self.assertIn("agent", err_no_agent["error"])
                with patch.dict(os.environ, {"HERMES_A2A_STATE_DIR": str(self.tmp_root / "empty_state")}):
                    err_no_ctx = json.loads(ctx_off.tools["swarm_post"]["handler"]({"summary": "missing ctx"}))
                    self.assertFalse(err_no_ctx["ok"])
                    self.assertIn("swarm_id", err_no_ctx["error"])

                # With explicit swarm_id/agent/http_url/board_dir/token_file/conversation_id args and NO env vars,
                # all 6 tools execute cleanly against live http_server.py and disk board!
                explicit_ctx = {
                    "swarm_id": sid,
                    "agent": "gw-explicit-w",
                    "http_url": base_url,
                    "board_dir": captured["HERMES_SWARM_BOARD_DIR"],
                    "token_file": captured["HERMES_SWARM_TOKEN_FILE"],
                    "conversation_id": sid,
                }
                exp_post = json.loads(ctx_off.tools["swarm_post"]["handler"]({
                    **explicit_ctx,
                    "summary": "@coordinator [done] Explicit args without env",
                    "kind": "handoff_ready",
                }))
                self.assertTrue(exp_post["ok"], msg=exp_post)
                self.assertEqual(exp_post["source"], "http")
                self.assertEqual(exp_post["message"]["sender"], "gw-explicit-w")
                self.assertEqual(exp_post["message"]["kind"], "handoff_ready")

                exp_read = json.loads(ctx_off.tools["swarm_read"]["handler"]({**explicit_ctx, "since": 0}))
                self.assertTrue(exp_read["ok"], msg=exp_read)
                self.assertEqual(exp_read["source"], "http")
                self.assertTrue(any(m["summary"] == "@coordinator [done] Explicit args without env" for m in exp_read["messages"]))

                exp_status = json.loads(ctx_off.tools["swarm_status"]["handler"]({**explicit_ctx}))
                self.assertTrue(exp_status["ok"], msg=exp_status)
                self.assertEqual(exp_status["source"], "http")

                exp_claim = json.loads(ctx_off.tools["swarm_claim"]["handler"]({**explicit_ctx, "scope": "src/exp.py"}))
                self.assertTrue(exp_claim["ok"], msg=exp_claim)
                exp_rel = json.loads(ctx_off.tools["swarm_release"]["handler"]({**explicit_ctx, "scope": "src/exp.py"}))
                self.assertTrue(exp_rel["ok"], msg=exp_rel)
                self.assertEqual(exp_rel["released"], ["src/exp.py"])

                head_exp = exp_read["head"]
                mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    summary="@gw-explicit-w Ready for explicit wait check",
                    swarm_id=sid,
                )
                exp_wait = json.loads(ctx_off.tools["swarm_wait"]["handler"]({
                    **explicit_ctx,
                    "since": head_exp,
                    "timeout_seconds": 2.0,
                }))
                self.assertTrue(exp_wait["ok"], msg=exp_wait)
                self.assertTrue(exp_wait["woken"])
                self.assertEqual(exp_wait["messages"][0]["summary"], "@gw-explicit-w Ready for explicit wait check")

            # 2) With injected HERMES_SWARM_* env, plugin registers all 6 tools + prompt section and talks to LIVE http_server.py
            with patch.dict(os.environ, {k: v for k, v in captured.items() if v is not None}, clear=False):
                ctx_on = _Ctx()
                plugin_mod.register(ctx_on)
                self.assertEqual(
                    set(ctx_on.tools.keys()),
                    {"swarm_post", "swarm_read", "swarm_claim", "swarm_release", "swarm_status", "swarm_wait", "swarm_channel"},
                )
                self.assertIn("hermes-swarm-bridge.worker", ctx_on.sections)

                # Live HTTP post with Bearer token from HERMES_SWARM_TOKEN_FILE
                p_res = json.loads(ctx_on.tools["swarm_post"]["handler"]({
                    "summary": "@coordinator [done] Live plugin post",
                    "details": "Ignore @ghost in details",
                    "kind": "handoff",
                }))
                self.assertTrue(p_res["ok"])
                self.assertEqual(p_res["source"], "http")
                self.assertEqual(p_res["message"]["mentions"], ["coordinator"])

                # Live HTTP read + status
                r_res = json.loads(ctx_on.tools["swarm_read"]["handler"]({"since": 0}))
                self.assertTrue(r_res["ok"])
                self.assertEqual(r_res["source"], "http")
                self.assertTrue(any(m["summary"] == "@coordinator [done] Live plugin post" for m in r_res["messages"]))

                s_res = json.loads(ctx_on.tools["swarm_status"]["handler"]({}))
                self.assertTrue(s_res["ok"])
                self.assertEqual(s_res["source"], "http")
                self.assertIn("telemetry", s_res)

                # Claim & release via live HTTP /internal/board/{claim,release}
                c_res = json.loads(ctx_on.tools["swarm_claim"]["handler"]({"scope": "src/bridge.py", "notes": "testing"}))
                self.assertTrue(c_res["ok"])
                self.assertEqual(c_res["source"], "http")
                rel_res = json.loads(ctx_on.tools["swarm_release"]["handler"]({"scope": "src/bridge.py"}))
                self.assertTrue(rel_res["ok"])
                self.assertEqual(rel_res["source"], "http")
                self.assertEqual(rel_res["released"], ["src/bridge.py"])

                # swarm_wait wakes up when coordinator posts @bridge-w
                head_before = r_res["head"]

                def _coord_ping() -> None:
                    time.sleep(0.15)
                    mgr.board_service.post_message(
                        sender="coordinator",
                        sender_role="coordinator",
                        summary="@bridge-w Please verify final check",
                        swarm_id=sid,
                    )

                t_ping = threading.Thread(target=_coord_ping, daemon=True)
                t_ping.start()
                w_res = json.loads(ctx_on.tools["swarm_wait"]["handler"]({"since": head_before, "timeout_seconds": 3.0}))
                t_ping.join(timeout=2.0)
                self.assertTrue(w_res["ok"])
                self.assertTrue(w_res["woken"])
                self.assertEqual(w_res["messages"][0]["summary"], "@bridge-w Please verify final check")

                # Disk fallback when HTTP URL is dead
                with patch.dict(os.environ, {"HERMES_SWARM_HTTP_URL": "http://127.0.0.1:1"}):
                    fb_res = json.loads(ctx_on.tools["swarm_post"]["handler"]({"summary": "Disk fallback from live bridge"}))
                    self.assertTrue(fb_res["ok"])
                    self.assertEqual(fb_res["source"], "disk")

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_preamble_native_tools_first_and_fallback_ordering(self) -> None:
        bdir = self.state_dir / "swarms" / "pre-sw"
        tok_file = self.state_dir / "internal_token"
        for role_name in ("worker", "verifier", "narrator", "monitor"):
            wname = role_name if role_name in ("narrator", "monitor") else f"{role_name}-1"
            pre = runtime_mod.build_worker_preamble(
                models.WorkerConfig(role=role_name, worker_name=wname),
                swarm_id="pre-sw",
                task_id="t-pre",
                context_id="c-pre",
                board_dir=bdir,
                http_url="http://127.0.0.1:19955",
                token_file=tok_file,
                conversation_id="conv-pre-1",
            )
            self.assertIn('swarm_id="pre-sw"', pre)
            self.assertIn(f'agent="{wname}"', pre)
            self.assertIn(f'board_dir="{bdir.as_posix()}"', pre)
            self.assertIn('http_url="http://127.0.0.1:19955"', pre)
            self.assertIn('conversation_id="conv-pre-1"', pre)
            self.assertIn("Emergency fallback ONLY", pre)
            native_idx = pre.find("Use native `swarm_read`") if role_name == "narrator" else pre.find("swarm_post")
            cli_idx = pre.find("Emergency fallback ONLY")
            self.assertGreater(native_idx, -1)
            self.assertGreater(cli_idx, native_idx, f"{role_name} preamble did not put native swarm_* before Emergency fallback")
            self.assertGreater(pre.find("board_cli.py", cli_idx), cli_idx)

    def test_kill_all_five_workers_emits_zero_all_active_finished_wakes(self) -> None:
        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "kill-5of5-swarm"
            mgr.store.upsert_meta(
                sid,
                title="Kill 5 of 5",
                workspace=str(self.repo_dir),
                primary_conversation_id="conv-k5-p",
                coordinator_conversation_id="conv-k5-c",
            )
            rt = mgr.get_runtime(sid)
            wake_q = mgr.subscribe_wake_events()
            observed: list[dict] = []

            blocking_worker = self.tmp_root / "kill5_worker.py"
            blocking_worker.write_text(
                "import json, sys, time\n"
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-k5'}), flush=True)\n"
                "time.sleep(60)\n",
                encoding="utf-8",
            )

            try:
                with patch.object(
                    rt,
                    "_build_hermes_command",
                    return_value=([sys.executable, str(blocking_worker)], str(self.repo_dir)),
                ):
                    names = [f"w-five-{i}" for i in range(1, 6)]
                    for wname in names:
                        await rt.spawn_workers(
                            name=wname,
                            prompt=f"Hold {wname}",
                            workdir=str(self.repo_dir),
                            return_immediately=True,
                        )
                    for _ in range(80):
                        if all(rt.is_worker_busy(wn) for wn in names):
                            break
                        await asyncio.sleep(0.03)
                    self.assertTrue(all(rt.is_worker_busy(wn) for wn in names))

                    for wn in names:
                        res = await rt.kill_worker(wn, reason="batch stop")
                        self.assertTrue(res["killed"])
                    await asyncio.sleep(0.2)

                    while not wake_q.empty():
                        observed.append(wake_q.get_nowait())

                self.assertFalse(
                    any(
                        "All active" in str(ev.get("summary") or (ev.get("message") or {}).get("summary") or "")
                        for ev in observed
                    ),
                    msg=f"Spurious 'All active' wake emitted when killing 5/5 workers: {observed}",
                )
            finally:
                mgr.unsubscribe_wake_events(wake_q)
                await mgr.shutdown()

        asyncio.run(_run())

    def test_board_json_preserves_raw_kind_and_control_across_reload_and_snapshot(self) -> None:
        with patch.dict(os.environ, {"HERMES_A2A_HTTP_TOKEN": "kind-ctl-tok"}):
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-kind-ctl-p"
            coord_conv = "conv-kind-ctl-c"
            mgr.store.upsert_meta(
                sid,
                title="Kind & Control Persistence",
                workspace=str(self.repo_dir),
                primary_conversation_id=sid,
                coordinator_conversation_id=coord_conv,
            )
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            try:
                # 1) BoardService direct post with handoff_ready, blocker, and control dict
                m_hr = mgr.board_service.post_message(
                    sender="w-1",
                    msg_type="handoff_ready",
                    summary="@coordinator [done] Direct handoff_ready",
                    swarm_id=sid,
                )
                m_blk = mgr.board_service.post_message(
                    sender="w-1",
                    msg_type="blocker",
                    summary="Direct blocker",
                    swarm_id=sid,
                )
                m_ctl = mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    msg_type="system",
                    summary="Control pause",
                    control={"action": "pause", "target": "w-1"},
                    swarm_id=sid,
                )
                self.assertEqual(m_hr["kind"], "handoff_ready")
                self.assertEqual(m_hr["type"], "handoff")
                self.assertEqual(m_blk["kind"], "blocker")
                self.assertEqual(m_blk["type"], "escalation")
                self.assertEqual(m_ctl["control"], {"action": "pause", "target": "w-1"})

                # 2) HTTP POST /internal/board/post with handoff_ready, blocker, and control dict
                import httpx

                with httpx.Client(timeout=10.0) as hc:
                    r1 = hc.post(
                        f"{base_url}/internal/board/post",
                        headers={"Authorization": "Bearer kind-ctl-tok"},
                        json={
                            "swarmId": sid,
                            "sender": "w-2",
                            "kind": "handoff_ready",
                            "summary": "@coordinator HTTP handoff_ready",
                        },
                    )
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r1.json()["kind"], "handoff_ready")

                    r2 = hc.post(
                        f"{base_url}/internal/board/post",
                        headers={"Authorization": "Bearer kind-ctl-tok"},
                        json={
                            "swarmId": sid,
                            "sender": "w-2",
                            "kind": "blocker",
                            "summary": "HTTP blocker",
                            "control": {"action": "hold", "seq": 4},
                        },
                    )
                    self.assertEqual(r2.status_code, 200)
                    self.assertEqual(r2.json()["kind"], "blocker")
                    self.assertEqual(r2.json()["control"], {"action": "hold", "seq": 4})

                    # 3) Raw board.json on disk + fresh BoardService reload + GET /board observer snapshot
                    raw_disk = json.loads(mgr.store.paths(sid).board_file.read_text(encoding="utf-8"))
                    disk_by_sum = {m["summary"]: m for m in raw_disk["messages"]}
                    self.assertEqual(disk_by_sum["@coordinator [done] Direct handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(disk_by_sum["Direct blocker"]["kind"], "blocker")
                    self.assertEqual(disk_by_sum["Control pause"]["control"], {"action": "pause", "target": "w-1"})
                    self.assertEqual(disk_by_sum["@coordinator HTTP handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(disk_by_sum["HTTP blocker"]["kind"], "blocker")
                    self.assertEqual(disk_by_sum["HTTP blocker"]["control"], {"action": "hold", "seq": 4})

                    reloaded = board_mod.BoardService(self.state_dir).read_board(sid)
                    rel_by_sum = {m["summary"]: m for m in reloaded["messages"]}
                    self.assertEqual(rel_by_sum["@coordinator [done] Direct handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(rel_by_sum["Direct blocker"]["kind"], "blocker")
                    self.assertEqual(rel_by_sum["Control pause"]["control"], {"action": "pause", "target": "w-1"})
                    self.assertEqual(rel_by_sum["@coordinator HTTP handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(rel_by_sum["HTTP blocker"]["kind"], "blocker")
                    self.assertEqual(rel_by_sum["HTTP blocker"]["control"], {"action": "hold", "seq": 4})

                    snap = hc.get(
                        f"{base_url}/board?conversationId={coord_conv}",
                        headers={"Authorization": "Bearer kind-ctl-tok"},
                    ).json()
                    snap_by_sum = {m["summary"]: m for m in snap["messages"]}
                    self.assertEqual(snap_by_sum["@coordinator [done] Direct handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(snap_by_sum["Direct blocker"]["kind"], "blocker")
                    self.assertEqual(snap_by_sum["Control pause"]["control"], {"action": "pause", "target": "w-1"})
                    self.assertEqual(snap_by_sum["@coordinator HTTP handoff_ready"]["kind"], "handoff_ready")
                    self.assertEqual(snap_by_sum["HTTP blocker"]["kind"], "blocker")
                    self.assertEqual(snap_by_sum["HTTP blocker"]["control"], {"action": "hold", "seq": 4})
            finally:
                ctrl.stop()

    def test_orphan_wrapper_sweep_matches_targets_and_excludes_protected(self) -> None:
        fake_rows = [
            {"pid": 1001, "ppid": 1, "name": "timeout.exe", "cmdline": 'timeout.exe 30 python board_cli.py --swarm sw-1 --agent w-1 wait', "cwd": ""},
            {"pid": 1002, "ppid": 1, "name": "bash.exe", "cmdline": 'bash.exe -c "python C:/Tools/board_cli.py --board-dir C:/state/swarms/sw-1 --agent w-1 wait"', "cwd": ""},
            {"pid": 1003, "ppid": 1, "name": "cmd.exe", "cmdline": 'cmd.exe /c "cd /d C:\\repo\\.worktrees\\a2a-w-1 && pytest"', "cwd": ""},
            {"pid": 1004, "ppid": 1, "name": "python.exe", "cmdline": 'python.exe -m pytest', "cwd": r"C:\repo\.worktrees\a2a-w-1\sub"},
            # Other worker must NOT match
            {"pid": 1005, "ppid": 1, "name": "python.exe", "cmdline": 'python.exe board_cli.py --swarm sw-1 --agent w-2 wait', "cwd": ""},
            # Protected processes MUST be excluded even if cmdline references board_cli or .worktrees/a2a-w-1
            {"pid": 2001, "ppid": 1, "name": "hermes-gateway.exe", "cmdline": 'hermes-gateway.exe --note .worktrees/a2a-w-1', "cwd": ""},
            {"pid": 2002, "ppid": 1, "name": "hermes.exe", "cmdline": 'hermes.exe gateway run --dir .worktrees/a2a-w-1', "cwd": ""},
            {"pid": 2003, "ppid": 1, "name": "Antigravity.exe", "cmdline": 'Antigravity.exe C:/repo/.worktrees/a2a-w-1', "cwd": ""},
            {"pid": 2004, "ppid": 1, "name": "language_server_windows_x64.exe", "cmdline": 'language_server_windows_x64.exe .worktrees/a2a-w-1', "cwd": ""},
            {"pid": 2005, "ppid": 1, "name": "node.exe", "cmdline": 'node.exe /opt/plugins/hermes-swarm/sidecars/swarm_board/main.mjs .worktrees/a2a-w-1', "cwd": ""},
            {"pid": 2006, "ppid": 1, "name": "node.exe", "cmdline": 'node.exe wakeup_bridge.mjs board_cli.py', "cwd": ""},
            {"pid": 2007, "ppid": 1, "name": "python.exe", "cmdline": 'python.exe /opt/hermes-a2a-mcp/server.py --with-http .worktrees/a2a-w-1', "cwd": ""},
        ]
        killed_pids: list[int] = []

        async def _fake_kill(pid_or_proc, *, timeout: float = 5.0) -> None:
            killed_pids.append(int(pid_or_proc))

        async def _run() -> list[int]:
            with (
                patch.object(win_launcher, "_list_process_rows", return_value=fake_rows),
                patch.object(win_launcher, "kill_process_tree", side_effect=_fake_kill),
            ):
                return await win_launcher.find_and_kill_orphan_worker_processes(
                    swarm_id="sw-1",
                    worker_name="w-1",
                    worktree_path=r"C:\repo\.worktrees\a2a-w-1",
                )

        matched = asyncio.run(_run())
        self.assertEqual(set(matched), {1001, 1002, 1003, 1004})
        self.assertEqual(set(killed_pids), {1001, 1002, 1003, 1004})

    def test_hermes_config_and_live_tools_list_includes_swarm_bridge(self) -> None:
        import yaml

        hermes_home = Path(os.environ.get("HERMES_HOME") or (Path.home() / "AppData" / "Local" / "hermes"))
        cfg_path = hermes_home / "config.yaml"
        if cfg_path.is_file():
            doc = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            self.assertIn("hermes-swarm-bridge", ((doc.get("platform_toolsets") or {}).get("cli") or []))
            self.assertIn("hermes-swarm-bridge", ((doc.get("known_plugin_toolsets") or {}).get("cli") or []))

        hermes_exe = hermes_home / "bin" / ("hermes.exe" if sys.platform == "win32" else "hermes")
        if hermes_exe.is_file():
            env = dict(os.environ)
            env["HERMES_SWARM_ID"] = ""
            env["HERMES_SWARM_AGENT"] = ""
            proc = subprocess.run(
                [str(hermes_exe), "tools", "list"],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)
            self.assertRegex(proc.stdout, r"✓\s+enabled\s+hermes-swarm-bridge")


class WakeupAllHopsAndSecurityTests(unittest.TestCase):
    """End-to-end verification of all 6 inter-agent wakeup hops plus endpoint security & 1:1 boundaries."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-hops-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)

        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_A2A_HTTP_URL",
                "HERMES_A2A_HTTP_TOKEN",
                "HERMES_A2A_DEFAULT_WORKDIR",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        for k in ("HERMES_A2A_SWARM_ID", "HERMES_A2A_HTTP_URL", "HERMES_A2A_HTTP_TOKEN", "HERMES_A2A_DEFAULT_WORKDIR"):
            os.environ.pop(k, None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def _write_fake_worker_script(self, trace_file: Path, busy_gate: Path | None = None) -> Path:
        script = self.tmp_root / "fake_hop_worker.py"
        script.write_text(
            "\n".join([
                "import json, os, sys, time",
                "from pathlib import Path",
                "args = sys.argv[1:]",
                "query_file = ''",
                "resume_sid = ''",
                "i = 0",
                "while i < len(args):",
                "    if args[i] == '--query-file' and i + 1 < len(args):",
                "        query_file = args[i + 1]; i += 2; continue",
                "    if args[i] == '--resume' and i + 1 < len(args):",
                "        resume_sid = args[i + 1]; i += 2; continue",
                "    i += 1",
                "prompt = Path(query_file).read_text(encoding='utf-8') if query_file and Path(query_file).is_file() else ''",
                "wname = os.environ.get('HERMES_A2A_WORKER_NAME', 'worker')",
                "trace_path = Path(os.environ['HOP_TRACE_FILE'])",
                "with trace_path.open('a', encoding='utf-8') as fh:",
                "    fh.write(json.dumps({'worker': wname, 'resume': resume_sid, 'prompt': prompt}) + '\\n')",
                "sid = resume_sid or f'hs-{wname}-1'",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'mock-m', 'session_id': sid}), flush=True)",
                "if '[BUSY_GATE]' in prompt and not resume_sid:",
                "    gate = Path(os.environ['HOP_BUSY_GATE'])",
                "    deadline = time.monotonic() + 8.0",
                "    while not gate.exists() and time.monotonic() < deadline:",
                "        time.sleep(0.02)",
                "if '[DELEGATE_SUBAGENT]' in prompt:",
                "    print(json.dumps({'type': 'tool_use', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-a', 'input': {'goal': 'leaf audit', 'model': 'leaf-m'}}), flush=True)",
                "    print(json.dumps({'type': 'tool_result', 'name': 'delegate_task', 'tool_call_id': 'tc-sub-a', 'is_error': False, 'output': 'LEAF_PASS', 'duration_ms': 9}), flush=True)",
                "if '[TRIGGER_INPUT_REQUIRED]' in prompt:",
                "    out = '[INPUT_REQUIRED] Need approval token'",
                "    print(json.dumps({'type': 'text', 'text': out}), flush=True)",
                "    print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 3, 'text': out}), flush=True)",
                "    sys.exit(3)",
                "if '[TRIGGER_FAILED]' in prompt:",
                "    print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 1, 'error': 'simulated worker crash'}), flush=True)",
                "    sys.exit(1)",
                "print(json.dumps({'type': 'text', 'text': f'{wname}_OK'}), flush=True)",
                "print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0, 'text': f'{wname}_OK'}), flush=True)",
            ]),
            encoding="utf-8",
        )
        os.environ["HOP_TRACE_FILE"] = str(trace_file)
        if busy_gate is not None:
            os.environ["HOP_BUSY_GATE"] = str(busy_gate)
        return script

    def _read_trace(self, trace_file: Path) -> list[dict]:
        if not trace_file.is_file():
            return []
        return [json.loads(ln) for ln in trace_file.read_text(encoding="utf-8").splitlines() if ln.strip()]

    def test_hop1_primary_to_coordinator_emits_coordinator_wake_only(self) -> None:
        import httpx

        os.environ["HERMES_A2A_HTTP_TOKEN"] = "hop1-tok"
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-hop1-p"
        coord_conv = "conv-hop1-c"
        mgr.store.upsert_meta(
            sid,
            title="Hop 1 Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)
        headers = {"Authorization": "Bearer hop1-tok"}

        async def _run() -> None:
            board_events: list[tuple[str, dict]] = []
            wake_events: list[tuple[str, dict]] = []
            board_ready = asyncio.Event()
            wake_ready = asyncio.Event()
            got_both = asyncio.Event()

            async def _read_board_sse() -> None:
                async with httpx.AsyncClient(timeout=15.0) as hc:
                    async with hc.stream("GET", f"{base_url}/board/events?conversationId={coord_conv}", headers=headers) as resp:
                        self.assertEqual(resp.status_code, 200)
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                payload = json.loads(ln[6:])
                                board_events.append((ev, payload))
                                if ev == "snapshot":
                                    board_ready.set()
                                if ev == "coordinator_wake":
                                    if any(e == "coordinator_wake" for e, _ in wake_events):
                                        got_both.set()

            async def _read_wake_sse() -> None:
                async with httpx.AsyncClient(timeout=15.0) as hc:
                    async with hc.stream("GET", f"{base_url}/internal/wake/events", headers=headers) as resp:
                        self.assertEqual(resp.status_code, 200)
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                payload = json.loads(ln[6:])
                                wake_events.append((ev, payload))
                                if ev == "ready":
                                    wake_ready.set()
                                if ev == "coordinator_wake":
                                    if any(e == "coordinator_wake" for e, _ in board_events):
                                        got_both.set()

            t1 = asyncio.create_task(_read_board_sse())
            t2 = asyncio.create_task(_read_wake_sse())
            try:
                await asyncio.wait_for(asyncio.gather(board_ready.wait(), wake_ready.wait()), timeout=5.0)
                # Primary posts unrelated message -> no wake
                mgr.board_service.post_message(
                    sender="primary",
                    sender_role="primary",
                    summary="Background context only",
                    swarm_id=sid,
                )
                # Primary posts @coordinator -> coordinator_wake on BOTH streams, NEVER primary_wake
                posted = mgr.board_service.post_message(
                    sender="primary",
                    sender_role="primary",
                    summary="@coordinator Execute phase 1",
                    swarm_id=sid,
                )
                await asyncio.wait_for(got_both.wait(), timeout=5.0)
                await asyncio.sleep(0.15)
            finally:
                t1.cancel()
                t2.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)

            b_wakes = [(e, p) for e, p in board_events if e in ("coordinator_wake", "primary_wake")]
            w_wakes = [(e, p) for e, p in wake_events if e in ("coordinator_wake", "primary_wake")]
            self.assertEqual([e for e, _ in b_wakes], ["coordinator_wake"])
            self.assertEqual([e for e, _ in w_wakes], ["coordinator_wake"])
            self.assertEqual(w_wakes[0][1]["seq"], posted["seq"])
            self.assertEqual(w_wakes[0][1]["primaryConversationId"], sid)
            self.assertEqual(w_wakes[0][1]["coordinatorConversationId"], coord_conv)
            self.assertFalse(w_wakes[0][1]["synthetic"])

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_hop2_coordinator_to_worker_mentions_channel_all_and_busy_replay(self) -> None:
        trace_file = self.tmp_root / "hop2_trace.jsonl"
        busy_gate = self.tmp_root / "hop2_gate.flag"
        script = self._write_fake_worker_script(trace_file, busy_gate)

        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "hop2-swarm"
            mgr.store.upsert_meta(
                sid,
                title="Hop 2 Swarm",
                workspace=str(self.repo_dir),
                primary_conversation_id="conv-h2-p",
                coordinator_conversation_id="conv-h2-c",
            )
            mgr.set_host_loop(asyncio.get_running_loop())
            rt = mgr.get_runtime(sid)
            mgr.mention_router._debounce_seconds = 0.03

            def _fake_cmd(cfg, *, resume_session_id=None, query_file="-", effective_workdir=None):
                cmd = [sys.executable, str(script), "--query-file", query_file]
                if resume_session_id:
                    cmd.extend(["--resume", resume_session_id])
                return cmd, str(self.repo_dir)

            try:
                with patch.object(rt, "_build_hermes_command", side_effect=_fake_cmd):
                    # Spawn w-alpha (blocked by busy gate), w-beta (#backend), w-ui (#general), and narrator
                    await rt.spawn_workers(
                        name="w-alpha",
                        prompt="Initial alpha [BUSY_GATE]",
                        channels=["backend"],
                        workdir=str(self.repo_dir),
                        return_immediately=True,
                    )
                    await rt.spawn_workers(
                        name="w-beta",
                        prompt="Initial beta",
                        channels=["backend"],
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    await rt.spawn_workers(
                        name="w-ui",
                        prompt="Initial ui",
                        channels=["general"],
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    await rt.spawn_workers(
                        name="narrator",
                        role="narrator",
                        prompt="Maintain view.json",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    # Wait until w-alpha is actively running & busy
                    for _ in range(50):
                        if rt.is_worker_busy("w-alpha"):
                            break
                        await asyncio.sleep(0.02)
                    self.assertTrue(rt.is_worker_busy("w-alpha"))

                    # 1) Busy worker mention -> queued in pendingMentions -> replayed on turn end
                    mgr.board_service.post_message(
                        sender="coordinator",
                        sender_role="coordinator",
                        summary="@w-alpha Urgent queued task",
                        swarm_id=sid,
                    )
                    await asyncio.sleep(0.12)
                    board_mid = mgr.board_service.read_board(sid)
                    self.assertEqual(len(board_mid["pendingMentions"].get("w-alpha") or []), 1)

                    # Release gate -> turn 1 finishes and pending mention automatically resumes w-alpha
                    busy_gate.write_text("go", encoding="utf-8")
                    for _ in range(100):
                        entries = [e for e in self._read_trace(trace_file) if e["worker"] == "w-alpha" and e["resume"]]
                        if entries and not rt.is_worker_busy("w-alpha"):
                            break
                        await asyncio.sleep(0.05)

                    alpha_resumes = [e for e in self._read_trace(trace_file) if e["worker"] == "w-alpha" and e["resume"]]
                    self.assertEqual(len(alpha_resumes), 1)
                    self.assertEqual(alpha_resumes[0]["resume"], "hs-w-alpha-1")
                    self.assertIn("Urgent queued task", alpha_resumes[0]["prompt"])

                    # 2) @#backend wakes w-alpha & w-beta ONLY (not w-ui, not narrator)
                    mgr.board_service.channel_action(
                        "create",
                        channel="backend",
                        topic="Backend",
                        members=["w-alpha", "w-beta"],
                        swarm_id=sid,
                    )
                    mgr.board_service.post_message(
                        sender="coordinator",
                        sender_role="coordinator",
                        summary="Sync @#backend schema",
                        swarm_id=sid,
                    )
                    for _ in range(80):
                        tr = self._read_trace(trace_file)
                        if (
                            any("Sync @#backend schema" in e["prompt"] for e in tr if e["worker"] == "w-alpha")
                            and any("Sync @#backend schema" in e["prompt"] for e in tr if e["worker"] == "w-beta")
                            and not rt.is_worker_busy("w-alpha")
                            and not rt.is_worker_busy("w-beta")
                        ):
                            break
                        await asyncio.sleep(0.05)
                    tr_chan = self._read_trace(trace_file)
                    self.assertTrue(any("Sync @#backend schema" in e["prompt"] for e in tr_chan if e["worker"] == "w-alpha"))
                    self.assertTrue(any("Sync @#backend schema" in e["prompt"] for e in tr_chan if e["worker"] == "w-beta"))
                    self.assertFalse(any("Sync @#backend schema" in e["prompt"] for e in tr_chan if e["worker"] == "w-ui"))
                    for _ in range(40):
                        if not rt.is_worker_busy("narrator"):
                            break
                        await asyncio.sleep(0.05)

                    # 3) @all wakes w-alpha, w-beta, w-ui (excludes narrator)
                    mgr.board_service.post_message(
                        sender="coordinator",
                        sender_role="coordinator",
                        summary="@all Final checkpoint",
                        swarm_id=sid,
                    )
                    for _ in range(80):
                        tr = self._read_trace(trace_file)
                        got = {e["worker"] for e in tr if "@all Final checkpoint" in e["prompt"]}
                        if got == {"w-alpha", "w-beta", "w-ui"}:
                            break
                        await asyncio.sleep(0.05)
                    tr_all = self._read_trace(trace_file)
                    woken_all = {e["worker"] for e in tr_all if "@all Final checkpoint" in e["prompt"]}
                    self.assertEqual(woken_all, {"w-alpha", "w-beta", "w-ui"})
                    self.assertFalse(any("@all Final checkpoint" in e["prompt"] for e in tr_all if e["worker"] == "narrator"))

                    # 4) Direct @worker mention wakes w-ui only
                    mgr.board_service.post_message(
                        sender="coordinator",
                        sender_role="coordinator",
                        summary="@w-ui Polish button",
                        swarm_id=sid,
                    )
                    for _ in range(80):
                        tr = self._read_trace(trace_file)
                        if any("@w-ui Polish button" in e["prompt"] for e in tr if e["worker"] == "w-ui"):
                            break
                        await asyncio.sleep(0.05)
                    tr_dir = self._read_trace(trace_file)
                    self.assertEqual({e["worker"] for e in tr_dir if "@w-ui Polish button" in e["prompt"]}, {"w-ui"})
            finally:
                await mgr.shutdown()

        asyncio.run(_run())

    def test_hop3_worker_to_worker_http_and_disk_fallback_watcher(self) -> None:
        import httpx

        trace_file = self.tmp_root / "hop3_trace.jsonl"
        script = self._write_fake_worker_script(trace_file)
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr.mention_router._poll_interval = 0.2
        mgr.mention_router._debounce_seconds = 0.03
        sid = "conv-hop3-p"
        mgr.store.upsert_meta(
            sid,
            title="Hop 3 Peer Wakeups",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id="conv-hop3-c",
        )
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)
        tok = mgr.get_internal_token()
        tok_file = mgr.get_internal_token_file()
        swarm_dir = mgr.store.paths(sid).swarm_dir

        async def _run() -> None:
            rt = mgr.get_runtime(sid)

            def _fake_cmd(cfg, *, resume_session_id=None, query_file="-", effective_workdir=None):
                cmd = [sys.executable, str(script), "--query-file", query_file]
                if resume_session_id:
                    cmd.extend(["--resume", resume_session_id])
                return cmd, str(self.repo_dir)

            with patch.object(rt, "_build_hermes_command", side_effect=_fake_cmd):
                await rt.spawn_workers(
                    name="peer-a",
                    prompt="Boot peer-a",
                    workdir=str(self.repo_dir),
                    return_immediately=False,
                    wait_seconds=10.0,
                )
                await rt.spawn_workers(
                    name="peer-b",
                    prompt="Boot peer-b",
                    workdir=str(self.repo_dir),
                    return_immediately=False,
                    wait_seconds=10.0,
                )

                # Part A: peer-a -> @peer-b via HTTP POST /internal/board/post
                async with httpx.AsyncClient(timeout=10.0) as hc:
                    r_post = await hc.post(
                        f"{base_url}/internal/board/post",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={
                            "swarmId": sid,
                            "sender": "peer-a",
                            "kind": "handoff",
                            "summary": "@peer-b review via http post",
                        },
                    )
                    self.assertEqual(r_post.status_code, 200)

                for _ in range(80):
                    tr = self._read_trace(trace_file)
                    if (
                        any("review via http post" in e["prompt"] for e in tr if e["worker"] == "peer-b")
                        and not rt.is_worker_busy("peer-b")
                    ):
                        break
                    await asyncio.sleep(0.05)
                tr_a = self._read_trace(trace_file)
                self.assertTrue(any("review via http post" in e["prompt"] for e in tr_a if e["worker"] == "peer-b"))

                # Part B: peer-b -> @peer-a via disk-fallback board_cli.py subprocess + MentionRouter disk watcher
                env_cli = dict(os.environ)
                env_cli["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
                cli_proc = await asyncio.create_subprocess_exec(
                    sys.executable,
                    str(Path(__file__).resolve().parent.parent / "board_cli.py"),
                    "--board-dir",
                    str(swarm_dir),
                    "--swarm",
                    sid,
                    "--agent",
                    "peer-b",
                    "--role",
                    "worker",
                    "--http-url",
                    "http://127.0.0.1:1",
                    "--token-file",
                    str(tok_file),
                    "post",
                    "--channel",
                    "main",
                    "--kind",
                    "handoff",
                    "--summary",
                    "@peer-a verify via disk fallback",
                    env=env_cli,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout_b, stderr_b = await cli_proc.communicate()
                self.assertEqual(cli_proc.returncode, 0, msg=stderr_b.decode("utf-8", errors="replace"))
                self.assertEqual(json.loads(stdout_b.decode("utf-8"))["summary"], "@peer-a verify via disk fallback")

                for _ in range(80):
                    tr = self._read_trace(trace_file)
                    if (
                        any("verify via disk fallback" in e["prompt"] for e in tr if e["worker"] == "peer-a")
                        and not rt.is_worker_busy("peer-a")
                    ):
                        break
                    await asyncio.sleep(0.05)
                tr_b = self._read_trace(trace_file)
                self.assertTrue(any("verify via disk fallback" in e["prompt"] for e in tr_b if e["worker"] == "peer-a"))

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_hop4_worker_to_coordinator_all_triggers_and_synthetic_new_swarm(self) -> None:
        import httpx

        os.environ["HERMES_A2A_HTTP_TOKEN"] = "hop4-tok"
        trace_file = self.tmp_root / "hop4_trace.jsonl"
        script = self._write_fake_worker_script(trace_file)
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)
        headers = {"Authorization": "Bearer hop4-tok"}

        async def _run() -> None:
            wake_events: list[tuple[str, dict]] = []
            ready_ev = asyncio.Event()
            done_ev = asyncio.Event()

            async def _read_wake_sse() -> None:
                async with httpx.AsyncClient(timeout=20.0) as hc:
                    async with hc.stream("GET", f"{base_url}/internal/wake/events", headers=headers) as resp:
                        self.assertEqual(resp.status_code, 200)
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                payload = json.loads(ln[6:])
                                wake_events.append((ev, payload))
                                if ev == "ready":
                                    ready_ev.set()
                                if (
                                    sum(1 for e, p in wake_events if e == "coordinator_wake" and p.get("swarmId") == "hop4-dyn")
                                    >= 10
                                ):
                                    done_ev.set()

            sse_task = asyncio.create_task(_read_wake_sse())
            try:
                await asyncio.wait_for(ready_ev.wait(), timeout=5.0)
                ready_payload = next(p for e, p in wake_events if e == "ready")
                self.assertTrue(ready_payload["ok"])
                self.assertEqual(ready_payload["swarms"], [])

                # Create brand-new swarm AFTER SSE connect
                dyn_sid = "hop4-dyn"
                mgr.store.upsert_meta(
                    dyn_sid,
                    title="Hop 4 Dynamic Swarm",
                    workspace=str(self.repo_dir),
                    primary_conversation_id="conv-hop4-p",
                    coordinator_conversation_id="conv-hop4-c",
                )

                # All 7 board-post triggers + disallowed @primary attempt
                triggers = [
                    ("post", "@coordinator check in"),
                    ("handoff", "slice ready without mention"),
                    ("question", "need port choice without mention"),
                    ("blocker", "missing env key without mention"),
                    ("approval_request", "approve schema migration without mention"),
                    ("post", "[done] finished without mention"),
                    ("post", "[stall] waiting on peer without mention"),
                ]
                for kind, summary in triggers:
                    mgr.board_service.post_message(sender="worker-h4", msg_type=kind, summary=summary, swarm_id=dyn_sid)

                # Worker posting @primary MUST NEVER emit primary_wake
                mgr.board_service.post_message(
                    sender="worker-h4",
                    msg_type="post",
                    summary="@primary trying direct ping",
                    swarm_id=dyn_sid,
                )

                # Synthetic transitions: INPUT_REQUIRED, FAILED, and active-worker-count-drops-to-0
                rt = mgr.get_runtime(dyn_sid)

                def _fake_cmd(cfg, *, resume_session_id=None, query_file="-", effective_workdir=None):
                    return [sys.executable, str(script), "--query-file", query_file], str(self.repo_dir)

                with patch.object(rt, "_build_hermes_command", side_effect=_fake_cmd):
                    await rt.spawn_workers(
                        name="w-ir",
                        prompt="[TRIGGER_INPUT_REQUIRED]",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    await rt.spawn_workers(
                        name="w-fail",
                        prompt="[TRIGGER_FAILED]",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                    await rt.spawn_workers(
                        name="w-ok",
                        prompt="Complete cleanly",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )

                await asyncio.wait_for(done_ev.wait(), timeout=6.0)
                await asyncio.sleep(0.15)
            finally:
                sse_task.cancel()
                await asyncio.gather(sse_task, return_exceptions=True)

            coord_wakes = [p for e, p in wake_events if e == "coordinator_wake" and p.get("swarmId") == "hop4-dyn"]
            prim_wakes = [p for e, p in wake_events if e == "primary_wake"]
            self.assertEqual(prim_wakes, [], f"Tier-2 worker unexpectedly emitted primary_wake: {prim_wakes}")

            board_summaries = [p["message"]["summary"] for p in coord_wakes if not p.get("synthetic")]
            for _, expected_summary in triggers:
                self.assertIn(expected_summary, board_summaries)

            synth_states = [((p.get("task") or {}).get("state"), (p.get("task") or {}).get("activeWorkerCount")) for p in coord_wakes if p.get("synthetic")]
            self.assertIn((models.TaskState.INPUT_REQUIRED.value, 0), synth_states)
            self.assertIn((models.TaskState.FAILED.value, 0), synth_states)
            self.assertIn((models.TaskState.COMPLETED.value, 0), synth_states)
            for sp in [p for p in coord_wakes if p.get("synthetic")]:
                self.assertEqual(sp["seq"], 0)
                self.assertGreater(sp["wakeSeq"], 0)

            # Verify since_map catchup replays missed board & synthetic wakes
            since_map_q = json.dumps({"hop4-dyn": {"seq": 1, "wakeSeq": 1}})
            async with httpx.AsyncClient(timeout=10.0) as hc:
                async with hc.stream(
                    "GET",
                    f"{base_url}/internal/wake/events",
                    params={"since_map": since_map_q},
                    headers=headers,
                ) as resp2:
                    self.assertEqual(resp2.status_code, 200)
                    replayed: list[tuple[str, dict]] = []
                    ev = ""
                    async for ln in resp2.aiter_lines():
                        if ln.startswith("event: "):
                            ev = ln[7:].strip()
                        elif ln.startswith("data: "):
                            replayed.append((ev, json.loads(ln[6:])))
                            if len(replayed) >= 8:
                                break
                    self.assertEqual(replayed[0][0], "ready")
                    self.assertTrue(any(e == "coordinator_wake" for e, _ in replayed[1:]))

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_hop5_coordinator_to_primary_mention_and_escalation(self) -> None:
        import httpx

        os.environ["HERMES_A2A_HTTP_TOKEN"] = "hop5-tok"
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-hop5-p"
        coord_conv = "conv-hop5-c"
        mgr.store.upsert_meta(
            sid,
            title="Hop 5 Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)
        headers = {"Authorization": "Bearer hop5-tok"}

        async def _run() -> None:
            board_events: list[tuple[str, dict]] = []
            wake_events: list[tuple[str, dict]] = []
            b_ready = asyncio.Event()
            w_ready = asyncio.Event()
            both_two = asyncio.Event()

            async def _read_board() -> None:
                async with httpx.AsyncClient(timeout=15.0) as hc:
                    async with hc.stream("GET", f"{base_url}/board/events?conversationId={sid}", headers=headers) as resp:
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                board_events.append((ev, json.loads(ln[6:])))
                                if ev == "snapshot":
                                    b_ready.set()
                                if (
                                    sum(1 for e, _ in board_events if e == "primary_wake") >= 2
                                    and sum(1 for e, _ in wake_events if e == "primary_wake") >= 2
                                ):
                                    both_two.set()

            async def _read_wake() -> None:
                async with httpx.AsyncClient(timeout=15.0) as hc:
                    async with hc.stream("GET", f"{base_url}/internal/wake/events", headers=headers) as resp:
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                wake_events.append((ev, json.loads(ln[6:])))
                                if ev == "ready":
                                    w_ready.set()
                                if (
                                    sum(1 for e, _ in board_events if e == "primary_wake") >= 2
                                    and sum(1 for e, _ in wake_events if e == "primary_wake") >= 2
                                ):
                                    both_two.set()

            t1 = asyncio.create_task(_read_board())
            t2 = asyncio.create_task(_read_wake())
            try:
                await asyncio.wait_for(asyncio.gather(b_ready.wait(), w_ready.wait()), timeout=5.0)
                # Plain coordinator post does NOT wake primary
                mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    summary="@worker-1 proceed",
                    swarm_id=sid,
                )
                # Coordinator @primary -> primary_wake
                mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    summary="@primary Ready for sign-off",
                    msg_type="post",
                    swarm_id=sid,
                )
                # Coordinator kind="escalation" (even without @primary) -> primary_wake
                mgr.board_service.post_message(
                    sender="coordinator",
                    sender_role="coordinator",
                    summary="Escalate release blocker",
                    msg_type="escalation",
                    swarm_id=sid,
                )
                await asyncio.wait_for(both_two.wait(), timeout=5.0)
                await asyncio.sleep(0.15)
            finally:
                t1.cancel()
                t2.cancel()
                await asyncio.gather(t1, t2, return_exceptions=True)

            self.assertEqual([e for e, _ in board_events if e in ("primary_wake", "coordinator_wake")], ["primary_wake", "primary_wake"])
            self.assertEqual([e for e, _ in wake_events if e in ("primary_wake", "coordinator_wake")], ["primary_wake", "primary_wake"])

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_hop6_tier3_subagent_tracked_never_wakes_upper_tiers(self) -> None:
        import httpx

        os.environ["HERMES_A2A_HTTP_TOKEN"] = "hop6-tok"
        trace_file = self.tmp_root / "hop6_trace.jsonl"
        busy_gate = self.tmp_root / "hop6_gate.flag"
        script = self._write_fake_worker_script(trace_file, busy_gate)
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-hop6-p"
        coord_conv = "conv-hop6-c"
        mgr.store.upsert_meta(
            sid,
            title="Hop 6 Subagents",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        ctrl = http_server.EmbeddedA2AHttpController(mgr)
        base_url = ctrl.start(host="127.0.0.1", port=0)
        headers = {"Authorization": "Bearer hop6-tok"}

        async def _run() -> None:
            wake_events: list[tuple[str, dict]] = []
            ready_ev = asyncio.Event()

            async def _read_wake() -> None:
                async with httpx.AsyncClient(timeout=15.0) as hc:
                    async with hc.stream("GET", f"{base_url}/internal/wake/events", headers=headers) as resp:
                        ev = ""
                        async for ln in resp.aiter_lines():
                            if ln.startswith("event: "):
                                ev = ln[7:].strip()
                            elif ln.startswith("data: "):
                                wake_events.append((ev, json.loads(ln[6:])))
                                if ev == "ready":
                                    ready_ev.set()

            t_sse = asyncio.create_task(_read_wake())
            try:
                await asyncio.wait_for(ready_ev.wait(), timeout=5.0)
                rt = mgr.get_runtime(sid)

                def _fake_cmd(cfg, *, resume_session_id=None, query_file="-", effective_workdir=None):
                    return [sys.executable, str(script), "--query-file", query_file], str(self.repo_dir)

                with patch.object(rt, "_build_hermes_command", side_effect=_fake_cmd):
                    # Keep a second worker active so w-lead completing doesn't fire drop-to-0 during subagent check
                    await rt.spawn_workers(
                        name="w-keeper",
                        prompt="Keep swarm active [BUSY_GATE]",
                        workdir=str(self.repo_dir),
                        return_immediately=True,
                    )
                    await rt.spawn_workers(
                        name="w-lead",
                        prompt="Run [DELEGATE_SUBAGENT] check",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )

                    # Also simulate a Tier-3 subagent board post & synthetic subagent transition
                    mgr.board_service.post_message(
                        sender="deleg-leaf-1",
                        sender_role="subagent",
                        msg_type="escalation",
                        summary="@coordinator @primary Tier 3 post",
                        swarm_id=sid,
                    )
                    rt._emit_task_wake_if_needed(
                        task_id="t-sub",
                        worker_name="deleg-leaf-1",
                        worker_role="subagent",
                        state=models.TaskState.FAILED.value,
                        error_text="subagent failure",
                    )
                    await asyncio.sleep(0.2)

                    # Verify subagents recorded on board & observer snapshot
                    async with httpx.AsyncClient(timeout=10.0) as hc:
                        snap = (await hc.get(f"{base_url}/board?conversationId={coord_conv}", headers=headers)).json()
                    lead_mem = next(m for m in snap["members"] if m["name"] == "w-lead")
                    self.assertEqual(len(lead_mem["subagents"]), 1)
                    self.assertEqual(lead_mem["subagents"][0]["id"], "tc-sub-a")
                    self.assertEqual(lead_mem["subagents"][0]["status"], "done")

                    # Zero wake events emitted by delegate_task or subagent messages/transitions
                    self.assertFalse(
                        any(e in ("coordinator_wake", "primary_wake") for e, _ in wake_events),
                        msg=f"Unexpected wake from Tier-3 subagent: {wake_events}",
                    )
                    busy_gate.write_text("go", encoding="utf-8")
                    for _ in range(80):
                        if not rt.is_worker_busy("w-keeper"):
                            break
                        await asyncio.sleep(0.05)
            finally:
                t_sse.cancel()
                await asyncio.gather(t_sse, return_exceptions=True)

        try:
            asyncio.run(_run())
        finally:
            ctrl.stop()

    def test_endpoint_security_and_1to1_boundary_internal_wake(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr.store.upsert_meta(
            "conv-sec-1",
            title="Sec Swarm 1",
            workspace=str(self.repo_dir),
            primary_conversation_id="conv-sec-1",
            coordinator_conversation_id="conv-sec-1c",
        )
        mgr.store.upsert_meta(
            "conv-sec-arch",
            title="Archived Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id="conv-sec-arch",
            archived=True,
        )

        # Bearer token configured -> 401 without/wrong token, 403 on non-loopback Host/client, 200 when valid
        app = http_server.create_a2a_app(mgr, http_url="http://127.0.0.1:19940", bearer_token="wake-secret-xyz")
        client = TestClient(app, base_url="http://127.0.0.1:19940")
        auth_hdr = {"Authorization": "Bearer wake-secret-xyz"}

        # /board/swarms remains 404; /board stays 1:1 by conversationId
        self.assertEqual(client.get("/board/swarms", headers=auth_hdr).status_code, 404)
        self.assertEqual(client.get("/board", headers=auth_hdr).json(), {"conversationId": None, "swarm": None, "member": None})
        b1 = client.get("/board?conversationId=conv-sec-1", headers=auth_hdr)
        self.assertEqual(b1.status_code, 200)
        self.assertEqual(b1.json()["swarm"], "conv-sec-1")
        self.assertNotIn("swarms", b1.json())

        # /internal/wake/swarms & /internal/wake/events auth & loopback Host checks
        for path in ("/internal/wake/swarms", "/internal/wake/events"):
            self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(client.get(path, headers={"Authorization": "Bearer wrong"}).status_code, 401)
            self.assertEqual(
                client.get(path, headers={"Host": "evil.example:19940", **auth_hdr}).status_code,
                403,
            )

        # Non-loopback client.host rejected with 403
        remote_client = TestClient(app, base_url="http://127.0.0.1:19940", client=("10.20.30.40", 4444))
        self.assertEqual(remote_client.get("/internal/wake/swarms", headers=auth_hdr).status_code, 403)
        self.assertEqual(remote_client.get("/internal/wake/events", headers=auth_hdr).status_code, 403)

        # Valid bearer & x-sidecar-token succeed; archived swarms excluded
        r_swarms = client.get("/internal/wake/swarms", headers=auth_hdr)
        self.assertEqual(r_swarms.status_code, 200)
        swarm_ids = [s["swarmId"] for s in r_swarms.json()["swarms"]]
        self.assertEqual(swarm_ids, ["conv-sec-1"])

        r_sidecar_tok = client.get("/internal/wake/swarms", headers={"x-sidecar-token": "wake-secret-xyz"})
        self.assertEqual(r_sidecar_tok.status_code, 200)

        self.assertEqual(client.get("/internal/wake/events?since_map=not-json", headers=auth_hdr).status_code, 400)
        with client.stream("GET", "/internal/wake/events", headers=auth_hdr) as resp:
            self.assertEqual(resp.status_code, 200)
            lines = []
            for ln in resp.iter_lines():
                if ln:
                    lines.append(ln)
                if len(lines) >= 2:
                    break
            joined = "\n".join(lines)
            self.assertIn("event: ready", joined)
            self.assertIn("conv-sec-1", joined)
            self.assertNotIn("conv-sec-arch", joined)


class Phase3GapClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-p3-gaps-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("HERMES_A2A_STATE_DIR", "HERMES_A2A_HTTP_TOKEN", "SWARM_OVERVIEW", "SWARM_GRAPH_TAB", "SWARM_RAW_TAB")
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("HERMES_A2A_HTTP_TOKEN", None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_default_internal_token_auth_and_internal_board_and_wake_endpoints(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-p3-http"
        coord_conv = "conv-p3-http-c"
        mgr.store.upsert_meta(
            sid,
            title="P3 HTTP",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        tok = mgr.get_internal_token()
        self.assertTrue(bool(tok))
        app = http_server.create_a2a_app(mgr, bearer_token=None)
        client = TestClient(app, base_url="http://127.0.0.1")

        try:
            # Even with HERMES_A2A_HTTP_TOKEN unset, default internal_token enforces 401 on missing/bad token
            for method, path, payload in (
                ("GET", "/internal/wake/swarms", None),
                ("GET", "/internal/wake/events", None),
                ("POST", "/internal/wake/events", {"since_map": {}, "active_only": True}),
                ("POST", "/internal/board/post", {"swarmId": sid, "sender": "w1", "summary": "hi"}),
                ("POST", "/internal/board/claim", {"swarmId": sid, "agent": "w1", "scope": "a.py"}),
                ("POST", "/internal/board/release", {"swarmId": sid, "agent": "w1", "scope": "a.py"}),
                ("POST", "/internal/board/channel", {"swarmId": sid, "agent": "coordinator", "action": "list"}),
                ("GET", f"/internal/board/read?swarmId={sid}", None),
                ("POST", "/internal/board/read", {"swarmId": sid}),
            ):
                r_no = client.request(method, path, json=payload)
                self.assertEqual(r_no.status_code, 401, f"{method} {path} expected 401 without token, got {r_no.status_code}")
                r_bad = client.request(method, path, headers={"Authorization": "Bearer wrong-tok"}, json=payload)
                self.assertEqual(r_bad.status_code, 401, f"{method} {path} expected 401 with bad token, got {r_bad.status_code}")

            hdr = {"Authorization": f"Bearer {tok}"}

            # /internal/board/{claim,release,channel,read}
            r_cl = client.post("/internal/board/claim", headers=hdr, json={"swarmId": sid, "agent": "w1", "scope": "src/x.py"})
            self.assertEqual(r_cl.status_code, 200)
            self.assertIn("src/x.py", r_cl.json()["claims"]["claims"])

            r_rel = client.post("/internal/board/release", headers=hdr, json={"swarmId": sid, "agent": "w1", "scope": "src/x.py"})
            self.assertEqual(r_rel.status_code, 200)
            self.assertEqual(r_rel.json()["released"], ["src/x.py"])

            r_ch = client.post(
                "/internal/board/channel",
                headers=hdr,
                json={"swarmId": sid, "agent": "coordinator", "action": "create", "channel": "api", "members": ["w1"]},
            )
            self.assertEqual(r_ch.status_code, 200)
            self.assertIsNotNone(r_ch.json().get("control_seq"))

            r_post = client.post(
                "/internal/board/post",
                headers=hdr,
                json={"swarmId": sid, "sender": "w1", "channel": "api", "summary": "API ready"},
            )
            self.assertEqual(r_post.status_code, 200)
            p_seq = r_post.json()["seq"]

            # Archive channel and verify post to archived channel returns 400
            r_arch = client.post(
                "/internal/board/channel",
                headers=hdr,
                json={"swarmId": sid, "agent": "coordinator", "action": "archive", "channel": "api"},
            )
            self.assertEqual(r_arch.status_code, 200)
            r_blk = client.post(
                "/internal/board/post",
                headers=hdr,
                json={"swarmId": sid, "sender": "w1", "channel": "api", "summary": "Blocked post"},
            )
            self.assertEqual(r_blk.status_code, 400)

            # GET and POST /internal/board/read with seqs, status, channel="*"
            r_get = client.get(f"/internal/board/read?swarmId={sid}&seqs={p_seq}", headers=hdr)
            self.assertEqual(r_get.status_code, 200)
            self.assertEqual([m["seq"] for m in r_get.json()["messages"]], [p_seq])

            r_post_rd = client.post("/internal/board/read", headers=hdr, json={"swarmId": sid, "channel": "*", "limit": 10})
            self.assertEqual(r_post_rd.status_code, 200)
            self.assertGreaterEqual(len(r_post_rd.json()["messages"]), 2)

            r_st = client.post("/internal/board/read", headers=hdr, json={"swarmId": sid, "status": True})
            self.assertEqual(r_st.status_code, 200)
            self.assertIn("members", r_st.json())

            # POST /internal/wake/events with >2048-char since_map and active_only=True
            big_map = {f"stale-swarm-{i}": {"seq": i, "wakeSeq": i} for i in range(120)}
            big_map[sid] = {"seq": 0, "wakeSeq": 0}
            self.assertGreater(len(json.dumps(big_map)), 2048)
            with client.stream("POST", "/internal/wake/events", headers=hdr, json={"since_map": big_map, "active_only": True}) as resp:
                self.assertEqual(resp.status_code, 200)
                first_chunk = next(resp.iter_lines())
                self.assertTrue(first_chunk.startswith("event:"))
        finally:
            asyncio.run(mgr.shutdown())

    def test_lock_dead_pid_fast_eviction_and_concurrent_takeover(self) -> None:
        lock_file = self.tmp_root / "fast_dead.lock"
        lock_file.write_text(
            json.dumps({"pid": 9999999, "ts": time.time(), "token": "dead-t", "released": False}),
            encoding="utf-8",
        )
        t0 = time.monotonic()
        with store.atomic_file_lock(lock_file, timeout=2.0, stale_seconds=30.0):
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 1.0, f"Dead PID lock stalled {elapsed:.2f}s")
        self.assertFalse(lock_file.exists())

    def test_persistent_wake_seq_catchup_and_ghost_working_recovery(self) -> None:
        sid = "conv-p3-restart"
        mgr1 = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr1.store.upsert_meta(
            sid,
            title="Restart Test",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id="conv-p3-restart-c",
        )
        ws1 = mgr1.store.allocate_wake_seq(sid)
        ws2 = mgr1.store.allocate_wake_seq(sid)
        self.assertEqual((ws1, ws2), (1, 2))

        # Seed an interrupted WORKING task + ghost working member on board.json
        mgr1.board_service.upsert_member("ghost-w", role="worker", state="working", swarm_id=sid)
        t_rec = {
            "id": "task-ghost-1",
            "contextId": "ctx-g1",
            "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
            "metadata": {"workerConfig": {"worker_name": "ghost-w", "role": "worker"}},
            "history": [],
            "artifacts": [],
        }
        store.atomic_write_json(mgr1.store.paths(sid).tasks_file, {"tasks": {"task-ghost-1": t_rec}})
        asyncio.run(mgr1.shutdown())

        # Restart: new SwarmManager loads state
        mgr2 = runtime_mod.SwarmManager(state_dir=self.state_dir)
        try:
            rt2 = mgr2.get_runtime(sid)
            # wakeSeq must persist across restart and increment monotonically
            ws3 = mgr2.store.allocate_wake_seq(sid)
            self.assertEqual(ws3, 3)

            # Interrupted WORKING task & ghost working member must both be reset
            reloaded_task = rt2._tasks["task-ghost-1"]
            self.assertEqual(reloaded_task["status"]["state"], models.TaskState.FAILED.value)
            b_after = mgr2.board_service.read_board(sid)
            self.assertEqual(b_after["members"]["ghost-w"]["state"], "stopped")

            # Synthetic catchup delivered when since_seq > 0 and since_wake_seq == 0
            mgr2._emit_board_event(
                sid,
                "coordinator_wake",
                {"synthetic": True, "summary": "Synth catchup", "task": {"id": "t-c1", "state": "TASK_STATE_COMPLETED"}},
            )
            missed = mgr2.get_wake_catchup_events({sid: {"seq": 5, "wakeSeq": 0}})
            self.assertTrue(
                any(ev.get("type") == "coordinator_wake" and ev.get("synthetic") for ev in missed),
                msg=f"Missed synthetic event not delivered when since_seq>0 and since_wake_seq==0: {missed}",
            )
        finally:
            asyncio.run(mgr2.shutdown())

    def test_switch_model_guards_group_kill_quota_and_dirty_worktree(self) -> None:
        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-p3-rt"
            mgr.store.upsert_meta(
                sid,
                title="Runtime Guards",
                workspace=str(self.repo_dir),
                primary_conversation_id=sid,
                coordinator_conversation_id="conv-p3-rt-c",
            )
            rt = mgr.get_runtime(sid)
            try:
                # Group kill <name>-1..k
                hold_script = self.tmp_root / "hold_grp.py"
                hold_script.write_text(
                    "import json, time\n"
                    "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-g'}), flush=True)\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(hold_script)], str(self.repo_dir))):
                    await rt.spawn_workers(
                        name="grp",
                        n=2,
                        prompt="Hold group",
                        channels=["backend"],
                        workdir=str(self.repo_dir),
                        return_immediately=True,
                    )
                    await asyncio.sleep(0.2)

                # Verify spawn_workers emitted subscribe control message
                msgs = mgr.board_service.read_board(sid)["messages"]
                self.assertTrue(
                    any((m.get("control") or {}).get("op") == "subscribe" for m in msgs),
                    msg=f"Expected subscribe control message on spawn_workers: {msgs}",
                )

                # Group kill by base name "grp" kills grp-1 and grp-2
                k_res = await rt.kill_worker("grp", reason="group stop")
                self.assertTrue(k_res["killed"])
                self.assertTrue(rt._is_worker_killed("grp-1"))
                self.assertTrue(rt._is_worker_killed("grp-2"))

                # switch_model on killed worker is rejected cleanly
                with self.assertRaises(RuntimeError):
                    await rt.switch_model("grp-1", model="new-model")

                # switch_model on coordinator works without KeyError
                sw_coord = await rt.switch_model("coordinator", model="coord-model")
                self.assertTrue(sw_coord["switched"])
                self.assertEqual(sw_coord["worker"], "coordinator")

                # QUOTA_RE failure classification
                quota_script = self.tmp_root / "quota_fail.py"
                quota_script.write_text(
                    "import json, sys\n"
                    "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'm', 'session_id': 'hs-q'}), flush=True)\n"
                    "print(json.dumps({'type': 'result', 'session_id': 'hs-q', 'exit_code': 1, 'error': 'HTTP 429 RESOURCE_EXHAUSTED quota'}), flush=True)\n"
                    "sys.exit(1)\n",
                    encoding="utf-8",
                )
                with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(quota_script)], str(self.repo_dir))):
                    q_res = await rt.spawn_workers(
                        name="w-quota",
                        prompt="Trigger quota",
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                t_id = q_res["tasks"][0]["id"]
                t_fail = rt._tasks[t_id]["metadata"].get("failure")
                self.assertIsInstance(t_fail, dict)
                self.assertEqual(t_fail.get("kind"), "quota")

                # worktree.py dirty worktree guard resets dirty reused worktree cleanly
                wt_info = await worktree_mod.create_worktree(self.repo_dir, "dirty-w")
                wt_path = Path(wt_info.worktree_path)
                (wt_path / "dirty_untracked.txt").write_text("dirty", encoding="utf-8")
                (wt_path / "app.py").write_text("dirty_mod", encoding="utf-8")
                wt_info2 = await worktree_mod.create_worktree(self.repo_dir, "dirty-w")
                self.assertEqual(Path(wt_info2.worktree_path), wt_path)
                self.assertFalse((wt_path / "dirty_untracked.txt").exists())
                self.assertIn("hello", (wt_path / "app.py").read_text(encoding="utf-8"))
                await worktree_mod.remove_worktree(self.repo_dir, wt_info2.worktree_path, branch=wt_info2.branch)
            finally:
                await mgr.shutdown()

        asyncio.run(_run())

    def test_observer_snapshot_parity_attention_narrator_frame_and_304(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-p3-snap"
        coord_conv = "conv-p3-snap-c"
        mgr.store.upsert_meta(
            sid,
            title="Snapshot Parity",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        try:
            # Workspace screenshot allowed by /board/frame
            ws_shot = self.repo_dir / "ws_preview.png"
            ws_shot.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 24)
            mgr.board_service.upsert_member("w-snap", role="worker", state="waiting", swarm_id=sid)
            # Killed narrator-a vs live narrator-b: pick_narrator must rank live narrator-b over killed narrator-a!
            mgr.board_service.upsert_member("narrator-a", role="narrator", state="killed", swarm_id=sid)
            mgr.board_service.upsert_member("narrator-b", role="narrator", state="waiting", swarm_id=sid)

            # Quota failed task -> systemAttention + failure.kind == "quota"
            old_ts = "2020-01-01T00:00:00Z"
            store.atomic_write_json(
                mgr.store.paths(sid).tasks_file,
                {
                    "tasks": {
                        "task-q": {
                            "id": "task-q",
                            "contextId": "ctx-q",
                            "status": {"state": models.TaskState.FAILED.value, "timestamp": old_ts, "message": "HTTP 429 quota exceeded"},
                            "metadata": {"workerConfig": {"worker_name": "w-snap", "role": "worker"}, "failure": {"kind": "quota", "text": "HTTP 429 quota exceeded"}},
                            "history": [],
                            "artifacts": [],
                        }
                    }
                },
            )
            mgr.get_runtime(sid)._load_state()

            m1 = mgr.board_service.post_message(
                sender="w-snap",
                summary="@coordinator check ws_preview.png",
                details=f"Screenshot: {ws_shot} | workspace shot",
                swarm_id=sid,
            )
            mgr.board_service.post_message(
                sender="coordinator",
                summary="Ack",
                reply_to=m1["seq"],
                swarm_id=sid,
            )

            snap = http_server.build_observer_snapshot(mgr, sid, member="coordinator")
            self.assertIn("systemAttention", snap)
            self.assertTrue(any(a.get("kind") == "quota" for a in snap["systemAttention"]), msg=snap["systemAttention"])
            self.assertIn("allowedFiles", snap)
            self.assertIn("urlHosts", snap)
            self.assertIn("edges", snap)
            self.assertTrue(len(snap["edges"]) >= 1)
            self.assertIn("unseenBy", snap)
            self.assertEqual(snap["narrator"]["name"], "narrator-b")
            w_mem = next(m for m in snap["members"] if m["name"] == "w-snap")
            self.assertIn("replied", w_mem)
            self.assertIn("replyAsk", w_mem)
            self.assertRegex(snap["etag"], rf"^{sid}\|coordinator\|[0-9a-f]+$")

            app = http_server.create_a2a_app(mgr)
            client = TestClient(app, base_url="http://127.0.0.1")
            r_1 = client.get(f"/board?conversationId={coord_conv}")
            self.assertEqual(r_1.status_code, 200)
            r_304 = client.get(f"/board?conversationId={coord_conv}", headers={"If-None-Match": r_1.json()["etag"]})
            self.assertEqual(r_304.status_code, 304)

            r_frame = client.get(f"/board/frame?conversationId={coord_conv}&path={ws_shot}")
            self.assertEqual(r_frame.status_code, 200)
        finally:
            asyncio.run(mgr.shutdown())

    def test_seed_existing_populates_pending_mentions(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-p3-seed"
        mgr.store.upsert_meta(sid, title="Seed Pending", workspace=str(self.repo_dir), primary_conversation_id=sid)
        try:
            b = mgr.board_service.read_board(sid)
            b["pendingMentions"] = {"w-busy": [{"seq": 1, "sender": "coordinator", "summary": "@w-busy pending"}]}
            mgr.store.write_board(sid, b)
            mgr.mention_router.seed_existing([sid])
            self.assertIn(sid, mgr.mention_router._swarms_with_pending)
        finally:
            asyncio.run(mgr.shutdown())

    def test_phase2_141_regressions(self) -> None:
        async def _run() -> None:
            import httpx
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-p2-141"
            coord_conv = "conv-p2-141-c"
            mgr.store.upsert_meta(
                sid,
                title="Phase 2 #141 Regressions",
                workspace=str(self.repo_dir),
                primary_conversation_id=sid,
                coordinator_conversation_id=coord_conv,
            )
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            tok = mgr.get_internal_token()
            auth_hdr = {"Authorization": f"Bearer {tok}"}
            try:
                # 1. board.py: reject unknown non-default channel (ValueError + HTTP 400, zero phantom seq)
                head_before = mgr.board_service.head_seq(mgr.board_service.read_board(sid))
                with self.assertRaises(ValueError):
                    mgr.board_service.post_message(
                        sender="w1",
                        summary="should fail",
                        channel="ghost-chan",
                        swarm_id=sid,
                    )
                self.assertEqual(mgr.board_service.head_seq(mgr.board_service.read_board(sid)), head_before)

                async with httpx.AsyncClient(timeout=10.0) as hc:
                    r_bad_ch = await hc.post(
                        f"{base_url}/internal/board/post",
                        headers=auth_hdr,
                        json={"swarmId": sid, "sender": "w1", "channel": "ghost-chan", "summary": "bad ch"},
                    )
                    self.assertEqual(r_bad_ch.status_code, 400)
                self.assertEqual(mgr.board_service.head_seq(mgr.board_service.read_board(sid)), head_before)

                # 2. board.py: suppress no-op subscribe/unsubscribe control messages when prev == next
                mgr.board_service.upsert_member("w1", role="worker", state="waiting", channels=["main"], swarm_id=sid)
                mgr.board_service.upsert_member("w2", role="worker", state="waiting", channels=["main"], swarm_id=sid)
                ch_create = mgr.board_service.channel_action(
                    "create", channel="backend", members=["w1"], actor="coordinator", swarm_id=sid
                )
                self.assertIsNotNone(ch_create.get("controlMessage"))
                head_after_create = mgr.board_service.head_seq(mgr.board_service.read_board(sid))
                ch_noop_sub = mgr.board_service.channel_action(
                    "subscribe", channel="backend", members=["w1"], actor="coordinator", swarm_id=sid
                )
                self.assertIsNone(ch_noop_sub.get("controlMessage"))
                self.assertEqual(mgr.board_service.head_seq(mgr.board_service.read_board(sid)), head_after_create)
                ch_noop_unsub = mgr.board_service.channel_action(
                    "unsubscribe", channel="backend", members=["w2"], actor="coordinator", swarm_id=sid
                )
                self.assertIsNone(ch_noop_unsub.get("controlMessage"))
                self.assertEqual(mgr.board_service.head_seq(mgr.board_service.read_board(sid)), head_after_create)

                # 3. board.py: exclude self-posts + readBy from unseenBy/visibleUnread, and wait updates readBy
                m_self = mgr.board_service.post_message(sender="w1", summary="w1 own post", swarm_id=sid)
                m_other = mgr.board_service.post_message(sender="w2", summary="@w1 please check", swarm_id=sid)
                snap_unread = http_server.build_observer_snapshot(mgr, sid, member="w1")
                self.assertNotIn(m_self["seq"], snap_unread["unseenBy"].get("w1", []))
                self.assertIn(m_other["seq"], snap_unread["unseenBy"].get("w1", []))
                w1_row = next(m for m in snap_unread["members"] if m["name"] == "w1")
                self.assertEqual(w1_row["visibleUnread"], 1)

                w_res = await mgr.board_service.wait_for_update(
                    swarm_id=sid, since=0, target="w1", mentions_only=True, timeout_seconds=1.0
                )
                self.assertTrue(w_res["woken"])
                board_after_wait = mgr.board_service.read_board(sid)
                m_other_disk = next(m for m in board_after_wait["messages"] if m["seq"] == m_other["seq"])
                self.assertIn("w1", [r.lower() for r in (m_other_disk.get("readBy") or [])])
                snap_after_wait = http_server.build_observer_snapshot(mgr, sid, member="w1")
                self.assertEqual(snap_after_wait["unseenBy"].get("w1", []), [])
                w1_row_after = next(m for m in snap_after_wait["members"] if m["name"] == "w1")
                self.assertEqual(w1_row_after["visibleUnread"], 0)

                # 4. board.py: message_matches_target("coordinator", ...) NOT waking on addressed peer handoffs (@verifier)
                peer_handoff = mgr.board_service.post_message(
                    sender="w1",
                    msg_type="handoff",
                    summary="@verifier [done] ready for review",
                    swarm_id=sid,
                )
                unaddressed_done = mgr.board_service.post_message(
                    sender="w1",
                    msg_type="handoff",
                    summary="[done] all slices complete",
                    swarm_id=sid,
                )
                b_now = mgr.board_service.read_board(sid)
                self.assertFalse(
                    mgr.board_service.message_matches_target(peer_handoff, target="coordinator", board=b_now, mentions_only=True)
                )
                self.assertTrue(
                    mgr.board_service.message_matches_target(peer_handoff, target="verifier", board=b_now, mentions_only=True)
                )
                self.assertTrue(
                    mgr.board_service.message_matches_target(unaddressed_done, target="coordinator", board=b_now, mentions_only=True)
                )

                # 5. http_server.py: build_observer_snapshot preserves "general" in channels alongside non-main channels
                b_ch = mgr.board_service.read_board(sid)
                b_ch["channels"]["general"] = models.new_channel_record("general", topic="General")
                mgr.store.write_board(sid, b_ch)
                snap_ch = http_server.build_observer_snapshot(mgr, sid)
                ch_names = [c["name"] for c in snap_ch["channels"]]
                self.assertIn("general", ch_names)
                self.assertIn("backend", ch_names)

                # 6. http_server.py: /board/frame fallback from pruned .worktrees/<wt>/<relpath> -> <workspace>/<relpath>
                canon_dir = self.repo_dir / "shots"
                canon_dir.mkdir(parents=True, exist_ok=True)
                canon_png = canon_dir / "proof.png"
                png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
                canon_png.write_bytes(png_bytes)
                pruned_ref = self.repo_dir / ".worktrees" / "wt-pruned" / "shots" / "proof.png"
                self.assertFalse(pruned_ref.exists())
                mgr.board_service.post_message(
                    sender="w1",
                    summary="Proof frame",
                    details=f"Screenshot: {pruned_ref}",
                    swarm_id=sid,
                )
                async with httpx.AsyncClient(timeout=10.0) as hc:
                    r_fr = await hc.get(
                        f"{base_url}/board/frame",
                        params={"conversationId": coord_conv, "path": str(pruned_ref)},
                    )
                    self.assertEqual(r_fr.status_code, 200)
                    self.assertEqual(r_fr.content, png_bytes)

                # 7. server.py: swarm_wait default <= 120s and max clamp <= 150s
                import inspect
                import server as mcp_srv
                sig = inspect.signature(mcp_srv.swarm_wait)
                self.assertLessEqual(float(sig.parameters["timeout_seconds"].default), 120.0)
                captured_timeouts: list[float] = []

                async def _fake_wait(**kwargs):
                    captured_timeouts.append(float(kwargs["timeout_seconds"]))
                    return {"woken": False, "timedOut": True, "swarmId": sid, "head": 0, "messages": []}

                with patch.object(mcp_srv, "_swarm_mgr", mgr):
                    with patch.object(mgr.board_service, "wait_for_update", side_effect=_fake_wait):
                        await mcp_srv.swarm_wait(swarm_id=sid, timeout_seconds=999.0)
                self.assertEqual(captured_timeouts, [150.0])

                # 8. board_cli.py: --files JSON array parsing
                import io
                from contextlib import redirect_stdout
                with redirect_stdout(io.StringIO()) as cli_buf:
                    rc_cli = board_cli.main([
                        "--swarm", sid,
                        "--agent", "w1",
                        "--http-url", base_url,
                        "--token-file", str(mgr.get_internal_token_file()),
                        "post",
                        "--summary", "cli json files",
                        "--files", '["src/calc.py", "tests/test_calc.py"]',
                    ])
                self.assertEqual(rc_cli, 0)
                self.assertEqual(json.loads(cli_buf.getvalue())["files"], ["src/calc.py", "tests/test_calc.py"])

                # 9. worktree.py: remove_worktree & prune_worktrees remove empty .worktrees/ parent dir cleanly
                wt_1 = await worktree_mod.create_worktree(self.repo_dir, "rm-clean")
                await worktree_mod.remove_worktree(self.repo_dir, wt_1.worktree_path, branch=wt_1.branch)
                self.assertFalse((self.repo_dir / ".worktrees").exists())

                wt_2 = await worktree_mod.create_worktree(self.repo_dir, "prune-clean")
                self.assertTrue(Path(wt_2.worktree_path).is_dir())
                await worktree_mod.prune_worktrees(self.repo_dir, active_ids=set())
                self.assertFalse((self.repo_dir / ".worktrees").exists())
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run())

    def test_phase2_5626_backend_regressions(self) -> None:
        import sqlite3

        # 1a. Honest telemetry unit checks: uncachedInput vs cacheRead, idempotency, deepseek-v4 262144, no bogus contextUsed
        raw_tok = {"input": 100_000, "cacheRead": 95_000, "output": 500}
        norm1 = runtime_mod._normalize_tokens_dict(raw_tok, context_limit=262_144)
        self.assertEqual(norm1["input"], 5_000)
        self.assertEqual(norm1["uncachedInput"], 5_000)
        self.assertEqual(norm1["cacheRead"], 95_000)
        self.assertEqual(norm1["output"], 500)
        self.assertEqual(norm1["total"], 100_500)
        self.assertEqual(norm1["new"], 5_500)
        self.assertEqual(norm1["contextUsed"], 0)
        self.assertEqual(norm1["contextLimit"], 262_144)
        self.assertEqual(norm1["contextPct"], 0.0)

        norm2 = runtime_mod._normalize_tokens_dict(norm1)
        self.assertEqual(norm2["input"], 5_000)
        self.assertEqual(norm2["uncachedInput"], 5_000)
        self.assertEqual(norm2["cacheRead"], 95_000)
        self.assertEqual(norm2["total"], 100_500)
        self.assertEqual(norm2["new"], 5_500)

        self.assertEqual(runtime_mod._resolve_context_limit("deepseek-v4", None, None), 1_000_000)
        self.assertEqual(runtime_mod._resolve_context_limit("deepseek/deepseek-v4.1", None, None), 1_000_000)

        # 1b. End-to-end worker telemetry: llm_active_ms excludes swarm_wait, custom 0.0 -> costUsd=None, delegate_task(list) -> 0 subagents
        hermes_home = self.tmp_root / "hermes_home_p2_5626"
        hermes_home.mkdir(parents=True, exist_ok=True)
        db_path = hermes_home / "state.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT)"
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "hs-p2-5626",
                None,
                "cli",
                2_000,
                200,
                48_000,
                0,
                0,
                0.0,
                None,
                "deepseek-v4",
                json.dumps({}),
                2,
                2,
                1000.0,
                1005.0,
                "custom",
                "https://local.example.invalid/v1",
            ),
        )
        con.commit()
        con.close()

        worker_script = self.tmp_root / "fake_p2_5626_worker.py"
        worker_script.write_text(
            "\n".join([
                "import json, time",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'deepseek-v4', 'session_id': 'hs-p2-5626'}), flush=True)",
                "time.sleep(0.32)",
                "print(json.dumps({'type': 'tool_use', 'name': 'delegate_task', 'tool_call_id': 'tc-list-1', 'input': {'action': 'list'}}), flush=True)",
                "print(json.dumps({'type': 'tool_result', 'name': 'delegate_task', 'tool_call_id': 'tc-list-1', 'is_error': False, 'output': '[]', 'duration_ms': 5}), flush=True)",
                "print(json.dumps({'type': 'tool_use', 'name': 'swarm_wait', 'tool_call_id': 'tc-wait-1', 'input': {'since': 0}}), flush=True)",
                "time.sleep(0.70)",
                "print(json.dumps({'type': 'tool_result', 'name': 'swarm_wait', 'tool_call_id': 'tc-wait-1', 'is_error': False, 'output': '{\"woken\": true}', 'duration_ms': 700}), flush=True)",
                "print(json.dumps({'type': 'text', 'text': 'P2_5626_OK'}), flush=True)",
                "print(json.dumps({'type': 'result', 'session_id': 'hs-p2-5626', 'exit_code': 0, 'duration_ms': 5000, 'cost_usd': 0.0, 'text': 'P2_5626_OK', 'tokens': {'input': 50000, 'cacheRead': 48000, 'output': 200}}), flush=True)",
            ]),
            encoding="utf-8",
        )

        async def _run() -> None:
            import httpx

            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-p2-5626"
            coord_conv = "conv-p2-5626-c"
            mgr.store.upsert_meta(
                sid,
                title="Phase 2 5626 Regressions",
                workspace=str(self.repo_dir),
                primary_conversation_id=sid,
                coordinator_conversation_id=coord_conv,
            )
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            try:
                rt = mgr.get_runtime(sid)
                with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                    with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(worker_script)], str(self.repo_dir))):
                        await rt.spawn_workers(
                            name="w-tel",
                            prompt="Test phase2 telemetry",
                            workdir=str(self.repo_dir),
                            model="deepseek-v4",
                            provider="custom",
                            return_immediately=False,
                        )
                snap_tel = http_server.build_observer_snapshot(mgr, sid)
                w_tel = next(m for m in snap_tel["members"] if m["name"] == "w-tel")
                self.assertEqual(w_tel["tokens"]["input"], 2_000)
                self.assertEqual(w_tel["tokens"]["cacheRead"], 48_000)
                self.assertEqual(w_tel["tokens"]["total"], 50_200)
                self.assertEqual(w_tel["tokens"]["new"], 2_200)
                self.assertEqual(w_tel["tokens"]["contextLimit"], 1_000_000)
                self.assertEqual(w_tel["tokens"]["contextUsed"], 0)
                self.assertIsNone(w_tel["costUsd"])
                self.assertEqual(w_tel["subagents"], [])
                t_tel = next(t for t in rt._tasks.values() if (t.get("metadata") or {}).get("workerConfig", {}).get("worker_name") == "w-tel")
                self.assertGreater(w_tel["tps"], 150.0, msg=f"act={t_tel['metadata']['liveActivity']}")
                self.assertLess((t_tel["metadata"]["liveActivity"].get("llmActiveMs") or 0), 650)

                # 2. Coordinator posted/read counts via channel_action + swarm_board_read default mark_read_by vs ""
                mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", swarm_id=sid)
                mgr.board_service.upsert_member("w1", role="worker", state="waiting", swarm_id=sid)
                ch_res = mgr.board_service.channel_action(
                    "create", channel="ops", members=["w1"], actor="coordinator", swarm_id=sid
                )
                ctrl_seq = ch_res["controlMessage"]["seq"]
                b_after_ch = mgr.board_service.read_board(sid)
                coord_mem = b_after_ch["members"]["coordinator"]
                self.assertEqual(coord_mem["lastPostSeq"], ctrl_seq)
                self.assertEqual(coord_mem["lastReadSeq"], ctrl_seq)

                m_w1 = mgr.board_service.post_message(sender="w1", summary="@coordinator ping 1", swarm_id=sid)
                with patch.object(mcp_srv, "_swarm_mgr", mgr):
                    await mcp_srv.swarm_board_read(swarm_id=sid, since=0)
                b_after_read = mgr.board_service.read_board(sid)
                self.assertGreaterEqual(b_after_read["members"]["coordinator"]["lastReadSeq"], m_w1["seq"])
                m_w1_disk = next(m for m in b_after_read["messages"] if m["seq"] == m_w1["seq"])
                self.assertIn("coordinator", [r.lower() for r in (m_w1_disk.get("readBy") or [])])

                m_w1_2 = mgr.board_service.post_message(sender="w1", summary="@coordinator ping 2", swarm_id=sid)
                with patch.object(mcp_srv, "_swarm_mgr", mgr):
                    await mcp_srv.swarm_board_read(swarm_id=sid, since=0, mark_read_by="")
                b_after_peek = mgr.board_service.read_board(sid)
                self.assertEqual(b_after_peek["members"]["coordinator"]["lastReadSeq"], m_w1["seq"])
                m_w1_2_disk = next(m for m in b_after_peek["messages"] if m["seq"] == m_w1_2["seq"])
                self.assertNotIn("coordinator", [r.lower() for r in (m_w1_2_disk.get("readBy") or [])])

                snap_coord = http_server.build_observer_snapshot(mgr, sid)
                coord_row = next(m for m in snap_coord["members"] if m["name"] == "coordinator")
                self.assertEqual(coord_row["lastPostSeq"], ctrl_seq)
                self.assertEqual(coord_row["lastReadSeq"], m_w1["seq"])
                self.assertGreaterEqual(coord_row["postCount"], 1)
                self.assertEqual(coord_row["posts"], coord_row["postCount"])
                self.assertGreaterEqual(coord_row["readCount"], 1)

                # 3. Active Tier-3 subagent suppresses false stuck alert even when parent currentTool startedAt > 10m ago
                old_ts = "2020-01-01T00:00:00Z"
                with rt._state_rlock:
                    rt._tasks["task-parent-sub"] = {
                        "id": "task-parent-sub",
                        "contextId": "ctx-parent-sub",
                        "status": {"state": models.TaskState.WORKING.value, "timestamp": old_ts},
                        "metadata": {
                            "workerConfig": {"worker_name": "w-parent", "role": "worker"},
                            "liveActivity": {
                                "steps": 2,
                                "toolCalls": 3,
                                "currentTool": {"name": "delegate_task", "preview": "sub", "startedAt": old_ts},
                                "lastEventAt": old_ts,
                            },
                        },
                        "history": [],
                        "artifacts": [],
                    }
                mgr.board_service.upsert_member("w-parent", role="worker", state="working", swarm_id=sid)
                # Force member timestamps old so only the active subagent keeps it fresh
                with mgr.board_service._locked_board(sid, mutate=True) as (_, b_fix, _):
                    b_fix["members"]["w-parent"]["lastHeartbeatAt"] = old_ts
                    b_fix["members"]["w-parent"]["updatedAt"] = old_ts
                mgr.board_service.upsert_subagents(
                    "w-parent",
                    [{"id": "sub-active-1", "name": "subagent-1", "status": "working", "updatedAt": models.now_iso()}],
                    swarm_id=sid,
                )
                snap_sub = http_server.build_observer_snapshot(mgr, sid)
                self.assertFalse(
                    any(a.get("kind") == "stuck" and a.get("member") == "w-parent" for a in snap_sub["systemAttention"]),
                    msg=snap_sub["systemAttention"],
                )

                # 4a. Pre-merge worktree screenshots (extract_screenshots + /board/frame) & allowedFiles dict
                wt_root = self.repo_dir / ".worktrees" / "a2a-w-shot"
                wt_assets = wt_root / "assets"
                wt_assets.mkdir(parents=True, exist_ok=True)
                wt_png = wt_assets / "dash-dark.png"
                png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 20
                wt_png.write_bytes(png_bytes)
                mgr.board_service.upsert_member(
                    "w-shot",
                    role="worker",
                    state="waiting",
                    worktree_path=str(wt_root),
                    swarm_id=sid,
                )
                mgr.board_service.post_message(
                    sender="w-shot",
                    summary="Dark dashboard screenshot",
                    details="Screenshot: assets/dash-dark.png | pre-merge dark",
                    swarm_id=sid,
                )
                b_shot = mgr.board_service.read_board(sid)
                extracted = board_mod.extract_screenshots(b_shot, workspace_path=str(self.repo_dir))
                self.assertTrue(any(Path(e["path"]).resolve() == wt_png.resolve() for e in extracted), msg=extracted)

                async with httpx.AsyncClient(timeout=10.0) as hc:
                    r_wt = await hc.get(
                        f"{base_url}/board/frame",
                        params={"conversationId": coord_conv, "path": "assets/dash-dark.png"},
                    )
                    self.assertEqual(r_wt.status_code, 200)
                    self.assertEqual(r_wt.content, png_bytes)

                snap_files = http_server.build_observer_snapshot(mgr, sid)
                self.assertIsInstance(snap_files["allowedFiles"], dict)
                self.assertTrue(
                    all(isinstance(k, str) and isinstance(v, str) and v.startswith("file://") for k, v in snap_files["allowedFiles"].items()),
                    msg=snap_files["allowedFiles"],
                )

                # 4b. swarm_manage(init|status) returns observer_ui_url
                with patch.object(mcp_srv, "_swarm_mgr", mgr), patch.object(mcp_srv, "_http_ctrl", ctrl):
                    init_res = json.loads(
                        await mcp_srv.swarm_manage(
                            action="init",
                            swarm_id="conv-ui-url",
                            title="UI URL Check",
                            workspace=str(self.repo_dir),
                            primary_conversation_id="conv-ui-url",
                            coordinator_conversation_id="conv-ui-url-c",
                        )
                    )
                    self.assertEqual(init_res["observer_ui_url"], f"{base_url}/board/ui?conversationId=conv-ui-url")
                    status_res = json.loads(await mcp_srv.swarm_manage(action="status", swarm_id="conv-ui-url"))
                    self.assertEqual(status_res["observer_ui_url"], f"{base_url}/board/ui?conversationId=conv-ui-url")
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run())


class Phase2HardeningRegressionTests(unittest.TestCase):
    """Targeted regression tests for Phase-2 store/board, HTTP, runtime, wakeup, and win_launcher hardening."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-v2-harden-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._env_backup = {
            k: os.environ.get(k)
            for k in ("HERMES_A2A_STATE_DIR", "HERMES_A2A_SWARM_ID", "HERMES_HOME", "HERMES_EXE")
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("HERMES_A2A_SWARM_ID", None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_read_json_safe_raise_on_error_preserves_corrupt_board(self) -> None:
        bad_json = self.tmp_root / "corrupt.json"
        bad_json.write_text("{corrupt-json", encoding="utf-8")
        self.assertEqual(store.read_json_safe(bad_json, default={"ok": False}), {"ok": False})
        with self.assertRaises(ValueError):
            store.read_json_safe(bad_json, default={}, raise_on_error=True)

        svc = board_mod.BoardService(state_dir=self.state_dir)
        sid = "harden-corrupt-board"
        svc.post_message(sender="coordinator", summary="Keep me", swarm_id=sid)
        bfile = store.resolve_swarm_paths(sid, state_dir=self.state_dir).board_file
        corrupt_payload = bfile.read_text(encoding="utf-8") + "\n{CORRUPT"
        bfile.write_text(corrupt_payload, encoding="utf-8")
        with self.assertRaises(ValueError):
            svc.post_message(sender="coordinator", summary="Should not wipe", swarm_id=sid)
        self.assertEqual(bfile.read_text(encoding="utf-8"), corrupt_payload)

    def test_claim_scope_conflict_force_and_expired_reap(self) -> None:
        svc = board_mod.BoardService(state_dir=self.state_dir)
        sid = "harden-claims"
        svc.claim_scope(scope="src/auth/*", holder="worker-1", ttl_seconds=3600.0, swarm_id=sid)
        with self.assertRaises(board_mod.ScopeConflictError):
            svc.claim_scope(scope="src/auth/login.py", holder="worker-2", swarm_id=sid)

        forced = svc.claim_scope(scope="src/auth/login.py", holder="worker-2", force=True, swarm_id=sid)
        self.assertIsNotNone(forced.get("conflictWarning"))

        # Terminal member claim must be reaped automatically
        svc.upsert_member("worker-dead", role="worker", state="failed", swarm_id=sid)
        svc.claim_scope(scope="src/term.py", holder="worker-dead", ttl_seconds=3600.0, swarm_id=sid)
        reaped_term = svc.claim_scope(scope="src/term.py", holder="worker-3", swarm_id=sid)
        self.assertIsNone(reaped_term.get("conflictWarning"))
        self.assertEqual(reaped_term["claimed"]["holder"], "worker-3")

        # Expired TTL claim must be reaped automatically on next claim_scope
        svc.claim_scope(scope="src/expired.py", holder="worker-1", ttl_seconds=0.01, swarm_id=sid)
        time.sleep(0.05)
        reaped = svc.claim_scope(scope="src/expired.py", holder="worker-3", swarm_id=sid)
        self.assertIsNone(reaped.get("conflictWarning"))
        self.assertEqual(reaped["claimed"]["holder"], "worker-3")

    def test_advance_contiguous_last_read_and_board_archive_overflow(self) -> None:
        svc = board_mod.BoardService(state_dir=self.state_dir)
        sid = "harden-cursor-archive"
        svc.upsert_member("worker-a", role="worker", channels=["*"], swarm_id=sid)
        svc.channel_action("create", channel="dev", swarm_id=sid)
        m1 = svc.post_message(sender="coordinator", summary="msg 1", channel="#main", swarm_id=sid)["seq"]
        m2 = svc.post_message(sender="coordinator", summary="msg 2 @worker-a", channel="#dev", swarm_id=sid)["seq"]
        m3 = svc.post_message(sender="coordinator", summary="msg 3", channel="#main", swarm_id=sid)["seq"]

        # Filtered read by channel="#dev" only shows channel-create (seq 1) + m2 (seq 3),
        # so contiguous lastReadSeq must NOT jump past m1 (seq 2)
        r_dev = svc.read_messages(agent="worker-a", mark_read_by="worker-a", channel="#dev", swarm_id=sid)
        self.assertEqual([m["seq"] for m in r_dev["messages"]], [1, m2])
        b_after_dev = svc.read_board(sid)
        self.assertLess(b_after_dev["members"]["worker-a"]["lastReadSeq"], m1)

        # Direct helper check: unread at m1 prevents jumping to head m3
        mem = {"lastReadSeq": 0, "channels": ["*"], "role": "worker"}
        msgs = [{"seq": m1, "channel": "#main", "channels": ["#main"]}, {"seq": m2, "channel": "#dev", "channels": ["#dev"]}, {"seq": m3, "channel": "#main", "channels": ["#main"]}]
        board_mod._advance_contiguous_last_read("worker-a", mem, msgs, {}, head=m3)
        self.assertEqual(mem["lastReadSeq"], m1 - 1)

        # Overflow past MAX_BOARD_MESSAGES archives trimmed messages to board_archive.jsonl
        with patch.object(board_mod, "MAX_BOARD_MESSAGES", 3):
            svc.post_message(sender="coordinator", summary="msg 4", swarm_id=sid)
            svc.post_message(sender="coordinator", summary="msg 5", swarm_id=sid)
        b_trimmed = svc.read_board(sid)
        self.assertEqual(len(b_trimmed["messages"]), 3)
        archive_file = store.resolve_swarm_paths(sid, state_dir=self.state_dir).board_file.with_name("board_archive.jsonl")
        self.assertTrue(archive_file.is_file())
        archived_lines = [json.loads(ln) for ln in archive_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        self.assertEqual(len(archived_lines), 3)
        self.assertEqual([m["summary"] for m in archived_lines[1:]], ["msg 1", "msg 2 @worker-a"])

    def test_http_hardening_403_413_400_409_and_0000_normalization(self) -> None:
        import httpx
        st_dir = self.state_dir

        async def _run() -> None:
            mgr = runtime_mod.SwarmManager(state_dir=st_dir)
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            prev_tok = os.environ.get("HERMES_A2A_HTTP_TOKEN")
            os.environ["HERMES_A2A_HTTP_TOKEN"] = "tok-0000"
            try:
                url = ctrl.start(host="0.0.0.0", port=0)
            finally:
                if prev_tok is None:
                    os.environ.pop("HERMES_A2A_HTTP_TOKEN", None)
                else:
                    os.environ["HERMES_A2A_HTTP_TOKEN"] = prev_tok
            try:
                self.assertIn("127.0.0.1", url)
                self.assertNotIn("0.0.0.0", url)
                token = mgr.get_internal_token()
                internal_headers = {"Authorization": f"Bearer {token}"}
                auth_headers = {"Authorization": "Bearer tok-0000"}
                outside_dir = str((self.tmp_root / "outside_board").resolve())

                async with httpx.AsyncClient(timeout=10.0) as client:
                    # /internal/board/* rejects outside board_dir with 403
                    r_403 = await client.post(
                        f"{url}/internal/board/read",
                        headers=internal_headers,
                        json={"board_dir": outside_dir},
                    )
                    self.assertEqual(r_403.status_code, 403)

                    # Oversized body (>2 MiB) returns 413
                    big_body = json.dumps({"summary": "x" * (2 * 1024 * 1024 + 1024)}).encode("utf-8")
                    r_413 = await client.post(
                        f"{url}/internal/board/post",
                        headers={**internal_headers, "Content-Type": "application/json"},
                        content=big_body,
                    )
                    self.assertEqual(r_413.status_code, 413)

                    # Invalid pageSize returns 400
                    r_400 = await client.get(f"{url}/v1/tasks?pageSize=notanint", headers=auth_headers)
                    self.assertEqual(r_400.status_code, 400)

                    # Conflicting claim returns 409
                    r_c1 = await client.post(
                        f"{url}/internal/board/claim",
                        headers=internal_headers,
                        json={"swarmId": "http-claim-test", "scope": "api/*", "holder": "w1"},
                    )
                    self.assertEqual(r_c1.status_code, 200)
                    r_c2 = await client.post(
                        f"{url}/internal/board/claim",
                        headers=internal_headers,
                        json={"swarmId": "http-claim-test", "scope": "api/users.py", "holder": "w2"},
                    )
                    self.assertEqual(r_c2.status_code, 409)

                    with patch.object(mcp_srv, "_swarm_mgr", mgr), patch.object(mcp_srv, "_http_ctrl", ctrl):
                        init_res = json.loads(
                            await mcp_srv.swarm_manage(
                                action="init",
                                swarm_id="conv-norm-host",
                                primary_conversation_id="conv-norm-host",
                            )
                        )
                        self.assertTrue(init_res["observer_ui_url"].startswith("http://127.0.0.1:"))

                        # Standalone /board/ui routes (/board/preload.js & /board/api/swarm) return 200
                        r_preload = await client.get(f"{url}/board/preload.js", headers=auth_headers)
                        self.assertEqual(r_preload.status_code, 200)
                        self.assertIn("window.sidecar", r_preload.text)
                        r_api_swarm = await client.get(
                            f"{url}/board/api/swarm?conversationId=conv-norm-host",
                            headers=auth_headers,
                        )
                        self.assertEqual(r_api_swarm.status_code, 200)
                        self.assertEqual(r_api_swarm.json()["swarm"], "conv-norm-host")

                        # swarm_workspace accepts `workspace` alias for `workdir`
                        ws_alias_dir = st_dir / "ws-alias-test"
                        ws_alias_dir.mkdir(parents=True, exist_ok=True)
                        ws_res = json.loads(
                            await mcp_srv.swarm_workspace(
                                action="set_workdir",
                                workspace=str(ws_alias_dir),
                                conversation_id="conv-norm-host",
                            )
                        )
                        self.assertTrue(ws_res["workdir"].lower().endswith("ws-alias-test"))
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run())

    def test_runtime_iter_stream_lines_socketpair_and_mention_router_reenqueue(self) -> None:
        async def _run() -> None:
            # _iter_stream_lines handles >64KB normal line and oversized line without LimitOverrunError
            reader = asyncio.StreamReader()
            line_100k = b"a" * 100_000
            line_over = b"b" * 2048
            reader.feed_data(line_100k + b"\n" + line_over + b"\n")
            reader.feed_eof()
            got = [item async for item in runtime_mod._iter_stream_lines(reader, max_line_bytes=150_000, chunk_size=4096)]
            self.assertEqual(got, [(line_100k, False), (line_over, False)])

            reader2 = asyncio.StreamReader()
            huge_line = b"z" * 25_000
            reader2.feed_data(huge_line + b"\nok\n")
            reader2.feed_eof()
            got2 = [item async for item in runtime_mod._iter_stream_lines(reader2, max_line_bytes=4096, chunk_size=1024)]
            self.assertEqual(len(got2), 2)
            self.assertTrue(got2[0][1])
            self.assertEqual(got2[1], (b"ok", False))

            # MentionRouter re-enqueues un-dispatched batch items if _wake_worker raises
            svc = board_mod.BoardService(state_dir=self.state_dir)
            sid = "harden-wake-reenqueue"
            svc.upsert_member("worker-r", role="worker", state="waiting", swarm_id=sid)
            post = svc.post_message(sender="coordinator", summary="@worker-r run", to=["worker-r"], swarm_id=sid)

            async def _failing_wake(_sid: str, _target: str, _prompt: str) -> bool:
                raise RuntimeError("transient wake failure")

            router = wakeup_mod.MentionRouter(
                board_service=svc,
                wake_worker=_failing_wake,
                debounce_seconds=0.01,
            )
            ok = await router._dispatch_worker_wake(sid, "worker-r", [post])
            self.assertFalse(ok)
            b_after = svc.read_board(sid)
            pending = b_after.get("pendingMentions", {}).get("worker-r") or []
            self.assertEqual([m["seq"] for m in pending], [post["seq"]])

        asyncio.run(_run())

        # _safe_socketpair succeeds on Windows Python 3.12+ (including AF_UNIX/None)
        orig_sp = win_launcher.socket.socketpair
        orig_fsp = getattr(win_launcher.socket, "_fallback_socketpair", None)
        try:
            win_launcher._install_safe_socketpair()
            af_unix = getattr(win_launcher.socket, "AF_UNIX", None)
            families = (None, win_launcher.socket.AF_INET, af_unix) if sys.platform == "win32" else (None, af_unix)
            for fam in families:
                kwargs = {"family": fam} if fam is not None else {}
                s1, s2 = win_launcher.socket.socketpair(**kwargs)
                try:
                    s1.sendall(b"ping")
                    self.assertEqual(s2.recv(4), b"ping")
                finally:
                    s1.close()
                    s2.close()
        finally:
            win_launcher.socket.socketpair = orig_sp
            if orig_fsp is not None:
                win_launcher.socket._fallback_socketpair = orig_fsp

    def test_v3_context_limits_token_economy_and_tps(self) -> None:
        # 1. _resolve_context_limit for 1M variants, deepseek-v4, and config.yaml fallback
        hermes_home = self.tmp_root / "hermes_home_v3_ctx"
        hermes_home.mkdir(parents=True, exist_ok=True)
        (hermes_home / "config.yaml").write_text(
            "\n".join([
                "model:",
                "  default: custom-local-model",
                "  context_length: 131072",
                "custom_providers:",
                "  - name: custom-provider",
                "    models:",
                "      custom-1m-model:",
                "        context_length: 1048576",
                "      my-cp-model:",
                "        context_length: 524288",
            ]),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
            self.assertEqual(runtime_mod._resolve_context_limit("custom-1m-model", "custom:custom-provider", "https://example.invalid/v1"), 1_048_576)
            self.assertEqual(runtime_mod._resolve_context_limit("model-1m-preview", None, None), 1_000_000)
            self.assertEqual(runtime_mod._resolve_context_limit("deepseek-v4", None, None), 1_000_000)
            self.assertEqual(runtime_mod._resolve_context_limit("my-cp-model", None, None), 524_288)
            self.assertEqual(runtime_mod._resolve_context_limit("custom-local-model", None, None), 131_072)

        # 2. _normalize_tokens_dict billable token economy
        norm = runtime_mod._normalize_tokens_dict(
            {"input": 100_000, "cacheRead": 80_000, "cacheWrite": 5_000, "output": 1_200, "contextUsed": 524_288},
            context_limit=1_048_576,
        )
        self.assertEqual(norm["pureInput"], 20_000)
        self.assertEqual(norm["cachedInput"], 80_000)
        self.assertEqual(norm["cacheWrite"], 5_000)
        self.assertEqual(norm["output"], 1_200)
        self.assertEqual(norm["new"], 21_200)
        self.assertEqual(norm["total"], 106_200)
        self.assertEqual(norm["totalProcessed"], 106_200)
        self.assertEqual(norm["contextLimit"], 1_048_576)
        self.assertEqual(norm["contextPct"], 50.0)

        # 3. compute_swarm_telemetry aggregation + lowCacheHitAlert + compactionWarningCount
        members_low_cache = [
            {
                "name": "w1",
                "role": "worker",
                "status": "working",
                "tps": 40.0,
                "elapsedMs": 10_000,
                "tokens": {
                    "pureInput": 90_000,
                    "cachedInput": 10_000,
                    "cacheWrite": 2_000,
                    "output": 400,
                    "totalProcessed": 102_400,
                    "contextPct": 75.0,
                },
            },
            {
                "name": "w2",
                "role": "worker",
                "status": "waiting",
                "elapsedMs": 5_000,
                "tokens": {
                    "pureInput": 10_000,
                    "cachedInput": 5_000,
                    "cacheWrite": 1_000,
                    "output": 200,
                    "totalProcessed": 16_200,
                    "contextPct": 20.0,
                },
            },
        ]
        tel = http_server.compute_swarm_telemetry(members_low_cache)
        self.assertEqual(tel["pureInputTokens"], 100_000)
        self.assertEqual(tel["cachedInputTokens"], 15_000)
        self.assertEqual(tel["cacheWriteTokens"], 3_000)
        self.assertEqual(tel["outputTokens"], 600)
        self.assertEqual(tel["totalProcessedTokens"], 118_600)
        self.assertEqual(tel["totalTokens"], 118_600)
        self.assertAlmostEqual(tel["cacheHitRate"], round(15_000 / 115_000, 4))
        self.assertEqual(tel["compactionWarningCount"], 1)
        self.assertTrue(tel["lowCacheHitAlert"])

        # High cache hit rate (>25%) -> lowCacheHitAlert is False
        members_high_cache = [
            {
                "name": "w1",
                "role": "worker",
                "status": "waiting",
                "tokens": {"pureInput": 20_000, "cachedInput": 80_000, "cacheWrite": 0, "output": 500, "contextPct": 10.0},
            }
        ]
        tel_hi = http_server.compute_swarm_telemetry(members_high_cache)
        self.assertFalse(tel_hi["lowCacheHitAlert"])
        self.assertEqual(tel_hi["compactionWarningCount"], 0)

        # 4. Resumed task TPS uses delta output since prior_output_tokens + streaming fallback tpsEstimated=True
        resume_script = self.tmp_root / "fake_v3_resume_worker.py"
        resume_script.write_text(
            "\n".join([
                "import json, sys, time",
                "turn_file = sys.argv[1]",
                "with open(turn_file, 'r', encoding='utf-8') as fh:",
                "    mode = fh.read().strip()",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'qwen3-coder-30b', 'session_id': 'hs-v3-tps'}), flush=True)",
                "time.sleep(0.35)",
                "if mode == 'turn1':",
                "    print(json.dumps({'type': 'text', 'text': 'turn1'}), flush=True)",
                "    print(json.dumps({'type': 'result', 'session_id': 'hs-v3-tps', 'exit_code': 0, 'duration_ms': 1000, 'text': 'turn1', 'tokens': {'input': 2000, 'cacheRead': 0, 'output': 1000}}), flush=True)",
                "elif mode == 'turn2':",
                "    print(json.dumps({'type': 'text', 'text': 'turn2'}), flush=True)",
                "    print(json.dumps({'type': 'result', 'session_id': 'hs-v3-tps', 'exit_code': 0, 'duration_ms': 1000, 'text': 'turn2', 'tokens': {'input': 2500, 'cacheRead': 2000, 'output': 1020}}), flush=True)",
                "else:",
                "    print(json.dumps({'type': 'text', 'text': 'x' * 400}), flush=True)",
                "    print(json.dumps({'type': 'result', 'session_id': 'hs-v3-tps-est', 'exit_code': 0, 'duration_ms': 1000, 'text': 'x' * 400, 'tokens': {'input': 100, 'cacheRead': 0, 'output': 0}}), flush=True)",
            ]),
            encoding="utf-8",
        )
        mode_file = self.tmp_root / "v3_tps_mode.txt"

        async def _run_tps() -> None:
            ws_dir = self.tmp_root / "ws_v3_tps"
            ws_dir.mkdir(parents=True, exist_ok=True)
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-v3-tps"
            mgr.store.upsert_meta(sid, title="V3 TPS", workspace=str(ws_dir), primary_conversation_id=sid)
            rt = mgr.get_runtime(sid)
            try:
                with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                    with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(resume_script), str(mode_file)], str(ws_dir))):
                        mode_file.write_text("turn1", encoding="utf-8")
                        spawn1 = await rt.spawn_workers(
                            name="w-tps",
                            prompt="turn 1",
                            workdir=str(ws_dir),
                            model="qwen3-coder-30b",
                            return_immediately=False,
                        )
                        t1 = spawn1["workers"][0]
                        cid = t1["contextId"]

                        import sqlite3
                        db_tps = hermes_home / "state.db"
                        con_tps = sqlite3.connect(str(db_tps))
                        con_tps.execute(
                            "CREATE TABLE sessions ("
                            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, "
                            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
                            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
                            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
                            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
                            "billing_provider TEXT, billing_base_url TEXT)"
                        )
                        con_tps.execute(
                            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            ("hs-v3-tps", None, "cli", 2000, 1000, 0, 0, 0, 0.0, None, "qwen3-coder-30b", "{}", 2, 0, 100.0, 101.0, "custom", "https://example.invalid/v1"),
                        )
                        con_tps.commit()
                        con_tps.close()

                        mode_file.write_text("turn2", encoding="utf-8")
                        t2 = await rt.send_message(
                            message_parts=[models.text_part("turn 2")],
                            context_id=cid,
                            config=models.WorkerConfig(worker_name="w-tps", role="worker", model="qwen3-coder-30b"),
                            return_immediately=False,
                        )
                        act2 = t2["metadata"]["liveActivity"]
                        self.assertEqual(act2["turnOutputTokens"], 20)
                        self.assertLess(act2["tps"], 100.0)
                        self.assertFalse(act2["tpsEstimated"])

                        mode_file.write_text("turn3", encoding="utf-8")
                        spawn3 = await rt.spawn_workers(
                            name="w-est",
                            prompt="stream fallback",
                            workdir=str(ws_dir),
                            model="qwen3-coder-30b",
                            return_immediately=False,
                        )
                        t3 = await rt.get_task(spawn3["workers"][0]["taskId"])
                        act3 = t3["metadata"]["liveActivity"]
                        self.assertEqual(act3["turnOutputTokens"], 0)
                        self.assertTrue(act3["tpsEstimated"])
                        self.assertGreater(act3["tps"], 0.0, msg=f"act3={act3}, t3_status={t3.get('status')}")
            finally:
                await mgr.shutdown()

        asyncio.run(_run_tps())

    def test_v3_board_archive_100_default_and_ghost_dir_guard(self) -> None:
        import inspect
        import io
        from contextlib import redirect_stdout
        import server as mcp_srv

        # Default limit=100 across board.py, server.py, board_cli.py
        self.assertEqual(inspect.signature(board_mod.BoardService.read_messages).parameters["limit"].default, 100)
        self.assertEqual(inspect.signature(mcp_srv.swarm_board_read).parameters["limit"].default, 100)
        cli_sid = "v3-cli-lim"
        cli_svc = board_mod.BoardService(state_dir=self.state_dir)
        for i in range(1, 26):
            cli_svc.post_message(sender="coordinator", summary=f"cli-{i}", swarm_id=cli_sid)
        cli_bdir = store.resolve_swarm_paths(cli_sid, state_dir=self.state_dir).board_file.parent
        with redirect_stdout(io.StringIO()) as cli_buf:
            rc = board_cli.main(["--board-dir", str(cli_bdir), "--swarm", cli_sid, "read"])
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(cli_buf.getvalue())["messages"]), 25)

        svc = board_mod.BoardService(state_dir=self.state_dir)
        sid = "v3-arch-board"
        for i in range(1, 7):
            svc.post_message(sender="coordinator", summary=f"live-{i}", swarm_id=sid)
        paths = store.resolve_swarm_paths(sid, state_dir=self.state_dir)
        # Write older messages (seq 100? no, seq -2? use seq 1..3 in archive and live 4..6? Better: write archive with seq 1,2 and live already has 1..6, or use fresh sid)
        sid2 = "v3-arch-board-2"
        paths2 = store.resolve_swarm_paths(sid2, state_dir=self.state_dir, create=True)
        arch_file = paths2.board_file.parent / "board_archive.jsonl"
        arch_file.write_text(
            "\n".join([
                json.dumps({"seq": 1, "sender": "coordinator", "summary": "arch-one needle-z", "channel": "main", "type": "note", "mentions": []}),
                json.dumps({"seq": 2, "sender": "coordinator", "summary": "arch-two", "channel": "main", "type": "note", "mentions": []}),
            ]) + "\n",
            encoding="utf-8",
        )
        # Seed live board with seq 3..5
        board_doc = {
            "schemaVersion": 2,
            "swarmId": sid2,
            "members": {},
            "channels": {"main": {"name": "main", "visibility": "public"}},
            "claims": {},
            "pendingMentions": {},
            "messages": [
                {"seq": 3, "sender": "coordinator", "summary": "live-three", "channel": "main", "type": "note", "mentions": []},
                {"seq": 4, "sender": "coordinator", "summary": "live-four", "channel": "main", "type": "note", "mentions": []},
                {"seq": 5, "sender": "coordinator", "summary": "live-five", "channel": "main", "type": "note", "mentions": []},
            ],
        }
        paths2.board_file.write_text(json.dumps(board_doc), encoding="utf-8")

        tail = svc.read_messages(swarm_id=sid2, since=0, limit=2)
        self.assertEqual([m["seq"] for m in tail["messages"]], [4, 5])

        full_arch = svc.read_messages(swarm_id=sid2, since=0, include_archive=True, full=True)
        self.assertEqual([m["seq"] for m in full_arch["messages"]], [1, 2, 3, 4, 5])

        bef = svc.read_messages(swarm_id=sid2, include_archive=True, before_seq=4, search="needle-z")
        self.assertEqual([m["seq"] for m in bef["messages"]], [1])

        # HTTP /internal/board/read on unknown swarm ID must NOT create ghost directory on disk
        async def _run_ghost() -> None:
            import httpx

            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            try:
                tok = mgr.get_internal_token()
                ghost_sid = "unknown-ghost-swarm-v3"
                ghost_dir = store.resolve_swarm_paths(ghost_sid, state_dir=self.state_dir, create=False).swarm_dir
                self.assertFalse(ghost_dir.exists())
                async with httpx.AsyncClient(timeout=5.0) as client:
                    resp = await client.post(
                        f"{base_url}/internal/board/read",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"swarmId": ghost_sid, "since": 0},
                    )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertTrue(data["ok"])
                self.assertEqual(data["messages"], [])
                self.assertFalse(ghost_dir.exists())
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run_ghost())

    def test_v3_logs_sse_task_stream_and_member_session_history(self) -> None:
        import sqlite3

        # 1. store.append_swarm_log writes events.jsonl and rotates safely
        log_sid = "v3-log-test"
        log_path = store.append_swarm_log(log_sid, "events", {"event": "boot", "detail": "ok"}, state_dir=self.state_dir)
        self.assertIsNotNone(log_path)
        self.assertTrue(log_path.is_file())
        self.assertEqual(log_path.name, "events.jsonl")
        rec = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["event"], "boot")

        # 2. query_member_session_history + GET /board/member/history from mock state.db (?mode=ro)
        hermes_home = self.tmp_root / "hermes_home_v3_hist"
        hermes_home.mkdir(parents=True, exist_ok=True)
        db_path = hermes_home / "state.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT, title TEXT)"
        )
        con.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, timestamp REAL, "
            "content TEXT, reasoning TEXT, reasoning_content TEXT, display_metadata TEXT, "
            "tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, compacted INTEGER)"
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-hist-1", None, "cli", 1000, 300, 5000, 0, 120, 0.0, None, "qwen3-coder-30b", "{}", 3, 1, 100.0, None, "custom", "https://example.invalid/v1", "Main"),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-hist-1", "assistant", 101.0, "Running check", "Thinking step", None, json.dumps({"reasoning_tokens": 42}), json.dumps([{"id": "tc-1", "name": "read_file", "arguments": {"path": "a.py"}}]), None, None, 0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-hist-1", "tool", 102.0, "file contents", None, None, "{}", "[]", "read_file", "tc-1", 0),
        )
        con.commit()
        con.close()

        async def _run_sse_and_hist() -> None:
            import httpx

            ws_dir = self.tmp_root / "ws_v3_hist"
            ws_dir.mkdir(parents=True, exist_ok=True)
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-v3-hist"
            mgr.store.upsert_meta(sid, title="V3 History & SSE", workspace=str(ws_dir), primary_conversation_id=sid)
            rt = mgr.get_runtime(sid)
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            try:
                # Seed task + member for history lookup
                t_id = "task-hist-1"
                ctx_id = "ctx-hist-1"
                with rt._state_rlock:
                    rt._tasks[t_id] = {
                        "id": t_id,
                        "contextId": ctx_id,
                        "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                        "metadata": {
                            "workerConfig": {"worker_name": "w-hist", "role": "worker", "model": "qwen3-coder-30b"},
                            "liveActivity": {"hermesSessionId": "hs-hist-1", "tps": 25.5, "tpsEstimated": False},
                            "tokens": {"pureInput": 1000, "cachedInput": 5000, "output": 300},
                        },
                    }
                    rt._context_sessions[ctx_id] = "hs-hist-1"
                rt._save_state()
                mgr.board_service.upsert_member("w-hist", role="worker", state="working", task_id=t_id, swarm_id=sid)

                with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                    hist = runtime_mod.query_member_session_history(self.state_dir, sid, "w-hist")
                    self.assertEqual(hist["sessionId"], "hs-hist-1")
                    self.assertEqual(len(hist["messages"]), 2)
                    self.assertEqual(hist["messages"][0]["reasoning"], "Thinking step")
                    self.assertEqual(hist["messages"][0]["reasoningTokens"], 42)
                    self.assertEqual(hist["messages"][0]["toolCalls"][0]["id"], "tc-1")
                    self.assertEqual(hist["messages"][1]["toolName"], "read_file")
                    self.assertEqual(hist["messages"][1]["toolCallId"], "tc-1")

                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r_hist = await client.get(
                            f"{base_url}/board/member/history",
                            params={"conversationId": sid, "member": "w-hist"},
                        )
                        self.assertEqual(r_hist.status_code, 200)
                        j_hist = r_hist.json()
                        self.assertEqual(j_hist["member"], "w-hist")
                        self.assertEqual(len(j_hist["messages"]), 2)

                # _sse_task_stream registers in _ACTIVE_SSE_QUEUES and cleans up
                init_task = {"id": t_id, "status": {"state": models.TaskState.WORKING.value}}
                gen = http_server._sse_task_stream(rt, t_id, 1, initial_task=init_task, is_rest=True)
                first_chunk = await gen.__anext__()
                self.assertIn("event: snapshot", first_chunk)
                self.assertGreaterEqual(len(http_server._ACTIVE_SSE_QUEUES), 1)
                await gen.aclose()
                self.assertEqual(len(http_server._ACTIVE_SSE_QUEUES), 0)

                # _MAX_SSE_SUBSCRIBERS cap returns 503
                with patch.object(http_server, "_MAX_SSE_SUBSCRIBERS", 0):
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r_503 = await client.get(f"{base_url}/v1/tasks/{t_id}:subscribe")
                        self.assertEqual(r_503.status_code, 503)
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run_sse_and_hist())

    def test_v3_killed_worker_gateway_interrupt_task_id_sweep_and_403_post(self) -> None:
        async def _run_kill_guard() -> None:
            import httpx

            ws_dir = self.tmp_root / "ws_v3_kill_guard"
            ws_dir.mkdir(parents=True, exist_ok=True)
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-v3-kill-guard"
            mgr.store.upsert_meta(sid, title="V3 Kill Guard", workspace=str(ws_dir), primary_conversation_id=sid)
            rt = mgr.get_runtime(sid)
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            tok = mgr.get_internal_token()
            try:
                t_id = "task-kill-guard-1"
                ctx_id = "ctx-kill-guard-1"
                with rt._state_rlock:
                    rt._tasks[t_id] = {
                        "id": t_id,
                        "contextId": ctx_id,
                        "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                        "metadata": {
                            "workerConfig": {"worker_name": "z-worker", "role": "worker", "model": "demo-model"},
                            "liveActivity": {"hermesSessionId": "hs-zombie-1"},
                        },
                    }
                    rt._context_sessions[ctx_id] = "hs-zombie-1"
                rt._save_state()
                mgr.board_service.upsert_member("z-worker", role="worker", state="working", task_id=t_id, swarm_id=sid)

                interrupted_sids: list[str] = []
                sweep_calls: list[dict[str, Any]] = []

                async def _fake_interrupt(session_id: str, **_kw: Any) -> bool:
                    interrupted_sids.append(session_id)
                    return True

                async def _fake_sweep(**kwargs: Any) -> list[int]:
                    sweep_calls.append(dict(kwargs))
                    return [42424]

                with patch.object(win_launcher, "interrupt_hermes_gateway_session", side_effect=_fake_interrupt):
                    with patch.object(win_launcher, "find_and_kill_orphan_worker_processes", side_effect=_fake_sweep):
                        res = await rt.kill_worker("z-worker", reason="Verify gateway interrupt + prompt-task sweep")

                self.assertTrue(res["killed"])
                self.assertEqual(res["killedOrphanPids"], [42424])
                self.assertIn("hs-zombie-1", interrupted_sids)
                self.assertEqual(len(sweep_calls), 1)
                self.assertIn(t_id, sweep_calls[0].get("task_ids") or [])

                # Killed member cannot post via BoardService or internal HTTP POST
                with self.assertRaises(PermissionError):
                    mgr.board_service.post_message(sender="z-worker", summary="Zombie post", swarm_id=sid)

                async with httpx.AsyncClient(timeout=5.0) as client:
                    r_post = await client.post(
                        f"{base_url}/internal/board/post",
                        headers={"Authorization": f"Bearer {tok}"},
                        json={"swarmId": sid, "sender": "z-worker", "summary": "Zombie HTTP post"},
                    )
                    self.assertEqual(r_post.status_code, 403)
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run_kill_guard())

        # Also verify win_launcher.find_and_kill_orphan_worker_processes matches win_launcher prompt-task-<tid>
        fake_rows = [
            {
                "pid": 998877,
                "ppid": 1,
                "name": "python.exe",
                "cmdline": "python.exe win_launcher.py -- chat --query-file C:/tmp/swarms/s1/prompt-task-abc123.txt",
                "cwd": "",
            },
            {
                "pid": 998878,
                "ppid": 1,
                "name": "python.exe",
                "cmdline": "python.exe win_launcher.py -- chat --query-file C:/tmp/swarms/s1/prompt-task-other999.txt",
                "cwd": "",
            },
        ]
        killed_pids: list[int] = []

        async def _fake_tree_kill(pid_or_proc: Any, **_kw: Any) -> None:
            killed_pids.append(int(pid_or_proc))

        with patch.object(win_launcher, "_list_process_rows", return_value=fake_rows):
            with patch.object(win_launcher, "kill_process_tree", side_effect=_fake_tree_kill):
                matched = asyncio.run(
                    win_launcher.find_and_kill_orphan_worker_processes(
                        swarm_id="s1",
                        worker_name="z-worker",
                        task_ids=["task-abc123"],
                    )
                )
        self.assertEqual(matched, [998877])
        self.assertEqual(killed_pids, [998877])

    def test_v3_member_history_scope_state_timestamps_live_overlay_and_subagent_guard(self) -> None:
        import sqlite3

        hermes_home = self.tmp_root / "hermes_home_v3_full"
        hermes_home.mkdir(parents=True, exist_ok=True)
        db_path = hermes_home / "state.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT, title TEXT)"
        )
        con.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, timestamp REAL, "
            "content TEXT, reasoning TEXT, reasoning_content TEXT, display_metadata TEXT, "
            "tool_calls TEXT, tool_name TEXT, tool_call_id TEXT, compacted INTEGER)"
        )
        # Worker w-main session + its child subagent + an unrelated foreign subagent
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-main-1", None, "cli", 400, 120, 1000, 0, 50, 0.0, None, "demo-model", "{}", 4, 2, 1795799100.0, None, "custom", "https://example.invalid/v1", "Worker Main"),
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-sub-own", "hs-main-1", "subagent", 80, 25, 0, 0, 0, 0.0, None, "demo-sub-model", "{}", 1, 0, 1795799120.0, None, "custom", "https://example.invalid/v1", "Own Sub"),
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-foreign-parent", None, "cli", 999, 999, 0, 0, 0, 0.0, None, "secret-model", "{}", 1, 0, 1795799100.0, None, "custom", "https://example.invalid/v1", "Foreign Parent"),
        )
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-sub-foreign", "hs-foreign-parent", "subagent", 777, 111, 0, 0, 0, 0.0, None, "secret-model", "{}", 1, 0, 1795799130.0, None, "custom", "https://example.invalid/v1", "Foreign Sub"),
        )
        # Messages in hs-main-1: float epoch sec, ms epoch, and 0/null edge cases
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-main-1", "user", 1795799157.445611, "Do work", None, None, "{}", "[]", None, None, 0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-main-1", "assistant", 1795799160123, "Checking", "Step think", None, json.dumps({"reasoning_tokens": 18}), json.dumps([{"id": "tc-committed", "name": "read_file", "arguments": {"path": "a.py"}}]), None, None, 0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-main-1", "tool", 0.0, "ok", None, None, "{}", "[]", "read_file", "tc-committed", 0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-sub-own", "assistant", 1795799165.5, "Own subagent reply", None, None, "{}", "[]", None, None, 0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, reasoning, reasoning_content, display_metadata, tool_calls, tool_name, tool_call_id, compacted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("hs-sub-foreign", "assistant", 1795799166.5, "SECRET FOREIGN DATA", None, None, "{}", "[]", None, None, 0),
        )
        con.commit()
        con.close()

        async def _run_full_history_checks() -> None:
            import httpx

            ws_dir = self.tmp_root / "ws_v3_full_hist"
            ws_dir.mkdir(parents=True, exist_ok=True)
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-v3-full-hist"
            coord_cid = "coord-v3-full-hist"
            mgr.store.upsert_meta(
                sid,
                title="Full History Spec",
                workspace=str(ws_dir),
                primary_conversation_id=sid,
                coordinator_conversation_id=coord_cid,
            )
            rt = mgr.get_runtime(sid)
            ctrl = http_server.EmbeddedA2AHttpController(mgr)
            base_url = ctrl.start(host="127.0.0.1", port=0)
            try:
                t_id = "task-full-1"
                ctx_id = "ctx-full-1"
                with rt._state_rlock:
                    rt._tasks[t_id] = {
                        "id": t_id,
                        "contextId": ctx_id,
                        "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                        "metadata": {
                            "createdAt": models.now_iso(),
                            "workerConfig": {"worker_name": "w-main", "role": "worker", "model": "demo-model"},
                            "liveActivity": {
                                "hermesSessionId": "hs-main-1",
                                "startedAt": models.now_iso(),
                                "tps": 31.2,
                                "tpsEstimated": True,
                                "currentTool": {"name": "grep", "preview": "pattern=foo", "startedAt": models.now_iso()},
                                "recentTools": [{"name": "grep", "preview": "pattern=foo", "status": "running", "durationMs": 0}],
                            },
                            "tokens": {"pureInput": 400, "cachedInput": 1000, "output": 120},
                        },
                    }
                    rt._context_sessions[ctx_id] = "hs-main-1"
                # Seed live buffers (including committed tc-committed and uncommitted tc-live)
                rt._append_live_reasoning(t_id, "Live thought delta")
                rt._append_live_transcript(t_id, "Live answer delta")
                rt._append_live_tool_trace(t_id, {"event": "tool_use", "name": "read_file", "tool_call_id": "tc-committed", "input": {"path": "a.py"}})
                rt._append_live_tool_trace(t_id, {"event": "tool_result", "name": "read_file", "tool_call_id": "tc-committed", "duration_ms": 5, "is_error": False, "output_preview": "ok"})
                rt._append_live_tool_trace(t_id, {"event": "tool_use", "name": "grep", "tool_call_id": "tc-live", "input": {"pattern": "foo"}})
                rt._save_state()

                # Deliberately omit member_rec state or set raw TASK_STATE_WORKING on a second worker
                mgr.board_service.upsert_member(
                    "w-main",
                    role="worker",
                    state="working",
                    task_id=t_id,
                    swarm_id=sid,
                )
                mgr.board_service.upsert_subagents(
                    "w-main",
                    [{"id": "sub-1", "sessionId": "hs-sub-own", "label": "Own Sub", "status": "working"}],
                    swarm_id=sid,
                )

                # Verify get_live_worker_overlay returns live buffers + telemetry
                overlay = rt.get_live_worker_overlay("w-main")
                self.assertIsNotNone(overlay)
                self.assertTrue(overlay["active"])
                self.assertEqual(overlay["state"], "working")
                self.assertEqual(overlay["reasoning"], "Live thought delta")
                self.assertEqual(overlay["transcript"], "Live answer delta")
                self.assertEqual(len(overlay["toolTrace"]), 3)

                # Verify build_observer_snapshot includes subagent sessionId
                snap = http_server.build_observer_snapshot(mgr, sid)
                w_snap = next(m for m in snap["members"] if m["name"] == "w-main")
                self.assertEqual(w_snap["subagents"][0]["sessionId"], "hs-sub-own")

                with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                    # 1. Full query with runtime overlay + scope keys + timestampMs + dedup
                    res = runtime_mod.query_member_session_history(self.state_dir, sid, "w-main", runtime=rt)
                    self.assertTrue(res["ok"])
                    self.assertEqual(res["swarm"], sid)
                    self.assertEqual(res["swarmId"], sid)
                    self.assertEqual(res["primaryConversationId"], sid)
                    self.assertEqual(res["coordinatorConversationId"], coord_cid)
                    self.assertEqual(res["conversationId"], sid)
                    self.assertIn(sid, res["conversationIds"])
                    self.assertIn(coord_cid, res["conversationIds"])
                    self.assertEqual(res["state"], "working")
                    self.assertEqual(res["messages"][0]["timestampMs"], 1795799157446)
                    self.assertEqual(res["messages"][1]["timestampMs"], 1795799160123)
                    self.assertIsNone(res["messages"][2]["timestampMs"])
                    self.assertTrue(res["live"]["active"])
                    self.assertEqual(res["live"]["reasoning"], "Live thought delta")
                    self.assertEqual(res["live"]["transcript"], "Live answer delta")
                    self.assertEqual(res["live"]["currentTool"]["name"], "grep")
                    # Committed tc-committed MUST be deduped, only tc-live remains
                    trace_ids = [item.get("tool_call_id") for item in res["live"]["toolTrace"]]
                    self.assertEqual(trace_ids, ["tc-live"])

                    # 2. Incremental poll after_id=3 must STILL dedup tc-committed from earlier DB rows
                    res_inc = runtime_mod.query_member_session_history(self.state_dir, sid, "w-main", after_id=3, runtime=rt)
                    self.assertEqual(res_inc["messages"], [])
                    self.assertEqual([item.get("tool_call_id") for item in res_inc["live"]["toolTrace"]], ["tc-live"])

                    # 3. Valid subagent query returns own subagent messages and suppresses parent live overlay
                    res_sub = runtime_mod.query_member_session_history(
                        self.state_dir, sid, "w-main", subagent_session_id="hs-sub-own", runtime=rt
                    )
                    self.assertEqual(res_sub["sessionId"], "hs-sub-own")
                    self.assertEqual(len(res_sub["messages"]), 1)
                    self.assertEqual(res_sub["messages"][0]["content"], "Own subagent reply")
                    self.assertFalse(res_sub["live"]["active"])
                    self.assertIsNone(res_sub["currentTool"])

                    # 4. Foreign subagent query is rejected cleanly with zero leak
                    res_foreign = runtime_mod.query_member_session_history(
                        self.state_dir, sid, "w-main", subagent_session_id="hs-sub-foreign", runtime=rt
                    )
                    self.assertIsNone(res_foreign["sessionId"])
                    self.assertEqual(res_foreign["messages"], [])
                    self.assertEqual(res_foreign["totalCount"], 0)
                    self.assertFalse(res_foreign["hasMore"])
                    self.assertFalse(res_foreign["live"]["active"])
                    self.assertTrue(res_foreign.get("subagentRejected"))
                    self.assertEqual(res_foreign["tokens"], {})

                    # 5. HTTP GET /board/member/history passes runtime=rt and canonical scope keys
                    async with httpx.AsyncClient(timeout=5.0) as client:
                        r_http = await client.get(
                            f"{base_url}/board/member/history",
                            params={"conversationId": sid, "member": "w-main"},
                        )
                        self.assertEqual(r_http.status_code, 200)
                        j_http = r_http.json()
                        self.assertTrue(j_http["ok"])
                        self.assertEqual(j_http["primaryConversationId"], sid)
                        self.assertEqual(j_http["coordinatorConversationId"], coord_cid)
                        self.assertTrue(j_http["live"]["active"])
                        self.assertEqual(j_http["live"]["reasoning"], "Live thought delta")
                        self.assertEqual([x.get("tool_call_id") for x in j_http["live"]["toolTrace"]], ["tc-live"])

                    # 6. Raw TASK_STATE_WORKING fallback & killed worker suppression
                    with rt._state_rlock:
                        rt._tasks["task-raw-2"] = {
                            "id": "task-raw-2",
                            "contextId": "ctx-raw-2",
                            "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                            "metadata": {
                                "workerConfig": {"worker_name": "w-raw", "role": "worker", "model": "demo-model"},
                                "liveActivity": {"currentTool": {"name": "bash", "preview": "ls"}},
                            },
                        }
                    rt._save_state()
                    res_raw = runtime_mod.query_member_session_history(self.state_dir, sid, "w-raw", runtime=rt)
                    self.assertEqual(res_raw["state"], "working")

                    mgr.board_service.upsert_member("w-main", role="worker", state="killed", killed=True, task_id=t_id, swarm_id=sid)
                    res_killed = runtime_mod.query_member_session_history(self.state_dir, sid, "w-main", runtime=rt)
                    self.assertEqual(res_killed["state"], "killed")
                    self.assertIsNone(res_killed["currentTool"])
                    self.assertFalse(res_killed["live"]["active"])
            finally:
                ctrl.stop()
                await mgr.shutdown()

        asyncio.run(_run_full_history_checks())

        # 7. Verify _read_stdout captures thinking/reasoning/thought events into liveReasoning
        async def _run_stdout_reasoning_capture() -> None:
            ws_dir = self.tmp_root / "ws_v3_stdout_think"
            ws_dir.mkdir(parents=True, exist_ok=True)
            mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
            sid = "conv-v3-stdout-think"
            mgr.store.upsert_meta(sid, title="Stdout Think", workspace=str(ws_dir), primary_conversation_id=sid)
            rt = mgr.get_runtime(sid)

            class _FakeStream:
                def __init__(self, lines: list[bytes]) -> None:
                    self._lines = list(lines)

                async def read(self, _n: int = 65536) -> bytes:
                    if not self._lines:
                        return b""
                    return self._lines.pop(0)

            class _FakeProc:
                def __init__(self, stdout_lines: list[bytes]) -> None:
                    self.pid = 55501
                    self.returncode = None
                    self.stdout = _FakeStream(stdout_lines)
                    self.stderr = _FakeStream([])

                async def wait(self) -> int:
                    self.returncode = 0
                    return 0

            stream_payload = [
                (json.dumps({"type": "system", "subtype": "init", "session_id": "hs-think-1", "model": "demo-model"}) + "\n").encode("utf-8"),
                (json.dumps({"type": "thinking", "text": "A "}) + "\n").encode("utf-8"),
                (json.dumps({"type": "reasoning", "reasoning": "B "}) + "\n").encode("utf-8"),
                (json.dumps({"type": "thought", "thought": "C"}) + "\n").encode("utf-8"),
                (json.dumps({"type": "text", "text": "Final reply"}) + "\n").encode("utf-8"),
                (json.dumps({"type": "result", "exit_code": 0, "text": "Final reply", "session_id": "hs-think-1"}) + "\n").encode("utf-8"),
            ]
            fake_proc = _FakeProc(stream_payload)
            try:
                with patch.object(win_launcher, "spawn_hermes_subprocess", return_value=fake_proc):
                    task = await rt.send_message(
                        message_parts=[models.text_part("Think out loud")],
                        config=models.WorkerConfig(worker_name="w-think", workdir=str(ws_dir), model="demo-model"),
                        return_immediately=False,
                        wait_seconds=10.0,
                    )
                self.assertEqual((task.get("status") or {}).get("state"), models.TaskState.COMPLETED.value)
                self.assertEqual((task.get("metadata") or {}).get("liveReasoning"), "A B C")
                self.assertEqual((task.get("metadata") or {}).get("liveTranscript"), "Final reply")
                overlay_done = rt.get_live_worker_overlay("w-think")
                self.assertIsNotNone(overlay_done)
                self.assertEqual(overlay_done["reasoning"], "A B C")
                self.assertFalse(overlay_done["active"])
            finally:
                await mgr.shutdown()

        asyncio.run(_run_stdout_reasoning_capture())

        # 8. Verify older minimal state.db schema compatibility (missing optional columns)
        legacy_home = self.tmp_root / "hermes_home_legacy_schema"
        legacy_home.mkdir(parents=True, exist_ok=True)
        legacy_db = legacy_home / "state.db"
        lcon = sqlite3.connect(str(legacy_db))
        lcon.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, model TEXT, started_at REAL, ended_at REAL)"
        )
        lcon.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, timestamp REAL, content TEXT)"
        )
        lcon.execute("INSERT INTO sessions VALUES ('hs-leg-1', NULL, 'cli', 'demo-model', 1795799100.0, NULL)")
        lcon.execute("INSERT INTO messages (session_id, role, timestamp, content) VALUES ('hs-leg-1', 'assistant', 1795799110.5, 'Legacy row')")
        lcon.commit()
        lcon.close()

        leg_sid = "conv-v3-legacy-db"
        mgr_leg = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr_leg.store.upsert_meta(leg_sid, title="Legacy DB", primary_conversation_id=leg_sid)
        mgr_leg.board_service.upsert_member("w-leg", role="worker", state="waiting", task_id="t-leg-1", swarm_id=leg_sid)
        leg_paths = store.resolve_swarm_paths(leg_sid, state_dir=self.state_dir, create=True)
        leg_paths.tasks_file.write_text(
            json.dumps({
                "tasks": {
                    "t-leg-1": {
                        "id": "t-leg-1",
                        "contextId": "c-leg-1",
                        "status": {"state": models.TaskState.COMPLETED.value},
                        "metadata": {
                            "workerConfig": {"worker_name": "w-leg", "model": "demo-model"},
                            "liveActivity": {"hermesSessionId": "hs-leg-1"},
                        },
                    }
                }
            }),
            encoding="utf-8",
        )
        with patch.dict(os.environ, {"HERMES_HOME": str(legacy_home)}):
            res_leg = runtime_mod.query_member_session_history(self.state_dir, leg_sid, "w-leg")
        self.assertTrue(res_leg["ok"])
        self.assertEqual(res_leg["state"], "waiting")
        self.assertEqual(len(res_leg["messages"]), 1)
        self.assertEqual(res_leg["messages"][0]["content"], "Legacy row")
        self.assertEqual(res_leg["messages"][0]["timestampMs"], 1795799110500)


class Phase4HardeningRegressionTests(unittest.TestCase):
    """Comprehensive tests for coordinator history/validation, model propagation, telemetry math, SQLite caching, and auth/store hardening."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-p4-harden-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_A2A_HTTP_TOKEN",
                "HERMES_HOME",
                "ANTIGRAVITY_APP_DATA_DIR",
                "HERMES_A2A_SKIP_BRAIN_CHECK",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        for k in (
            "HERMES_A2A_SWARM_ID",
            "HERMES_A2A_HTTP_TOKEN",
            "HERMES_HOME",
            "ANTIGRAVITY_APP_DATA_DIR",
            "HERMES_A2A_SKIP_BRAIN_CHECK",
        ):
            os.environ.pop(k, None)
        self._orig_session_wd = mcp_srv._session_workdir
        self._orig_session_conv = mcp_srv._session_conversation_id
        self._orig_session_swarm = mcp_srv._session_swarm_id
        self._orig_mgr = mcp_srv._swarm_mgr
        self._orig_ctrl = mcp_srv._http_ctrl
        mcp_srv._session_workdir = None
        mcp_srv._session_conversation_id = None
        mcp_srv._session_swarm_id = None
        mcp_srv._swarm_mgr = runtime_mod.SwarmManager(
            state_dir=self.state_dir,
            default_workdir_provider=lambda: mcp_srv._session_workdir,
        )
        mcp_srv._http_ctrl = http_server.EmbeddedA2AHttpController(mcp_srv._swarm_mgr)

    def tearDown(self) -> None:
        try:
            mcp_srv._http_ctrl.stop()
        except Exception:
            pass
        mcp_srv._session_workdir = self._orig_session_wd
        mcp_srv._session_conv = self._orig_session_conv
        mcp_srv._session_conversation_id = self._orig_session_conv
        mcp_srv._session_swarm_id = self._orig_session_swarm
        mcp_srv._swarm_mgr = self._orig_mgr
        mcp_srv._http_ctrl = self._orig_ctrl
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_coordinator_history_model_stats_and_bind_validation(self) -> None:
        app_data = self.tmp_root / "ag_app_data"
        brain_dir = app_data / "brain"
        prim_uuid = "737e69ed-0042-4d71-8d92-6242a30408a9"
        coord_uuid = "9f556b92-cca8-48c0-a956-eb5c05d0871b"
        spliced_uuid = "9f556b92-cca8-4d71-8d92-6242a30408a9"

        # Seed primary subagent record and coordinator transcript_full.jsonl
        sub_dir = brain_dir / prim_uuid / ".system_generated" / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        (sub_dir / f"{coord_uuid}.json").write_text(
            json.dumps({"conversationId": coord_uuid, "subagentDescriptor": {"role": "Swarm Coordinator"}}),
            encoding="utf-8",
        )
        coord_logs = brain_dir / coord_uuid / ".system_generated" / "logs"
        coord_logs.mkdir(parents=True, exist_ok=True)
        steps = [
            {
                "step_index": 0,
                "source": "SYSTEM",
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "created_at": "2026-11-28T01:12:21Z",
                "content": "Boot coordinator",
            },
            {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-28T01:12:25Z",
                "model": "gemini-4-hydrogen-medium",
                "input_tokens": 10000,
                "cache_read_tokens": 0,
                "output_tokens": 400,
                "thinking": "Checking swarm catalog",
                "content": "",
                "tool_calls": [{"name": "swarm_catalog", "args": {"workdir": str(self.repo_dir)}}],
            },
            {
                "step_index": 2,
                "source": "MODEL",
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-28T01:12:26Z",
                "content": "Created At: 2026-11-28T01:12:26Z\nCompleted At: 2026-11-28T01:12:27Z\n{\"ok\": true}",
            },
            {
                "step_index": 3,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-28T01:12:30Z",
                "model": "gemini-4-hydrogen-medium",
                "input_tokens": 1000,
                "cache_read_tokens": 10400,
                "output_tokens": 600,
                "content": "Workers spawned cleanly.",
                "tool_calls": [],
            },
        ]
        (coord_logs / "transcript_full.jsonl").write_text(
            "\n".join(json.dumps(s) for s in steps) + "\n",
            encoding="utf-8",
        )

        async def _run() -> None:
            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}):
                await mcp_srv.swarm_manage(
                    "init",
                    title="Coord Test",
                    primary_conversation_id=prim_uuid,
                    workspace=str(self.repo_dir),
                )
                # Spliced UUID auto-heals cleanly to the real local subagent conversationId
                healed = json.loads(
                    await mcp_srv.swarm_manage(
                        "bind_coordinator",
                        primary_conversation_id=prim_uuid,
                        coordinator_conversation_id=spliced_uuid,
                    )
                )
                self.assertEqual(healed["coordinator_conversation_id"], coord_uuid)
                self.assertEqual(healed["auto_corrected_from"], spliced_uuid)
                self.assertEqual(mcp_srv._swarm_mgr.store.resolve_swarm_for_conversation(coord_uuid), prim_uuid)

                # Unrelated UUID with multiple candidates still fails closed with valid candidate hints
                alt_uuid = "aaaa1111-1111-4111-8111-111111111111"
                (sub_dir / f"{alt_uuid}.json").write_text(
                    json.dumps({"conversationId": alt_uuid, "subagentDescriptor": {"role": "Other"}}),
                    encoding="utf-8",
                )
                with self.assertRaises(ValueError) as ctx_err:
                    await mcp_srv.swarm_manage(
                        "bind_coordinator",
                        primary_conversation_id=prim_uuid,
                        coordinator_conversation_id="00000000-0000-4000-8000-000000000000",
                    )
                self.assertIn(coord_uuid, str(ctx_err.exception))
                (sub_dir / f"{alt_uuid}.json").unlink()

                # Skip check env var allows synthetic UUID
                with patch.dict(os.environ, {"HERMES_A2A_SKIP_BRAIN_CHECK": "1"}):
                    skipped = json.loads(
                        await mcp_srv.swarm_manage(
                            "bind_coordinator",
                            primary_conversation_id=prim_uuid,
                            coordinator_conversation_id=spliced_uuid,
                            model="explicit-coord-model",
                            provider="antigravity",
                        )
                    )
                    self.assertEqual(skipped["model"], "explicit-coord-model")
                    self.assertEqual(skipped["provider"], "antigravity")

                # Valid UUID binds cleanly and auto-extracts model from transcript_full.jsonl
                bound = json.loads(
                    await mcp_srv.swarm_manage(
                        "bind_coordinator",
                        primary_conversation_id=prim_uuid,
                        coordinator_conversation_id=coord_uuid,
                    )
                )
                self.assertEqual(bound["coordinator_conversation_id"], coord_uuid)
                self.assertEqual(bound["model"], "gemini-4-hydrogen-medium")
                self.assertEqual(bound["provider"], "antigravity")

                # Verify ORCHESTRATOR_INSTRUCTIONS alignment
                self.assertIn("get_workdir", mcp_srv.ORCHESTRATOR_INSTRUCTIONS)
                self.assertIn("conversation_id", mcp_srv.ORCHESTRATOR_INSTRUCTIONS)
                self.assertIn("bind_coordinator", mcp_srv.ORCHESTRATOR_INSTRUCTIONS)
                self.assertIn('Model="inherit"', mcp_srv.ORCHESTRATOR_INSTRUCTIONS)

                # Coordinator history + pagination + tokens + TPS + workspace
                hist = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator")
                self.assertTrue(hist["ok"])
                self.assertEqual(hist["model"], "gemini-4-hydrogen-medium")
                self.assertEqual(hist["provider"], "antigravity")
                self.assertEqual(Path(hist["workspace"]), self.repo_dir.resolve())
                self.assertEqual(len(hist["messages"]), 4)
                self.assertEqual(hist["messages"][0]["role"], "system")
                self.assertEqual(hist["messages"][1]["role"], "assistant")
                self.assertEqual(hist["messages"][1]["reasoning"], "Checking swarm catalog")
                self.assertEqual(hist["messages"][1]["toolCalls"][0]["name"], "swarm_catalog")
                self.assertEqual(hist["messages"][2]["role"], "tool")
                self.assertEqual(hist["messages"][2]["toolCallId"], hist["messages"][1]["toolCalls"][0]["id"])
                self.assertEqual(hist["tokens"]["pureInput"], 11000)
                self.assertEqual(hist["tokens"]["cachedInput"], 10400)
                self.assertEqual(hist["tokens"]["output"], 1000)
                # llmActiveMs = (01:12:25 - 01:12:21) + (01:12:30 - 01:12:27) = 7s -> 1000/7 = 142.9 tps
                self.assertGreater(hist["tps"], 100.0)

                hist_inc = runtime_mod.query_member_session_history(
                    self.state_dir, prim_uuid, "coordinator", after_id=hist["messages"][1]["id"]
                )
                self.assertEqual(len(hist_inc["messages"]), 2)

                # Snapshot exposes coordinator model/provider/stats
                snap = http_server.build_observer_snapshot(mcp_srv._swarm_mgr, prim_uuid)
                coord_m = next(m for m in snap["members"] if m["name"] == "coordinator")
                self.assertEqual(coord_m["model"], "gemini-4-hydrogen-medium")
                self.assertEqual(coord_m["provider"], "antigravity")
                self.assertEqual(coord_m["steps"], 2)
                self.assertEqual(coord_m["toolCalls"], 1)

                # Fallback to transcript.jsonl when transcript_full.jsonl is absent
                (coord_logs / "transcript_full.jsonl").unlink()
                (coord_logs / "transcript.jsonl").write_text(json.dumps(steps[1]) + "\n", encoding="utf-8")
                hist_fb = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator")
                self.assertEqual(len(hist_fb["messages"]), 1)
                self.assertEqual(hist_fb["model"], "gemini-4-hydrogen-medium")

        asyncio.run(_run())

    def test_worker_db_extra_model_token_double_count_tps_and_live_step_scoping(self) -> None:
        import sqlite3

        hermes_home = self.tmp_root / "hermes_home_p4"
        hermes_home.mkdir(parents=True, exist_ok=True)
        db_path = hermes_home / "state.db"
        con = sqlite3.connect(str(db_path))
        con.execute(
            "CREATE TABLE sessions ("
            "id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, title TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, "
            "cache_write_tokens INTEGER, reasoning_tokens INTEGER, "
            "estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT)"
        )
        con.execute(
            "CREATE TABLE messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, timestamp REAL, "
            "content TEXT, reasoning TEXT, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT)"
        )
        # Worker session in state.db: pureInput=2000, cacheRead=8000, output=500, model=db-resolved-model
        con.execute(
            "INSERT INTO sessions VALUES (?, NULL, 'cli', 'Worker', 2000, 500, 8000, 0, 0, 0.0, 0.0, ?, ?, 2, 1, ?, ?, 'custom', NULL)",
            (
                "hs-p4-w1",
                "db-resolved-model",
                json.dumps({"gateway_runtime": {"provider": "custom:custom-provider"}}),
                1795800000.0,
                1795800010.0,
            ),
        )
        # Tier-3 subagent with tokens & timings
        con.execute(
            "INSERT INTO sessions VALUES (?, ?, 'subagent', 'Leaf audit', 300, 120, 100, 0, 0, 0.0, 0.0, ?, ?, 2, 1, ?, ?, 'custom', NULL)",
            ("hs-p4-sub1", "hs-p4-w1", "leaf-db-model", json.dumps({"_subagent_id": "sub-1"}), 1795800001.0, 1795800003.0),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, tool_calls) VALUES (?, 'assistant', 1795800001.0, 'Step 1 reply', ?)",
            ("hs-p4-w1", json.dumps([{"id": "tc-s1", "name": "bash", "arguments": {"cmd": "ls"}}])),
        )
        con.execute(
            "INSERT INTO messages (session_id, role, timestamp, content, tool_name, tool_call_id) VALUES (?, 'tool', 1795800002.0, 'ok', 'bash', 'tc-s1')",
            ("hs-p4-w1",),
        )
        con.commit()
        con.close()

        # Fake multi-step worker where result event lacks cacheRead (10000 inclusive) while DB has cacheRead=8000, pureInput=2000
        worker_script = self.tmp_root / "fake_p4_worker.py"
        worker_script.write_text(
            "\n".join([
                "import json, time",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': '', 'session_id': 'hs-p4-w1'}), flush=True)",
                "print(json.dumps({'type': 'thinking', 'text': 'Step1Think'}), flush=True)",
                "print(json.dumps({'type': 'text', 'text': 'Step1Text'}), flush=True)",
                "print(json.dumps({'type': 'tool_use', 'name': 'swarm_wait', 'tool_call_id': 'tc-s1', 'input': {'since': 0}}), flush=True)",
                "time.sleep(0.05)",
                "print(json.dumps({'type': 'tool_result', 'name': 'swarm_wait', 'tool_call_id': 'tc-s1', 'is_error': False, 'output': 'ok', 'duration_ms': 2000}), flush=True)",
                "print(json.dumps({'type': 'thinking', 'text': 'Step2Think'}), flush=True)",
                "print(json.dumps({'type': 'text', 'text': 'Step2Text'}), flush=True)",
                "time.sleep(0.3)",
                "print(json.dumps({'type': 'result', 'session_id': 'hs-p4-w1', 'exit_code': 0, 'duration_ms': 10000, 'text': 'Step2Text', 'tokens': {'input': 10000, 'output': 500}}), flush=True)",
            ]),
            encoding="utf-8",
        )

        async def _run() -> None:
            mgr = mcp_srv._swarm_mgr
            sid = "conv-p4-worker"
            mgr.store.upsert_meta(sid, title="P4 Worker", workspace=str(self.repo_dir), primary_conversation_id=sid)
            rt = mgr.get_runtime(sid)
            prompt = (
                f"You are `backend-dev` in this Hermes swarm (`swarm_id`: `{sid}`, `workdir`: `{self.repo_dir}`).\n\n"
                "Ownership & Locks:\n- You ONLY own `app.py`.\n\n"
                "Task:\n1. Implement token math fix cleanly.\n"
            )
            with patch.dict(os.environ, {"HERMES_HOME": str(hermes_home)}):
                with patch.object(rt, "_build_hermes_command", return_value=([sys.executable, str(worker_script)], str(self.repo_dir))):
                    res = await rt.spawn_workers(
                        name="backend-dev",
                        prompt=prompt,
                        workdir=str(self.repo_dir),
                        return_immediately=False,
                        wait_seconds=15.0,
                    )
                task = res["tasks"][0]
                self.assertEqual(task["status"]["state"], models.TaskState.COMPLETED.value)

                # 1. Step-scoped liveTranscript/liveReasoning reset on step boundary (no Step1 concatenation!)
                overlay = rt.get_live_worker_overlay("backend-dev")
                self.assertIsNotNone(overlay)
                self.assertEqual(overlay["transcript"], "Step2Text")
                self.assertEqual(overlay["reasoning"], "Step2Think")

                # 2. db_extra model & provider propagated to workerConfig, board, snapshot, and history
                self.assertEqual(task["metadata"]["workerConfig"]["model"], "db-resolved-model")
                self.assertEqual(task["metadata"]["workerConfig"]["provider"], "custom")
                board = mgr.board_service.read_board(sid)
                self.assertEqual(board["members"]["backend-dev"]["model"], "db-resolved-model")
                self.assertEqual(board["members"]["backend-dev"]["provider"], "custom")
                self.assertEqual(board["members"]["backend-dev"]["assignment"], "Implement token math fix cleanly.")

                # 3. Cache+uncached double-count prevented: pureInput=2000, cachedInput=8000, output=500 -> totalProcessed=10500 (not 18500!)
                tok = task["metadata"]["tokens"]
                self.assertEqual(tok["pureInput"], 2000)
                self.assertEqual(tok["cachedInput"], 8000)
                self.assertEqual(tok["output"], 500)
                self.assertEqual(tok["totalProcessed"], 10500)

                # 4. avgTps in compute_swarm_telemetry uses llmActiveMs (excluding swarm_wait idle time)
                snap = http_server.build_observer_snapshot(mgr, sid)
                w_snap = next(m for m in snap["members"] if m["name"] == "backend-dev")
                self.assertEqual(w_snap["model"], "db-resolved-model")
                self.assertGreater(w_snap["llmActiveMs"], 0)
                self.assertLess(w_snap["llmActiveMs"], w_snap["elapsedMs"])
                self.assertEqual(
                    snap["telemetry"]["avgTps"],
                    runtime_mod._compute_tps(500, w_snap["llmActiveMs"]),
                )

                # 5. Tier-3 subagent tokens/TPS included in query_member_session_history + SQLite schema caching + incremental after_id
                with patch.object(runtime_mod, "_COMMITTED_TC_CACHE", {}):
                    hist_main = runtime_mod.query_member_session_history(self.state_dir, sid, "backend-dev", runtime=rt)
                    self.assertEqual(hist_main["model"], "db-resolved-model")
                    self.assertEqual(hist_main["provider"], "custom")
                    self.assertEqual(Path(hist_main["workspace"]), self.repo_dir.resolve())
                    self.assertEqual(len(hist_main["subagents"]), 1)
                    self.assertEqual(hist_main["subagents"][0]["tokens"]["output"], 120)
                    self.assertAlmostEqual(hist_main["subagents"][0]["tps"], 60.0)

                    hist_sub = runtime_mod.query_member_session_history(
                        self.state_dir, sid, "backend-dev", subagent_session_id="hs-p4-sub1", runtime=rt
                    )
                    self.assertEqual(hist_sub["model"], "leaf-db-model")
                    self.assertEqual(hist_sub["tokens"]["output"], 120)
                    self.assertAlmostEqual(hist_sub["tps"], 60.0)

                    # Simulate active live overlay and verify incremental poll uses cache without full scan
                    with rt._state_rlock:
                        t_obj = rt._tasks[task["id"]]
                        t_obj["status"]["state"] = models.TaskState.WORKING.value
                        rt._live_buffers[task["id"]]["liveToolTrace"] = [
                            {"event": "tool_use", "name": "bash", "tool_call_id": "tc-s1"},
                            {"event": "tool_use", "name": "grep", "tool_call_id": "tc-s2"},
                        ]
                    h_live1 = runtime_mod.query_member_session_history(self.state_dir, sid, "backend-dev", runtime=rt)
                    self.assertEqual([x["tool_call_id"] for x in h_live1["live"]["toolTrace"]], ["tc-s2"])
                    h_live2 = runtime_mod.query_member_session_history(
                        self.state_dir, sid, "backend-dev", after_id=h_live1["lastId"], runtime=rt
                    )
                    self.assertEqual([x["tool_call_id"] for x in h_live2["live"]["toolTrace"]], ["tc-s2"])

                # 6. Preambles forbid tool_call JSON batch wrappers and Probe posts
                pre = runtime_mod.build_worker_preamble(
                    models.WorkerConfig(worker_name="backend-dev", role="worker"),
                    swarm_id=sid,
                    task_id=task["id"],
                    context_id=task["contextId"],
                    board_dir=mgr.store.paths(sid).swarm_dir,
                )
                self.assertIn("tool_call", pre)
                self.assertIn("Probe post", pre)

                # 7. _clean_assignment_title strips swarm boilerplate
                self.assertEqual(
                    http_server._clean_assignment_title(
                        "You are `verifier` in this Hermes swarm (`swarm_id`: `737e69ed-0042`, `workdir`: `C:\\Proj…",
                        "verifier",
                    ),
                    "verifier",
                )

        asyncio.run(_run())

    def test_migrate_board_dict_plan_ghost_dirs_sync_wait_clear_workspace_diff_and_auth(self) -> None:
        # 1. migrate_board_v1_to_v2 preserves dict plan["tasks"] and plan["goal"]
        migrated = store.migrate_board_v1_to_v2(
            {
                "plan": {
                    "summary": "Build API",
                    "goal": {"title": "Ship API", "line": "Implement and verify."},
                    "tasks": {"store-dev": "Implement store", "verifier": {"title": "Verify suite", "status": "queued"}},
                    "threads": [{"id": "t-1", "title": "Core", "tasks": ["store-dev", "verifier"]}],
                }
            },
            "mig-swarm",
        )
        self.assertIsInstance(migrated["plan"]["tasks"], dict)
        self.assertEqual(migrated["plan"]["tasks"]["store-dev"], "Implement store")
        self.assertEqual(migrated["plan"]["tasks"]["verifier"]["title"], "Verify suite")
        self.assertEqual(migrated["plan"]["goal"], {"title": "Ship API", "line": "Implement and verify."})

        # 2. Ghost swarm dir prevention on read-only paths, wait_for_update, set_member_wait, release_scope
        svc = board_mod.BoardService(state_dir=self.state_dir)
        ghost_id = "ghost-typo-id-999"
        ghost_dir = self.state_dir / "swarms" / ghost_id
        svc.read_messages(swarm_id=ghost_id, mark_read_by="coordinator")
        svc.set_member_wait("coordinator", waiting=True, swarm_id=ghost_id)
        svc.release_scope(scope="app.py", holder="coordinator", swarm_id=ghost_id)

        async def _check_wait_and_tools() -> None:
            w_ghost = await svc.wait_for_update(swarm_id=ghost_id, since=0, timeout_seconds=0.25)
            self.assertFalse(w_ghost["woken"])
            self.assertTrue(w_ghost["timedOut"])
            self.assertFalse(ghost_dir.exists())

            # Synchronous waitSince clear in wait_for_update finally
            real_sid = "real-wait-swarm"
            svc.upsert_member("coordinator", role="coordinator", swarm_id=real_sid)
            w_real = await svc.wait_for_update(swarm_id=real_sid, since=0, timeout_seconds=0.25)
            self.assertTrue(w_real["timedOut"])
            # Immediately after wait_for_update returns (no sleep!), waitSince MUST already be None on disk
            b_after = svc.read_board(real_sid)
            self.assertIsNone(b_after["members"]["coordinator"]["waitSince"])

            # swarm_workspace(git_status|git_diff) resolves meta["workspace"] via conversation_id
            await mcp_srv.swarm_manage(
                "init",
                title="WS Test",
                primary_conversation_id="conv-ws-resolve",
                workspace=str(self.repo_dir),
            )
            mcp_srv._session_workdir = None
            gs = json.loads(await mcp_srv.swarm_workspace("git_status", conversation_id="conv-ws-resolve"))
            self.assertTrue(gs["ok"])
            self.assertEqual(gs["source"], "swarm_meta")
            gd = json.loads(await mcp_srv.swarm_workspace("git_diff", conversation_id="conv-ws-resolve"))
            self.assertTrue(gd["ok"])

            # swarm_worker(action="diff") with omitted name aggregates across workers
            rt = mcp_srv._swarm_mgr.get_runtime("conv-ws-resolve")
            with rt._state_rlock:
                rt._tasks["t-diff-1"] = {
                    "id": "t-diff-1",
                    "contextId": "c-diff-1",
                    "status": {"state": models.TaskState.COMPLETED.value, "timestamp": models.now_iso()},
                    "metadata": {"workerConfig": {"worker_name": "w-one", "workdir": str(self.repo_dir)}},
                }
            diff_all = json.loads(await mcp_srv.swarm_worker("diff", conversation_id="conv-ws-resolve"))
            self.assertEqual(diff_all["count"], 1)
            self.assertEqual(diff_all["workers"][0]["worker"], "w-one")

        asyncio.run(_check_wait_and_tools())

        # 3. _check_loopback_wake_auth internal_token fallback on HERMES_A2A_HTTP_TOKEN mismatch + /board/{asset}
        mgr_auth = runtime_mod.SwarmManager(state_dir=self.state_dir)
        # First create state/internal_token while HERMES_A2A_HTTP_TOKEN is unset
        disk_tok = mgr_auth.get_internal_token()
        self.assertTrue((self.state_dir / "internal_token").is_file())
        with patch.dict(os.environ, {"HERMES_A2A_HTTP_TOKEN": "mismatched-env-token"}):
            app = http_server.create_a2a_app(mgr_auth, http_url="http://127.0.0.1:19950", bearer_token="mismatched-env-token")
            client = TestClient(app, base_url="http://127.0.0.1:19950")
            # /internal/wake/swarms accepts state/internal_token even when HERMES_A2A_HTTP_TOKEN mismatches
            r_wake_fb = client.get("/internal/wake/swarms", headers={"Authorization": f"Bearer {disk_tok}"})
            self.assertEqual(r_wake_fb.status_code, 200)
            # Wrong token still rejected
            r_wake_bad = client.get("/internal/wake/swarms", headers={"Authorization": "Bearer totally-wrong"})
            self.assertEqual(r_wake_bad.status_code, 401)
            # Static assets and preload.js load without Authorization header when bearer_token is enabled
            self.assertEqual(client.get("/board/styles.css").status_code, 200)
            self.assertEqual(client.get("/board/preload.js").status_code, 200)
            # /board/ui accepts ?token= query param while /board API still requires header
            self.assertEqual(client.get("/board/ui").status_code, 401)
            self.assertEqual(client.get("/board/ui?token=mismatched-env-token").status_code, 200)
            self.assertEqual(client.get("/board?conversationId=x").status_code, 401)

    def test_findings_3_6_7_8_9_11d_regressions(self) -> None:
        import sqlite3

        # --- Finding #3: brain root fallback & remote/synthetic UUID validation ---
        fake_home = self.tmp_root / "fake_home_f3"
        std_brain = fake_home / ".gemini" / "antigravity" / "brain"
        std_brain.mkdir(parents=True, exist_ok=True)
        missing_app_data = self.tmp_root / "missing_app_data"
        missing_app_data.mkdir(parents=True, exist_ok=True)

        with (
            patch.object(Path, "home", return_value=fake_home),
            patch.dict(
                os.environ,
                {"ANTIGRAVITY_APP_DATA_DIR": str(missing_app_data), "ANTIGRAVITY_EXECUTABLE_DATA_DIR": ""},
            ),
        ):
            roots_fb = runtime_mod.resolve_antigravity_brain_roots()
            self.assertIn(std_brain, roots_fb)

            # Seed unrelated local conversation in std_brain; remote primary UUID is NOT in local brain -> must pass!
            unrelated_prim = "11111111-1111-4111-8111-111111111111"
            (std_brain / unrelated_prim).mkdir(parents=True, exist_ok=True)
            remote_prim = "22222222-2222-4222-8222-222222222222"
            remote_coord = "33333333-3333-4333-8333-333333333333"
            ok_remote, err_remote = runtime_mod.validate_coordinator_conversation_id(
                remote_coord, primary_cid=remote_prim
            )
            self.assertTrue(ok_remote, msg=str(err_remote))
            self.assertIsNone(err_remote)

            # Local primary present in brain -> unknown coordinator UUID still rejected
            ok_local, err_local = runtime_mod.validate_coordinator_conversation_id(
                remote_coord, primary_cid=unrelated_prim
            )
            self.assertFalse(ok_local)
            self.assertIn("not found in local Antigravity brain directory", str(err_local))

        # --- Findings #6 & #11d: /board & /board/events internal_token loopback fallback + static asset Host check ---
        mgr_auth = runtime_mod.SwarmManager(state_dir=self.state_dir)
        disk_tok = mgr_auth.get_internal_token()
        sid_auth = "conv-f6-auth"
        mgr_auth.store.upsert_meta(sid_auth, title="F6 Auth", workspace=str(self.repo_dir), primary_conversation_id=sid_auth)
        with patch.dict(os.environ, {"HERMES_A2A_HTTP_TOKEN": "mismatched-env-token"}):
            app = http_server.create_a2a_app(mgr_auth, http_url="http://127.0.0.1:19951", bearer_token="mismatched-env-token")
            client = TestClient(app, base_url="http://127.0.0.1:19951")
            remote_client = TestClient(app, base_url="http://127.0.0.1:19951", client=("10.20.30.40", 4444))

            # Loopback internal_token fallback works on /board, /board/member/history, and /board/events
            self.assertEqual(
                client.get(f"/board?conversationId={sid_auth}", headers={"Authorization": f"Bearer {disk_tok}"}).status_code,
                200,
            )
            self.assertEqual(
                client.get(f"/board?conversationId={sid_auth}", headers={"x-sidecar-token": disk_tok}).status_code,
                200,
            )
            self.assertEqual(
                client.get(
                    f"/board/member/history?conversationId={sid_auth}&member=coordinator",
                    headers={"Authorization": f"Bearer {disk_tok}"},
                ).status_code,
                200,
            )
            with client.stream(
                "GET",
                f"/board/events?conversationId={sid_auth}",
                headers={"Authorization": f"Bearer {disk_tok}"},
            ) as ev_resp:
                self.assertEqual(ev_resp.status_code, 200)

            # Invalid token, non-loopback Host, and non-loopback client IP remain strictly rejected
            self.assertEqual(
                client.get(f"/board?conversationId={sid_auth}", headers={"Authorization": "Bearer bad-tok"}).status_code,
                401,
            )
            self.assertEqual(
                client.get(
                    f"/board?conversationId={sid_auth}",
                    headers={"Host": "evil.example:19951", "Authorization": f"Bearer {disk_tok}"},
                ).status_code,
                401,
            )
            self.assertEqual(
                remote_client.get(
                    f"/board?conversationId={sid_auth}",
                    headers={"Authorization": f"Bearer {disk_tok}"},
                ).status_code,
                401,
            )

            # Finding #11d: /board/preload.js & /board/styles.css allow loopback without auth, reject evil Host with 403
            self.assertEqual(client.get("/board/preload.js").status_code, 200)
            self.assertEqual(client.get("/board/styles.css").status_code, 200)
            self.assertEqual(client.get("/board/preload.js", headers={"Host": "evil.example:19951"}).status_code, 403)
            self.assertEqual(client.get("/board/styles.css", headers={"Host": "evil.example:19951"}).status_code, 403)

        # --- Finding #7: _parse_antigravity_transcript RUNNING -> DONE + ERROR_MESSAGE ---
        app_data_f7 = self.tmp_root / "ag_f7"
        brain_f7 = app_data_f7 / "brain"
        prim_f7 = "44444444-4444-4444-8444-444444444444"
        coord_f7 = "55555555-5555-4555-8555-555555555555"
        (brain_f7 / prim_f7 / ".system_generated" / "subagents").mkdir(parents=True, exist_ok=True)
        (brain_f7 / prim_f7 / ".system_generated" / "subagents" / f"{coord_f7}.json").write_text(
            json.dumps({"conversationId": coord_f7}), encoding="utf-8"
        )
        tlog_dir = brain_f7 / coord_f7 / ".system_generated" / "logs"
        tlog_dir.mkdir(parents=True, exist_ok=True)
        tlog_file = tlog_dir / "transcript_full.jsonl"

        step0 = {
            "step_index": 0,
            "source": "SYSTEM",
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-28T01:00:00Z",
            "content": "Boot",
        }
        step1_running = {
            "step_index": 1,
            "source": "MODEL",
            "type": "PLANNER_RESPONSE",
            "status": "RUNNING",
            "created_at": "2026-11-28T01:00:05Z",
            "model": "custom-test-model",
            "thinking": "Drafting plan",
            "content": "Partial answer",
            "tool_calls": [{"name": "swarm_status", "args": {"swarm_id": prim_f7}}],
        }
        tlog_file.write_text("\n".join(json.dumps(s) for s in (step0, step1_running)) + "\n", encoding="utf-8")

        with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data_f7)}):
            mgr_f7 = runtime_mod.SwarmManager(state_dir=self.state_dir)
            mgr_f7.store.upsert_meta(
                prim_f7,
                title="F7 Test",
                workspace=str(self.repo_dir),
                primary_conversation_id=prim_f7,
                coordinator_conversation_id=coord_f7,
            )
            h_run = runtime_mod.query_member_session_history(self.state_dir, prim_f7, "coordinator")
            self.assertEqual(len(h_run["messages"]), 1)
            self.assertEqual(h_run["lastId"], 1)
            self.assertTrue(h_run["live"]["active"])
            self.assertEqual(h_run["live"]["transcript"], "Partial answer")
            self.assertEqual(h_run["live"]["reasoning"], "Drafting plan")
            self.assertEqual(h_run["live"]["toolCalls"][0]["name"], "swarm_status")
            self.assertEqual(h_run["live"]["currentTool"]["name"], "swarm_status")

            step1_done = {
                "step_index": 1,
                "source": "MODEL",
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "created_at": "2026-11-28T01:00:08Z",
                "model": "custom-test-model",
                "input_tokens": 500,
                "cache_read_tokens": 0,
                "output_tokens": 80,
                "thinking": "Drafting plan",
                "content": "Final answer",
                "tool_calls": [],
            }
            step2_err = {
                "step_index": 2,
                "source": "SYSTEM",
                "type": "ERROR_MESSAGE",
                "status": "DONE",
                "created_at": "2026-11-28T01:00:09Z",
                "error": "The stream was interrupted.",
                "content": "Error: The stream was interrupted.",
            }
            tlog_file.write_text(
                "\n".join(json.dumps(s) for s in (step0, step1_done, step2_err)) + "\n", encoding="utf-8"
            )
            h_done = runtime_mod.query_member_session_history(
                self.state_dir, prim_f7, "coordinator", after_id=h_run["lastId"]
            )
            self.assertEqual(len(h_done["messages"]), 2)
            self.assertEqual(h_done["messages"][0]["content"], "Final answer")
            self.assertEqual(h_done["messages"][1]["status"], "error")
            self.assertTrue(h_done["messages"][1]["isError"])
            self.assertIn("stream was interrupted", h_done["messages"][1]["content"])
            self.assertEqual(h_done["live"]["transcript"], "")
            self.assertEqual(h_done["live"]["toolCalls"], [])

        # --- Finding #8: _COMMITTED_TC_CACHE invalidation on in-place DB delete/update ---
        hh_f8 = self.tmp_root / "hh_f8"
        hh_f8.mkdir(parents=True, exist_ok=True)
        db_f8 = hh_f8 / "state.db"
        con_f8 = sqlite3.connect(str(db_f8))
        con_f8.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, title TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT)"
        )
        con_f8.execute(
            "CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, timestamp REAL, "
            "content TEXT, reasoning TEXT, tool_calls TEXT, tool_name TEXT, tool_call_id TEXT)"
        )
        con_f8.execute(
            "INSERT INTO sessions VALUES ('hs-f8', NULL, 'cli', 'F8', 100, 20, 0, 0, 0, 0.0, 0.0, 'custom-test-model', '{}', 2, 1, 100.0, NULL, 'custom', NULL)"
        )
        con_f8.execute(
            "INSERT INTO messages (id, session_id, role, timestamp, content, tool_calls) VALUES (1, 'hs-f8', 'assistant', 100.0, 'step1', ?)",
            (json.dumps([{"id": "tc-f8-1", "name": "bash", "arguments": {"cmd": "ls"}}]),),
        )
        con_f8.execute(
            "INSERT INTO messages (id, session_id, role, timestamp, content, tool_name, tool_call_id) VALUES (2, 'hs-f8', 'tool', 101.0, 'ok', 'bash', 'tc-f8-1')"
        )
        con_f8.commit()
        con_f8.close()

        sid_f8 = "conv-f8-cache"
        mgr_f8 = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr_f8.store.upsert_meta(sid_f8, title="F8", workspace=str(self.repo_dir), primary_conversation_id=sid_f8)
        rt_f8 = mgr_f8.get_runtime(sid_f8)
        with rt_f8._state_rlock:
            rt_f8._context_sessions["ctx-f8"] = "hs-f8"
            rt_f8._tasks["task-f8"] = {
                "id": "task-f8",
                "contextId": "ctx-f8",
                "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                "metadata": {
                    "workerConfig": {"worker_name": "w-f8", "role": "worker", "model": "custom-test-model"},
                    "liveActivity": {"hermesSessionId": "hs-f8"},
                },
            }
            rt_f8._live_buffers["task-f8"] = {
                "liveToolTrace": [
                    {"event": "tool_use", "name": "bash", "tool_call_id": "tc-f8-1"},
                    {"event": "tool_use", "name": "grep", "tool_call_id": "tc-f8-2"},
                ],
                "liveTranscript": "",
                "liveReasoning": "",
                "recentTools": ["bash", "grep"],
                "startedAt": models.now_iso(),
            }

        with patch.dict(os.environ, {"HERMES_HOME": str(hh_f8)}), patch.object(runtime_mod, "_COMMITTED_TC_CACHE", {}):
            h8_1 = runtime_mod.query_member_session_history(self.state_dir, sid_f8, "w-f8", runtime=rt_f8)
            self.assertEqual([x["tool_call_id"] for x in h8_1["live"]["toolTrace"]], ["tc-f8-2"])

            # Append tc-f8-2 to SQLite -> incremental cache suppresses it
            con_f8 = sqlite3.connect(str(db_f8))
            con_f8.execute(
                "INSERT INTO messages (id, session_id, role, timestamp, content, tool_calls) VALUES (3, 'hs-f8', 'assistant', 102.0, 'step2', ?)",
                (json.dumps([{"id": "tc-f8-2", "name": "grep", "arguments": {"query": "x"}}]),),
            )
            con_f8.commit()
            con_f8.close()
            h8_2 = runtime_mod.query_member_session_history(
                self.state_dir, sid_f8, "w-f8", after_id=h8_1["lastId"], runtime=rt_f8
            )
            self.assertEqual(h8_2["live"]["toolTrace"], [])

            # Delete id=3 in-place (simulating NTFS in-place delete without ctime change) -> cache MUST invalidate
            con_f8 = sqlite3.connect(str(db_f8))
            con_f8.execute("DELETE FROM messages WHERE id = 3")
            con_f8.commit()
            con_f8.close()
            h8_3 = runtime_mod.query_member_session_history(
                self.state_dir, sid_f8, "w-f8", after_id=2, runtime=rt_f8
            )
            self.assertEqual([x["tool_call_id"] for x in h8_3["live"]["toolTrace"]], ["tc-f8-2"])

        # --- Finding #9: final_provider precedence (billingProvider > gw_prov > cfg.provider) ---
        hh_f9 = self.tmp_root / "hh_f9"
        hh_f9.mkdir(parents=True, exist_ok=True)
        db_f9 = hh_f9 / "state.db"
        con_f9 = sqlite3.connect(str(db_f9))
        con_f9.execute(
            "CREATE TABLE sessions (id TEXT PRIMARY KEY, parent_session_id TEXT, source TEXT, title TEXT, "
            "input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER, cache_write_tokens INTEGER, "
            "reasoning_tokens INTEGER, estimated_cost_usd REAL, actual_cost_usd REAL, model TEXT, model_config TEXT, "
            "message_count INTEGER, tool_call_count INTEGER, started_at REAL, ended_at REAL, "
            "billing_provider TEXT, billing_base_url TEXT)"
        )
        con_f9.execute(
            "INSERT INTO sessions VALUES (?, NULL, 'cli', 'F9a', 100, 50, 0, 0, 0, 0.0, 0.0, 'custom-test-model', ?, 1, 0, 100.0, 101.0, 'custom', NULL)",
            ("hs-f9-bill", json.dumps({"gateway_runtime": {"provider": "gw-prov"}})),
        )
        con_f9.execute(
            "INSERT INTO sessions VALUES (?, NULL, 'cli', 'F9b', 100, 50, 0, 0, 0, 0.0, 0.0, 'custom-test-model', ?, 1, 0, 100.0, 101.0, NULL, NULL)",
            ("hs-f9-gw", json.dumps({"gateway_runtime": {"provider": "gw-prov"}})),
        )
        con_f9.commit()
        con_f9.close()

        f9_script = self.tmp_root / "fake_f9_worker.py"
        f9_script.write_text(
            "\n".join([
                "import json, sys",
                "sid = sys.argv[1]",
                "print(json.dumps({'type': 'system', 'subtype': 'init', 'model': 'custom-test-model', 'session_id': sid}), flush=True)",
                "print(json.dumps({'type': 'result', 'session_id': sid, 'exit_code': 0, 'duration_ms': 100, 'text': 'done', 'tokens': {'input': 100, 'output': 50}}), flush=True)",
            ]),
            encoding="utf-8",
        )

        async def _run_f9() -> None:
            mgr9 = mcp_srv._swarm_mgr
            sid9 = "conv-f9-prov"
            mgr9.store.upsert_meta(sid9, title="F9", workspace=str(self.repo_dir), primary_conversation_id=sid9)
            rt9 = mgr9.get_runtime(sid9)
            with patch.dict(os.environ, {"HERMES_HOME": str(hh_f9)}):
                with patch.object(rt9, "_build_hermes_command", return_value=([sys.executable, str(f9_script), "hs-f9-bill"], str(self.repo_dir))):
                    r1 = await rt9.spawn_workers(
                        name="w-f9-bill",
                        prompt="test",
                        workdir=str(self.repo_dir),
                        provider="requested-prov",
                        return_immediately=False,
                    )
                self.assertEqual(r1["tasks"][0]["metadata"]["resolvedProvider"], "custom")
                with patch.object(rt9, "_build_hermes_command", return_value=([sys.executable, str(f9_script), "hs-f9-gw"], str(self.repo_dir))):
                    r2 = await rt9.spawn_workers(
                        name="w-f9-gw",
                        prompt="test",
                        workdir=str(self.repo_dir),
                        provider="requested-prov",
                        return_immediately=False,
                    )
                self.assertEqual(r2["tasks"][0]["metadata"]["resolvedProvider"], "gw-prov")

        asyncio.run(_run_f9())


class RolePolicyTests(unittest.TestCase):
    """Verify local/workspace role_policy.json enforcement, implementer alias, and defaults."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-rp-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        _init_temp_git_repo(self.repo_dir)
        self.orig_mgr = mcp_srv._swarm_mgr
        self.orig_http = mcp_srv._http_ctrl
        self.orig_wd = mcp_srv._session_workdir
        self.orig_conv = mcp_srv._session_conversation_id
        self.orig_sid = mcp_srv._session_swarm_id
        mcp_srv._swarm_mgr = runtime_mod.SwarmManager(
            state_dir=self.state_dir,
            default_workdir_provider=lambda: str(self.repo_dir),
        )
        mcp_srv._http_ctrl = http_server.EmbeddedA2AHttpController(mcp_srv._swarm_mgr)
        mcp_srv._session_workdir = str(self.repo_dir)
        mcp_srv._session_conversation_id = None
        mcp_srv._session_swarm_id = None

    def tearDown(self) -> None:
        mcp_srv._swarm_mgr = self.orig_mgr
        mcp_srv._http_ctrl = self.orig_http
        mcp_srv._session_workdir = self.orig_wd
        mcp_srv._session_conversation_id = self.orig_conv
        mcp_srv._session_swarm_id = self.orig_sid
        self._tmp.cleanup()

    def test_default_policy_implementer_alias_and_warn_mode(self) -> None:
        import role_policy as rp_mod

        self.assertEqual(models.normalize_role("implementer"), "worker")
        self.assertEqual(models.validate_worker_role("implementer"), "worker")

        with patch.dict(os.environ, {"HERMES_SWARM_ROLE_POLICY": "default"}):
            pol = rp_mod.load_role_policy(str(self.repo_dir))
            self.assertEqual(pol["source"], "default")
            self.assertEqual(pol["enforcement"], "warn")

            ok, warn = rp_mod.evaluate_worker_spawn_policy("verifier", worker_name="v-1", policy=pol)
            self.assertTrue(ok)
            self.assertIn("ide_only_roles", warn or "")

            async def _run() -> None:
                init_res = json.loads(await mcp_srv.swarm_manage(
                    "init",
                    primary_conversation_id="conv-rp-warn",
                    workspace=str(self.repo_dir),
                ))
                self.assertEqual(init_res["role_policy"]["enforcement"], "warn")

                async def _fake_spawn(**kwargs):
                    return {"tasks": [{"id": "t1", "worker": kwargs["name"], "role": models.validate_worker_role(kwargs["role"]), "model": kwargs.get("model")}]}

                with patch.object(mcp_srv._swarm_mgr.get_runtime(init_res["swarm_id"]), "spawn_workers", side_effect=_fake_spawn):
                    impl_res = json.loads(await mcp_srv.swarm_worker(
                        "spawn",
                        name="coder-1",
                        prompt="Implement change",
                        role="implementer",
                        swarm_id=init_res["swarm_id"],
                    ))
                    self.assertNotIn("policy_warning", impl_res)
                    self.assertEqual(impl_res["tasks"][0]["role"], "worker")

                    ver_res = json.loads(await mcp_srv.swarm_worker(
                        "spawn",
                        name="verifier",
                        prompt="Check diff",
                        role="verifier",
                        swarm_id=init_res["swarm_id"],
                    ))
                    self.assertIn("policy_warning", ver_res)

            asyncio.run(_run())

    def test_reject_policy_defaults_and_explicit_override(self) -> None:
        policy_file = self.tmp_root / "custom_role_policy.json"
        policy_file.write_text(json.dumps({
            "version": 1,
            "enforcement": "reject",
            "ide_only_roles": ["researcher", "architect", "verifier"],
            "ide_subagent_preferences": {
                "tool": "invoke_subagent",
                "type_name": "self",
                "preferred_tiers": {"research": "pro"},
            },
            "hermes_worker_defaults": {
                "model": "custom-exec-model",
                "provider": "custom-exec-provider",
            },
        }), encoding="utf-8")

        captured: list[dict] = []

        async def _fake_spawn(**kwargs):
            captured.append(dict(kwargs))
            return {"tasks": [{"id": "t2", "worker": kwargs["name"], "model": kwargs.get("model"), "provider": kwargs.get("provider")}]}

        with patch.dict(os.environ, {"HERMES_SWARM_ROLE_POLICY": str(policy_file)}):
            async def _run() -> None:
                cat_res = json.loads(await mcp_srv.swarm_catalog(workdir=str(self.repo_dir)))
                self.assertEqual(cat_res["role_policy"]["enforcement"], "reject")
                self.assertEqual(cat_res["role_policy"]["hermes_worker_defaults"]["model"], "custom-exec-model")

                init_res = json.loads(await mcp_srv.swarm_manage(
                    "init",
                    primary_conversation_id="conv-rp-reject",
                    workspace=str(self.repo_dir),
                ))
                sid = init_res["swarm_id"]
                rt = mcp_srv._swarm_mgr.get_runtime(sid)

                with patch.object(rt, "spawn_workers", side_effect=_fake_spawn):
                    with self.assertRaises(ValueError) as ctx_role:
                        await mcp_srv.swarm_worker(
                            "spawn",
                            name="res-1",
                            prompt="Research kernels",
                            role="researcher",
                            swarm_id=sid,
                        )
                    self.assertIn("invoke_subagent", str(ctx_role.exception))

                    with self.assertRaises(ValueError) as ctx_name:
                        await mcp_srv.swarm_worker(
                            "spawn",
                            name="verifier-1",
                            prompt="Audit code",
                            role="worker",
                            swarm_id=sid,
                        )
                    self.assertIn("name='verifier'", str(ctx_name.exception))

                    # Allowed execution role inherits hermes_worker_defaults when omitted
                    ok_res = json.loads(await mcp_srv.swarm_worker(
                        "spawn",
                        name="impl-1",
                        prompt="Implement kernel patch",
                        role="implementer",
                        swarm_id=sid,
                    ))
                    self.assertEqual(ok_res["tasks"][0]["model"], "custom-exec-model")
                    self.assertEqual(ok_res["tasks"][0]["provider"], "custom-exec-provider")

                    # Explicit override works even under enforcement='reject'
                    ovr_res = json.loads(await mcp_srv.swarm_worker(
                        "spawn",
                        name="ver-override",
                        prompt="Explicitly run verification script",
                        role="verifier",
                        model="explicit-model",
                        allow_ide_only_role=True,
                        swarm_id=sid,
                    ))
                    self.assertEqual(ovr_res["tasks"][0]["model"], "explicit-model")

            asyncio.run(_run())


class BackendPerfAndFreezeRegressionTests(unittest.TestCase):
    """Regression coverage for transcript cache, ETag stability/compact/member, SQLite memoization, lock split, idle coordinator, and off-loop wakeup."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-perf-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_HOME",
                "ANTIGRAVITY_APP_DATA_DIR",
                "HERMES_SWARM_ROLE_POLICY",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("HERMES_A2A_SWARM_ID", None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_transcript_mtime_size_cache_hit_invalidation_and_incremental_slicing(self) -> None:
        app_data = self.tmp_root / "ag_cache"
        brain_dir = app_data / "brain"
        prim_uuid = "aaaa1111-1111-4111-8111-111111111111"
        coord_uuid = "bbbb2222-2222-4222-8222-222222222222"
        logs_dir = brain_dir / coord_uuid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        tfile = logs_dir / "transcript_full.jsonl"
        step0 = {
            "step_index": 0,
            "type": "SYSTEM_MESSAGE",
            "status": "DONE",
            "created_at": "2026-11-28T10:00:00Z",
            "content": "Boot",
        }
        step1 = {
            "step_index": 1,
            "type": "PLANNER_RESPONSE",
            "status": "DONE",
            "created_at": "2026-11-28T10:00:04Z",
            "model": "gemini-4-cache-test",
            "input_tokens": 500,
            "output_tokens": 120,
            "content": "First step done",
            "tool_calls": [],
        }
        tfile.write_text("\n".join(json.dumps(s) for s in (step0, step1)) + "\n", encoding="utf-8")

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr.store.upsert_meta(
            prim_uuid,
            title="Cache Test",
            workspace=str(self.repo_dir),
            primary_conversation_id=prim_uuid,
            coordinator_conversation_id=coord_uuid,
        )
        mgr.board_service.upsert_member(
            "coordinator",
            role="coordinator",
            runtime="antigravity",
            state="working",
            conversation_id=coord_uuid,
            swarm_id=prim_uuid,
        )

        read_count = 0
        orig_read_text = Path.read_text

        resolved_tfile = tfile.resolve()

        def _counting_read_text(path_obj: Path, *args, **kwargs):
            nonlocal read_count
            try:
                if path_obj.resolve() == resolved_tfile:
                    read_count += 1
            except OSError:
                pass
            return orig_read_text(path_obj, *args, **kwargs)

        try:
            with (
                patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}),
                patch.object(Path, "read_text", autospec=True, side_effect=_counting_read_text),
            ):
                p1 = runtime_mod._parse_antigravity_transcript(tfile)
                self.assertEqual(read_count, 1)
                self.assertEqual(len(p1["messages"]), 2)
                # Mutating returned dict/list must NOT corrupt cache
                p1["messages"].append({"id": 999, "role": "user", "content": "mutated"})

                p2 = runtime_mod._parse_antigravity_transcript(tfile)
                self.assertEqual(read_count, 1)
                self.assertEqual(len(p2["messages"]), 2)

                # Summary call hits cache without re-reading disk
                s1 = runtime_mod.extract_antigravity_transcript_summary([coord_uuid], primary_cid=prim_uuid)
                s2 = runtime_mod.extract_antigravity_transcript_summary([coord_uuid], primary_cid=prim_uuid)
                self.assertEqual(read_count, 1)
                self.assertEqual(s1["model"], "gemini-4-cache-test")
                self.assertEqual(s2["steps"], 1)

                # Incremental after_id polls slice cached entries safely with 0 extra disk reads
                h_full = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator")
                self.assertEqual(read_count, 1)
                self.assertEqual(len(h_full["messages"]), 2)
                h_caught_up = runtime_mod.query_member_session_history(
                    self.state_dir, prim_uuid, "coordinator", after_id=h_full["lastId"]
                )
                self.assertEqual(read_count, 1)
                self.assertEqual(h_caught_up["messages"], [])
                h_after_first = runtime_mod.query_member_session_history(
                    self.state_dir, prim_uuid, "coordinator", after_id=h_full["messages"][0]["id"]
                )
                self.assertEqual(read_count, 1)
                self.assertEqual([m["content"] for m in h_after_first["messages"]], ["First step done"])

                # Updating file (size + mtime change) invalidates cache and reads exactly once more
                step2 = {
                    "step_index": 2,
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-28T10:00:09Z",
                    "model": "gemini-4-cache-test",
                    "input_tokens": 600,
                    "output_tokens": 80,
                    "content": "Second step done",
                    "tool_calls": [],
                }
                tfile.write_text("\n".join(json.dumps(s) for s in (step0, step1, step2)) + "\n", encoding="utf-8")
                h_inc2 = runtime_mod.query_member_session_history(
                    self.state_dir, prim_uuid, "coordinator", after_id=h_full["lastId"]
                )
                self.assertEqual(read_count, 2)
                self.assertEqual([m["content"] for m in h_inc2["messages"]], ["Second step done"])
        finally:
            asyncio.run(mgr.shutdown())

    def test_etag_stability_compact_mode_member_preserved_and_fast_unchanged(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-perf-etag"
        coord_conv = "conv-perf-etag-coord"
        mgr.store.upsert_meta(
            sid,
            title="ETag Stability Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", conversation_id=coord_conv, swarm_id=sid)
        mgr.board_service.upsert_member("w-active", role="worker", state="working", model="m-test", provider="custom", swarm_id=sid)
        mgr.board_service.post_message(sender="coordinator", summary="@w-active start", swarm_id=sid)

        rt = mgr.get_runtime(sid)
        with rt._state_rlock:
            rt._tasks["task-tps-1"] = {
                "id": "task-tps-1",
                "contextId": "ctx-tps-1",
                "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                "artifacts": [],
                "history": [],
                "metadata": {
                    "createdAt": "2026-11-28T10:00:00Z",
                    "workerConfig": {"worker_name": "w-active", "role": "worker", "model": "m-test", "provider": "custom"},
                    "tokens": {"input": 1000, "output": 500, "total": 1500, "contextUsed": 1500, "contextLimit": 100000},
                    "liveActivity": {
                        "startedAt": "2026-11-28T10:00:00Z",
                        "lastEventAt": models.now_iso(),
                        "steps": 2,
                        "toolCalls": 3,
                        "lastTool": "bash",
                        "currentTool": {"name": "bash", "preview": "pytest", "startedAt": models.now_iso()},
                        "recentTools": [],
                        "elapsedMs": 10_000,
                        "llmActiveMs": 0,
                        "tps": 50.0,
                    },
                },
            }

        try:
            # 1) Wall-clock elapsedMs/TPS drift while active must NOT invalidate ETag
            with patch.object(runtime_mod, "_elapsed_ms", side_effect=[10_000, 15_000, 25_000, 35_000]):
                snap_a = http_server.build_observer_snapshot(mgr, sid)
                snap_b = http_server.build_observer_snapshot(mgr, sid)
            self.assertNotEqual(snap_a["telemetry"]["liveTps"], snap_b["telemetry"]["liveTps"])
            self.assertEqual(snap_a["etag"], snap_b["etag"])

            # 2) HTTP ?compact=1 & ?include_raw=0 skip raw_blob while preserving core fields
            app = http_server.create_a2a_app(mgr)
            client = TestClient(app, base_url="http://127.0.0.1")

            r_compact = client.get(f"/board?conversationId={coord_conv}&compact=1")
            self.assertEqual(r_compact.status_code, 200)
            j_compact = r_compact.json()
            self.assertEqual(j_compact["raw"], {})
            self.assertEqual(j_compact["member"], "coordinator")
            self.assertTrue(j_compact["members"])
            etag = j_compact["etag"]

            r_no_raw = client.get(f"/board?conversationId={coord_conv}&include_raw=0")
            self.assertEqual(r_no_raw.status_code, 200)
            self.assertEqual(r_no_raw.json()["raw"], {})

            # 3) Matching ?etag= with matching representation (?compact=1) preserves "member" AND short-circuits build_observer_snapshot
            with patch.object(http_server, "build_observer_snapshot", side_effect=AssertionError("Expected fast-path short-circuit")):
                r_unchanged = client.get(f"/board?conversationId={coord_conv}&compact=1&etag={etag}")
                self.assertEqual(r_unchanged.status_code, 200)
                j_unchanged = r_unchanged.json()
                self.assertTrue(j_unchanged["unchanged"])
                self.assertEqual(j_unchanged["member"], "coordinator")
                self.assertEqual(j_unchanged["etag"], etag)

                r_304 = client.get(f"/board?conversationId={coord_conv}&compact=1", headers={"If-None-Match": etag})
                self.assertEqual(r_304.status_code, 304)
                self.assertEqual(r_304.headers.get("etag"), etag)
                self.assertEqual(r_304.headers.get("x-swarm-member"), "coordinator")

            # 4) Real board mutation invalidates fast-path cache immediately
            mgr.board_service.post_message(sender="w-active", summary="@coordinator progress", swarm_id=sid)
            r_changed = client.get(f"/board?conversationId={coord_conv}&etag={etag}&compact=1")
            self.assertEqual(r_changed.status_code, 200)
            self.assertFalse(r_changed.json().get("unchanged", False))
            self.assertNotEqual(r_changed.json()["etag"], etag)
        finally:
            asyncio.run(mgr.shutdown())

    def test_task_to_summary_row_sqlite_lookup_memoization(self) -> None:
        fake_hh = self.tmp_root / "hh_memo"
        fake_hh.mkdir(parents=True, exist_ok=True)
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-sqlite-memo"
        mgr.store.upsert_meta(sid, title="SQLite Memo", workspace=str(self.repo_dir), primary_conversation_id=sid)
        mgr.board_service.upsert_member("w-old", role="worker", state="waiting", session_id="hs-memo-1", swarm_id=sid)
        rt = mgr.get_runtime(sid)
        with rt._state_rlock:
            rt._tasks["task-memo-1"] = {
                "id": "task-memo-1",
                "contextId": "ctx-memo-1",
                "status": {"state": models.TaskState.COMPLETED.value, "timestamp": "2026-11-28T09:00:00Z"},
                "artifacts": [],
                "history": [],
                "metadata": {
                    "createdAt": "2026-11-28T08:59:00Z",
                    "workerConfig": {"worker_name": "w-old", "role": "worker"},
                    "liveActivity": {"hermesSessionId": "hs-memo-1", "endedAt": "2026-11-28T09:00:00Z"},
                },
            }

        calls = 0

        def _fake_query(session_id: str, **kwargs):
            nonlocal calls
            calls += 1
            # Return model with NULL billingProvider (the exact live case that previously looped forever)
            return {}, None, {"model": "memo-model", "billingProvider": None}

        try:
            with (
                patch.dict(os.environ, {"HERMES_HOME": str(fake_hh)}),
                patch.object(runtime_mod, "_query_hermes_session_stats", side_effect=_fake_query),
            ):
                s1 = http_server.build_observer_snapshot(mgr, sid)
                s2 = http_server.build_observer_snapshot(mgr, sid)
                s3 = http_server.build_observer_snapshot(mgr, sid)
            self.assertEqual(calls, 1)
            for snap in (s1, s2, s3):
                row = next(t for t in snap["tasks"] if t["taskId"] == "task-memo-1")
                self.assertEqual(row["model"], "memo-model")
        finally:
            asyncio.run(mgr.shutdown())

    def test_stale_and_done_coordinator_transitions_to_waiting_while_running_stays_working(self) -> None:
        app_data = self.tmp_root / "ag_coord_idle"
        brain_dir = app_data / "brain"
        prim_done = "cccc3333-3333-4333-8333-333333333333"
        coord_done = "dddd4444-4444-4444-8444-444444444444"
        done_logs = brain_dir / coord_done / ".system_generated" / "logs"
        done_logs.mkdir(parents=True, exist_ok=True)
        tfile = done_logs / "transcript_full.jsonl"

        # Case 1: Active RUNNING step -> MUST stay "working"
        running_steps = [
            {"step_index": 0, "type": "SYSTEM_MESSAGE", "status": "DONE", "created_at": "2026-11-28T11:00:00Z", "content": "Start"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "status": "RUNNING", "created_at": "2026-11-28T11:00:02Z", "model": "gemini-live", "content": "Working..."},
        ]
        tfile.write_text("\n".join(json.dumps(s) for s in running_steps) + "\n", encoding="utf-8")

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr.store.upsert_meta(
            prim_done,
            title="Coord Idle Test",
            workspace=str(self.repo_dir),
            primary_conversation_id=prim_done,
            coordinator_conversation_id=coord_done,
        )
        mgr.board_service.upsert_member(
            "coordinator",
            role="coordinator",
            runtime="antigravity",
            state="working",
            conversation_id=coord_done,
            swarm_id=prim_done,
        )

        # Case 2: Coordinator with NO transcript bound >60s ago and 0 active workers -> "waiting"
        prim_stale = "eeee5555-5555-4555-8555-555555555555"
        mgr.store.upsert_meta(
            prim_stale,
            title="Stale Bound Test",
            workspace=str(self.repo_dir),
            primary_conversation_id=prim_stale,
            coordinator_conversation_id="ffff6666-6666-4666-8666-666666666666",
        )
        mgr.board_service.upsert_member(
            "coordinator",
            role="coordinator",
            runtime="antigravity",
            state="working",
            conversation_id="ffff6666-6666-4666-8666-666666666666",
            swarm_id=prim_stale,
        )
        b_stale = mgr.board_service.read_board(prim_stale)
        b_stale["members"]["coordinator"]["updatedAt"] = "2020-01-01T00:00:00Z"
        b_stale["members"]["coordinator"]["lastHeartbeatAt"] = "2020-01-01T00:00:00Z"
        mgr.store.write_board(prim_stale, b_stale)
        m_stale = mgr.store.read_meta(prim_stale)
        assert m_stale is not None
        m_stale["updatedAt"] = "2020-01-01T00:00:00Z"
        store.atomic_write_json(mgr.store.paths(prim_stale).meta_file, m_stale)

        try:
            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}):
                snap_run = http_server.build_observer_snapshot(mgr, prim_done)
                coord_run = next(m for m in snap_run["members"] if m["name"] == "coordinator")
                self.assertEqual(coord_run["status"], "working")
                hist_run = runtime_mod.query_member_session_history(self.state_dir, prim_done, "coordinator")
                self.assertEqual(hist_run["state"], "working")
                self.assertTrue(hist_run["live"]["active"])

                # Transition transcript to terminal DONE -> coordinator reflects as "waiting" immediately
                done_steps = [
                    running_steps[0],
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-28T11:00:05Z",
                        "model": "gemini-live",
                        "output_tokens": 50,
                        "content": "All done.",
                        "tool_calls": [],
                    },
                ]
                tfile.write_text("\n".join(json.dumps(s) for s in done_steps) + "\n", encoding="utf-8")
                snap_done = http_server.build_observer_snapshot(mgr, prim_done)
                coord_done_m = next(m for m in snap_done["members"] if m["name"] == "coordinator")
                self.assertEqual(coord_done_m["status"], "waiting")
                hist_done = runtime_mod.query_member_session_history(self.state_dir, prim_done, "coordinator")
                self.assertEqual(hist_done["state"], "waiting")
                self.assertFalse(hist_done["live"]["active"])

                # Stale coordinator with no transcript > 60s and 0 active workers -> "waiting"
                snap_stale = http_server.build_observer_snapshot(mgr, prim_stale)
                coord_stale_m = next(m for m in snap_stale["members"] if m["name"] == "coordinator")
                self.assertEqual(coord_stale_m["status"], "waiting")
        finally:
            asyncio.run(mgr.shutdown())

    def test_lock_decoupling_wakeup_offload_and_role_policy_cache(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-lock-wakeup"
        mgr.store.upsert_meta(sid, title="Lock & Wakeup", workspace=str(self.repo_dir), primary_conversation_id=sid)
        rt = mgr.get_runtime(sid)

        try:
            # 1) Runtime state lock, board thread lock, and log lock are strictly decoupled
            board_lock = store.get_swarm_thread_lock(sid)
            log_lock = store.get_swarm_log_lock(sid)
            self.assertIsNot(rt._state_rlock, board_lock)
            self.assertIsNot(log_lock, board_lock)
            self.assertIsNot(rt._state_rlock, log_lock)

            # Holding board_lock in one thread must NOT block rt.get_task_snapshots() in another thread
            lock_held = threading.Event()
            release_lock = threading.Event()

            def _hold_board_lock() -> None:
                with board_lock:
                    lock_held.set()
                    release_lock.wait(timeout=2.0)

            holder = threading.Thread(target=_hold_board_lock, daemon=True)
            holder.start()
            self.assertTrue(lock_held.wait(timeout=1.0))
            try:
                t0 = time.monotonic()
                snaps = rt.get_task_snapshots()
                elapsed = time.monotonic() - t0
                self.assertIsInstance(snaps, list)
                self.assertLess(elapsed, 0.5)
            finally:
                release_lock.set()
                holder.join(timeout=2.0)

            # 2) wakeup.poll_disk_once runs disk I/O off the asyncio event loop
            mgr.board_service.post_message(sender="coordinator", summary="seed post", swarm_id=sid)
            orig_read_board = mgr.board_service.read_board
            caller_threads: list[int] = []

            def _slow_read_board(target_sid: str = "default", **kwargs):
                caller_threads.append(threading.get_ident())
                time.sleep(0.12)
                return orig_read_board(target_sid, **kwargs)

            async def _check_non_blocking() -> None:
                loop_tid = threading.get_ident()
                ticks = 0

                async def _heartbeat() -> None:
                    nonlocal ticks
                    for _ in range(5):
                        await asyncio.sleep(0.02)
                        ticks += 1

                with patch.object(mgr.board_service, "read_board", side_effect=_slow_read_board):
                    hb = asyncio.create_task(_heartbeat())
                    await mgr.mention_router.poll_disk_once([sid])
                    await hb

                self.assertTrue(caller_threads, "Expected poll_disk_once to read changed board")
                self.assertTrue(all(tid != loop_tid for tid in caller_threads))
                self.assertGreaterEqual(ticks, 3)

            asyncio.run(_check_non_blocking())

            # 3) server._load_role_policy_cached caches by mtime+size and honors HERMES_SWARM_ROLE_POLICY overrides
            pfile = self.tmp_root / "cached_role_policy.json"
            pfile.write_text(json.dumps({"version": 1, "enforcement": "reject"}), encoding="utf-8")
            import role_policy as rp_mod

            load_calls = 0
            orig_load = rp_mod.load_role_policy

            def _counting_load(ws=None):
                nonlocal load_calls
                load_calls += 1
                return orig_load(ws)

            with (
                patch.object(rp_mod, "load_role_policy", side_effect=_counting_load),
                patch.dict(os.environ, {"HERMES_SWARM_ROLE_POLICY": str(pfile)}),
            ):
                pol1 = mcp_srv._load_role_policy_cached(str(self.repo_dir))
                pol2 = mcp_srv._load_role_policy_cached(str(self.repo_dir))
                self.assertEqual(pol1["enforcement"], "reject")
                self.assertEqual(pol2["enforcement"], "reject")
                self.assertEqual(load_calls, 1)

                # Modifying policy file invalidates cache
                pfile.write_text(json.dumps({"version": 1, "enforcement": "warn", "note": "updated"}), encoding="utf-8")
                pol3 = mcp_srv._load_role_policy_cached(str(self.repo_dir))
                self.assertEqual(pol3["enforcement"], "warn")
                self.assertEqual(load_calls, 2)

            # Env override "default" bypasses cached file immediately
            with patch.dict(os.environ, {"HERMES_SWARM_ROLE_POLICY": "default"}):
                pol_def = mcp_srv._load_role_policy_cached(str(self.repo_dir))
                self.assertEqual(pol_def["source"], "default")
                self.assertEqual(pol_def["enforcement"], "warn")
        finally:
            asyncio.run(mgr.shutdown())


class AuditPhase2BackendBugfixTests(unittest.TestCase):
    """Regression coverage for #292 backend findings: C1, C2, H1, H2, H3, H4, H5, M3, M4, M6, M7."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="hermes-bh-fix-")
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.repo_dir = self.tmp_root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)
        self._env_backup = {
            k: os.environ.get(k)
            for k in (
                "HERMES_A2A_STATE_DIR",
                "HERMES_A2A_SWARM_ID",
                "HERMES_HOME",
                "ANTIGRAVITY_APP_DATA_DIR",
                "ANTIGRAVITY_EXECUTABLE_DATA_DIR",
            )
        }
        os.environ["HERMES_A2A_STATE_DIR"] = str(self.state_dir)
        os.environ.pop("HERMES_A2A_SWARM_ID", None)

    def tearDown(self) -> None:
        for k, val in self._env_backup.items():
            if val is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = val
        self._tmp.cleanup()

    def test_c1_observer_etag_cache_toctou_does_not_poison_on_mid_build_mutation(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-c1-toctou"
        coord_conv = "conv-c1-toctou-c"
        mgr.store.upsert_meta(
            sid,
            title="C1 TOCTOU",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", conversation_id=coord_conv, swarm_id=sid)
        post_t0 = mgr.board_service.post_message(sender="coordinator", summary="post T0", swarm_id=sid)
        head_t0 = int(post_t0["seq"])
        # Pre-warm runtime so manager.get_runtime(sid) does not call read_board before base_sig_before
        rt = mgr.get_runtime(sid)

        paths = mgr.store.paths(sid)
        cache_key = (str(paths.swarm_dir), sid)
        orig_read_board = mgr.board_service.read_board
        injected = False

        def _mutating_read_board(target_sid: str = "default", **kwargs):
            nonlocal injected
            snap_board = orig_read_board(target_sid, **kwargs)
            if not injected and target_sid == sid:
                injected = True
                # Simulate a concurrent post landing AFTER T0 read and BEFORE T1 cache store!
                mgr.board_service.post_message(sender="coordinator", summary="post T1 concurrent", swarm_id=sid)
            return snap_board

        try:
            with patch.object(mgr.board_service, "read_board", side_effect=_mutating_read_board):
                snap_t0 = http_server.build_observer_snapshot(mgr, sid)
            self.assertTrue(injected)
            self.assertEqual(snap_t0["head"], head_t0)
            # C1: Because light_sig_before != light_sig_after, T0 ETag must NOT be cached!
            with http_server._OBSERVER_ETAG_CACHE_LOCK:
                self.assertNotIn(cache_key, http_server._OBSERVER_ETAG_CACHE)
            self.assertIsNone(
                http_server.try_fast_unchanged_snapshot(
                    mgr, sid, conv_id=sid, known_etag=snap_t0["etag"], include_raw=True
                )
            )
            # Subsequent poll with T0 ETag must observe T1 post cleanly (not swallowed as unchanged)
            app = http_server.create_a2a_app(mgr)
            client = TestClient(app, base_url="http://127.0.0.1")
            r_next = client.get(f"/board?conversationId={sid}&etag={snap_t0['etag']}")
            self.assertEqual(r_next.status_code, 200)
            j_next = r_next.json()
            self.assertFalse(j_next.get("unchanged", False))
            self.assertEqual(j_next["head"], head_t0 + 1)
            self.assertNotEqual(j_next["etag"], snap_t0["etag"])

            # Also verify in-memory runtime mutation mid-snapshot does NOT poison _OBSERVER_ETAG_CACHE
            orig_get_tasks = rt.get_task_snapshots
            mutated_rt = False

            def _mutating_get_tasks(*args, **kwargs):
                nonlocal mutated_rt
                res = orig_get_tasks(*args, **kwargs)
                if not mutated_rt:
                    mutated_rt = True
                    with rt._state_rlock:
                        rt._tasks["task-c1-race"] = {
                            "id": "task-c1-race",
                            "contextId": "ctx-c1",
                            "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                            "artifacts": [],
                            "history": [],
                            "metadata": {"workerConfig": {"worker_name": "w-c1", "role": "worker"}},
                        }
                return res

            with patch.object(rt, "get_task_snapshots", side_effect=_mutating_get_tasks):
                snap_rt_t0 = http_server.build_observer_snapshot(mgr, sid)
            self.assertTrue(mutated_rt)
            with http_server._OBSERVER_ETAG_CACHE_LOCK:
                self.assertNotIn(cache_key, http_server._OBSERVER_ETAG_CACHE)
            self.assertIsNone(
                http_server.try_fast_unchanged_snapshot(
                    mgr, sid, conv_id=sid, known_etag=snap_rt_t0["etag"], include_raw=True
                )
            )
        finally:
            asyncio.run(mgr.shutdown())

    def test_c2_coordinator_pending_tools_mid_turn_stale_running_and_lp_ts_parity(self) -> None:
        app_data = self.tmp_root / "ag_c2"
        brain_dir = app_data / "brain"
        prim_uuid = "1111aaaa-1111-4111-8111-111111111111"
        coord_uuid = "2222bbbb-2222-4222-8222-222222222222"
        logs_dir = brain_dir / coord_uuid / ".system_generated" / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        tfile = logs_dir / "transcript_full.jsonl"

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        mgr.store.upsert_meta(
            prim_uuid,
            title="C2 Coord State",
            workspace=str(self.repo_dir),
            primary_conversation_id=prim_uuid,
            coordinator_conversation_id=coord_uuid,
        )
        mgr.board_service.upsert_member(
            "coordinator",
            role="coordinator",
            runtime="antigravity",
            state="working",
            conversation_id=coord_uuid,
            swarm_id=prim_uuid,
        )
        rt = mgr.get_runtime(prim_uuid)

        try:
            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}):
                # 1) PLANNER_RESPONSE status="DONE" with pending tool_calls -> MUST stay "working"
                steps_pending_tc = [
                    {
                        "step_index": 0,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": models.now_iso(),
                        "model": "gemini-c2",
                        "content": "Running command...",
                        "tool_calls": [{"id": "tc-1", "name": "run_command", "args": {"cmd": "pytest"}}],
                    },
                ]
                tfile.write_text("\n".join(json.dumps(s) for s in steps_pending_tc) + "\n", encoding="utf-8")
                snap1 = http_server.build_observer_snapshot(mgr, prim_uuid)
                coord1 = next(m for m in snap1["members"] if m["name"] == "coordinator")
                hist1 = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator", runtime=rt)
                self.assertEqual(coord1["status"], "working")
                self.assertEqual(hist1["state"], "working")

                # 2) GENERIC status="DONE" (tool result returned, LLM thinking next turn, fresh <60s) -> stays "working"
                steps_generic_tail = [
                    *steps_pending_tc,
                    {
                        "step_index": 1,
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": models.now_iso(),
                        "content": "pytest passed",
                    },
                ]
                tfile.write_text("\n".join(json.dumps(s) for s in steps_generic_tail) + "\n", encoding="utf-8")
                snap2 = http_server.build_observer_snapshot(mgr, prim_uuid)
                coord2 = next(m for m in snap2["members"] if m["name"] == "coordinator")
                hist2 = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator", runtime=rt)
                self.assertEqual(coord2["status"], "working")
                self.assertEqual(hist2["state"], "working")

                # 3) Crashed RUNNING step with transcript mtime > 120s old -> transitions to "waiting"
                steps_crashed_run = [
                    {
                        "step_index": 0,
                        "type": "PLANNER_RESPONSE",
                        "status": "RUNNING",
                        "created_at": "2020-01-01T00:00:00Z",
                        "model": "gemini-c2",
                        "content": "Stuck running...",
                    },
                ]
                tfile.write_text("\n".join(json.dumps(s) for s in steps_crashed_run) + "\n", encoding="utf-8")
                stale_epoch = time.time() - 180.0
                os.utime(tfile, (stale_epoch, stale_epoch))
                snap3 = http_server.build_observer_snapshot(mgr, prim_uuid)
                coord3 = next(m for m in snap3["members"] if m["name"] == "coordinator")
                hist3 = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator", runtime=rt)
                self.assertEqual(coord3["status"], "waiting")
                self.assertEqual(hist3["state"], "waiting")
                self.assertFalse(hist3["live"]["active"])

                # 4) Stale GENERIC (>60s old mtime) + old meta/member updatedAt, BUT fresh coordinator board post lp["ts"] -> BOTH agree "working"
                tfile.write_text("\n".join(json.dumps(s) for s in steps_generic_tail) + "\n", encoding="utf-8")
                stale_gen = time.time() - 90.0
                os.utime(tfile, (stale_gen, stale_gen))
                mgr.board_service.post_message(sender="coordinator", summary="fresh board post", swarm_id=prim_uuid)
                # Backdate member updatedAt/lastHeartbeatAt & meta.updatedAt so ONLY lp["ts"] is fresh
                b_doc = mgr.board_service.read_board(prim_uuid)
                b_doc["members"]["coordinator"]["updatedAt"] = "2020-01-01T00:00:00Z"
                b_doc["members"]["coordinator"]["lastHeartbeatAt"] = "2020-01-01T00:00:00Z"
                mgr.store.write_board(prim_uuid, b_doc)
                m_doc = mgr.store.read_meta(prim_uuid)
                assert m_doc is not None
                m_doc["updatedAt"] = "2020-01-01T00:00:00Z"
                store.atomic_write_json(mgr.store.paths(prim_uuid).meta_file, m_doc)

                snap4 = http_server.build_observer_snapshot(mgr, prim_uuid)
                coord4 = next(m for m in snap4["members"] if m["name"] == "coordinator")
                hist4 = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator", runtime=rt)
                self.assertEqual(coord4["status"], "working")
                self.assertEqual(hist4["state"], "working")

                # 5) Terminal text-only PLANNER_RESPONSE (zero pending tool calls) -> immediately "waiting"
                steps_final_done = [
                    *steps_generic_tail,
                    {
                        "step_index": 2,
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": models.now_iso(),
                        "model": "gemini-c2",
                        "content": "Completed!",
                        "tool_calls": [],
                    },
                ]
                tfile.write_text("\n".join(json.dumps(s) for s in steps_final_done) + "\n", encoding="utf-8")
                snap5 = http_server.build_observer_snapshot(mgr, prim_uuid)
                coord5 = next(m for m in snap5["members"] if m["name"] == "coordinator")
                hist5 = runtime_mod.query_member_session_history(self.state_dir, prim_uuid, "coordinator", runtime=rt)
                self.assertEqual(coord5["status"], "waiting")
                self.assertEqual(hist5["state"], "waiting")
        finally:
            asyncio.run(mgr.shutdown())

    def test_h1_scan_disk_view_change_does_not_swallow_concurrent_board_post(self) -> None:
        async def _run() -> None:
            bs = board_mod.BoardService(self.state_dir)
            sid = "conv-h1-view-race"
            bs.upsert_member("narrator", role="narrator", state="waiting", swarm_id=sid)
            bs.upsert_member("w-target", role="worker", state="waiting", swarm_id=sid)
            bs.post_message(sender="coordinator", summary="seed #1", swarm_id=sid)

            woken_prompts: list[tuple[str, str]] = []

            async def _wake(swarm_id: str, target: str, prompt: str) -> bool:
                woken_prompts.append((target, prompt))
                return True

            router = wakeup_mod.MentionRouter(bs, wake_worker=_wake, debounce_seconds=0.0)
            router.seed_existing([sid])

            # Narrator updates view.json (view_changed=True, sig_changed=False)
            paths = store.resolve_swarm_paths(sid, state_dir=self.state_dir)
            (paths.swarm_dir / "view.json").write_text(json.dumps({"summary": "v1"}), encoding="utf-8")

            orig_mark = bs.mark_member_read
            concurrent_posted = False

            def _racy_mark(agent: str, seq: int, **kwargs):
                nonlocal concurrent_posted
                res = orig_mark(agent, seq, **kwargs)
                if not concurrent_posted:
                    concurrent_posted = True
                    # Message #2 arrives right between read_board() and st_after stat()!
                    bs.post_message(sender="coordinator", summary="@w-target urgent #2", swarm_id=sid)
                return res

            with patch.object(bs, "mark_member_read", side_effect=_racy_mark):
                await router.poll_disk_once([sid])
            # Even if a second poll runs, #2 must already be routed on the first poll (and never dropped!)
            self.assertTrue(
                any(t == "w-target" and "urgent #2" in p for t, p in woken_prompts),
                msg=f"Concurrent post #2 was dropped during view.json update! woken={woken_prompts}",
            )

        asyncio.run(_run())

    def test_h2_and_h3_compact_vs_full_etag_distinct_and_unchanged_has_conversation_ids(self) -> None:
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-h23-prim"
        coord_conv = "conv-h23-coord"
        third_conv = "conv-h23-worker3"
        mgr.store.upsert_meta(
            sid,
            title="H2 H3 Test",
            workspace=str(self.repo_dir),
            primary_conversation_id=sid,
            coordinator_conversation_id=coord_conv,
        )
        # Add 3rd conversation ID into meta.conversationIds
        m_doc = mgr.store.read_meta(sid)
        assert m_doc is not None
        m_doc["conversationIds"] = [sid, coord_conv, third_conv]
        store.atomic_write_json(mgr.store.paths(sid).meta_file, m_doc)
        with store._META_CACHE_LOCK:
            store._META_CACHE.clear()
        with store._CONV_RESOLVE_CACHE_LOCK:
            store._CONV_RESOLVE_CACHE.clear()

        mgr.board_service.upsert_member("coordinator", role="coordinator", runtime="antigravity", state="waiting", conversation_id=coord_conv, swarm_id=sid)
        mgr.board_service.post_message(sender="coordinator", summary="hello world", swarm_id=sid)

        app = http_server.create_a2a_app(mgr)
        client = TestClient(app, base_url="http://127.0.0.1")
        try:
            # H2: compact ETag vs full ETag must be distinct and NEVER cross-match!
            r_comp = client.get(f"/board?conversationId={third_conv}&compact=1")
            self.assertEqual(r_comp.status_code, 200)
            j_comp = r_comp.json()
            self.assertEqual(j_comp["raw"], {})
            etag_compact = j_comp["etag"]

            # Passing compact ETag to full /board (include_raw=True) MUST NOT return unchanged or 304
            r_full_with_comp_q = client.get(f"/board?conversationId={third_conv}&etag={etag_compact}")
            self.assertEqual(r_full_with_comp_q.status_code, 200)
            j_full = r_full_with_comp_q.json()
            self.assertFalse(j_full.get("unchanged", False))
            self.assertIn("board", j_full["raw"])
            self.assertTrue(bool(j_full["raw"]["board"]))
            etag_full = j_full["etag"]
            self.assertNotEqual(etag_full, etag_compact)

            r_full_with_comp_hdr = client.get(f"/board?conversationId={third_conv}", headers={"If-None-Match": etag_compact})
            self.assertEqual(r_full_with_comp_hdr.status_code, 200)
            self.assertIn("board", r_full_with_comp_hdr.json()["raw"])

            # Both compact & full ETags hit fast-path unchanged for their own representation AND carry conversationIds (H3)
            with patch.object(http_server, "build_observer_snapshot", side_effect=AssertionError("Expected fast-path")):
                r_fast_full = client.get(f"/board?conversationId={third_conv}&etag={etag_full}")
                self.assertEqual(r_fast_full.status_code, 200)
                jf = r_fast_full.json()
                self.assertTrue(jf["unchanged"])
                self.assertIn(third_conv, jf.get("conversationIds") or [])
                self.assertIn(sid, jf.get("conversationIds") or [])
                self.assertIn(coord_conv, jf.get("conversationIds") or [])

                r_fast_comp = client.get(f"/board?conversationId={third_conv}&compact=1&etag={etag_compact}")
                self.assertEqual(r_fast_comp.status_code, 200)
                jc = r_fast_comp.json()
                self.assertTrue(jc["unchanged"])
                self.assertIn(third_conv, jc.get("conversationIds") or [])

            # Full-path unchanged (fast-path bypassed) must ALSO carry conversationIds (H3)
            with patch.object(http_server, "try_fast_unchanged_snapshot", return_value=None):
                r_slow_unchanged = client.get(f"/board?conversationId={third_conv}&etag={etag_full}")
                self.assertEqual(r_slow_unchanged.status_code, 200)
                js = r_slow_unchanged.json()
                self.assertTrue(js["unchanged"])
                self.assertIn(third_conv, js.get("conversationIds") or [])
        finally:
            asyncio.run(mgr.shutdown())

    def test_h4_subagent_stuck_expiry_and_new_brain_view_json_busts_cache(self) -> None:
        from datetime import datetime, timedelta, timezone

        app_data = self.tmp_root / "ag_h4"
        brain_dir = app_data / "brain"
        brain_dir.mkdir(parents=True, exist_ok=True)
        narr_cid = "3333cccc-3333-4333-8333-333333333333"

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-h4-cache"
        mgr.store.upsert_meta(sid, title="H4 Test", workspace=str(self.repo_dir), primary_conversation_id=sid)
        mgr.board_service.upsert_member("narrator", role="narrator", state="waiting", conversation_id=narr_cid, swarm_id=sid)
        mgr.board_service.upsert_member("w-parent", role="worker", state="working", swarm_id=sid)

        old_ts = "2020-01-01T00:00:00Z"
        rt = mgr.get_runtime(sid)
        with rt._state_rlock:
            rt._tasks["task-h4-parent"] = {
                "id": "task-h4-parent",
                "contextId": "ctx-h4",
                "status": {"state": models.TaskState.WORKING.value, "timestamp": old_ts},
                "artifacts": [],
                "history": [],
                "metadata": {
                    "createdAt": old_ts,
                    "workerConfig": {"worker_name": "w-parent", "role": "worker", "model": "m1", "provider": "p1"},
                    "liveActivity": {
                        "startedAt": old_ts,
                        "lastEventAt": old_ts,
                        "currentTool": {"name": "delegate_task", "preview": "sub", "startedAt": old_ts},
                    },
                },
            }
        with mgr.board_service._locked_board(sid, mutate=True) as (_, b_fix, _):
            b_fix["members"]["w-parent"]["lastHeartbeatAt"] = old_ts
            b_fix["members"]["w-parent"]["updatedAt"] = old_ts

        # Subagent updated 9m58s ago -> 2s left before STUCK_MS
        sub_near_stale = (datetime.now(timezone.utc) - timedelta(seconds=598)).isoformat().replace("+00:00", "Z")
        mgr.board_service.upsert_subagents(
            "w-parent",
            [{"id": "sub-h4-1", "name": "subagent-1", "status": "working", "updatedAt": sub_near_stale}],
            swarm_id=sid,
        )

        paths = mgr.store.paths(sid)
        cache_key = (str(paths.swarm_dir), sid)
        try:
            with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(app_data)}):
                before_m = time.monotonic()
                snap1 = http_server.build_observer_snapshot(mgr, sid)
                self.assertFalse(any(a.get("kind") == "stuck" for a in snap1["systemAttention"]))
                self.assertFalse(snap1["view"]["exists"])
                with http_server._OBSERVER_ETAG_CACHE_LOCK:
                    entry = dict(http_server._OBSERVER_ETAG_CACHE[cache_key])
                # H4 part 1: nextExpiryMono must be bounded by sub_updated_ms + STUCK_MS (not inf!)
                self.assertTrue(entry["nextExpiryMono"] < float("inf"))
                self.assertLessEqual(entry["nextExpiryMono"] - before_m, 10.0)

                # H4 part 2: Creating brain/<narr_cid>/swarm_view/view.json later MUST bust fast-path cache!
                # Bump nextExpiryMono so we isolate the brain-dir view.json detection specifically.
                with http_server._OBSERVER_ETAG_CACHE_LOCK:
                    http_server._OBSERVER_ETAG_CACHE[cache_key]["nextExpiryMono"] = time.monotonic() + 300.0
                self.assertIsNotNone(
                    http_server.try_fast_unchanged_snapshot(
                        mgr, sid, conv_id=sid, known_etag=snap1["etag"], include_raw=True
                    )
                )
                brain_view_dir = brain_dir / narr_cid / "swarm_view"
                brain_view_dir.mkdir(parents=True, exist_ok=True)
                (brain_view_dir / "view.json").write_text(json.dumps({"summary": "fresh brain view"}), encoding="utf-8")
                self.assertIsNone(
                    http_server.try_fast_unchanged_snapshot(
                        mgr, sid, conv_id=sid, known_etag=snap1["etag"], include_raw=True
                    )
                )
                snap2 = http_server.build_observer_snapshot(mgr, sid)
                self.assertTrue(snap2["view"]["exists"])
                self.assertEqual(snap2["view"]["source"], "brain")
        finally:
            asyncio.run(mgr.shutdown())

    def test_h5_swarms_with_pending_thread_safe_under_concurrent_requeue(self) -> None:
        async def _run() -> None:
            bs = board_mod.BoardService(self.state_dir)
            router = wakeup_mod.MentionRouter(bs, debounce_seconds=0.0)
            stop_flag = threading.Event()
            errors: list[Exception] = []

            def _writer_thread(idx: int) -> None:
                try:
                    for j in range(60):
                        if stop_flag.is_set():
                            break
                        sid = f"sw-h5-{idx}-{j % 12}"
                        router._enqueue_batch_on_failure(sid, "w1", [{"seq": j + 1, "summary": "retry"}])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=_writer_thread, args=(i,), daemon=True) for i in range(4)]
            for t in threads:
                t.start()
            try:
                for _ in range(25):
                    await router.poll_disk_once()
                    await asyncio.sleep(0.002)
            finally:
                stop_flag.set()
                for t in threads:
                    t.join(timeout=5.0)
            self.assertEqual(errors, [])

        asyncio.run(_run())

    def test_m3_m4_m6_m7_fingerprint_wakeseq_reap_lock_and_transcript_cas(self) -> None:
        # M6: _reap_lock_file MUST refuse to reap a live holder lock when expected_token is None
        lpath = self.tmp_root / "m6.lock"
        lpath.write_text(
            json.dumps({"pid": os.getpid(), "ts": time.time(), "token": "live-holder-tok", "released": False}),
            encoding="utf-8",
        )
        self.assertFalse(store._reap_lock_file(lpath, expected_token=None))
        self.assertTrue(lpath.exists())
        # When lock genuinely has no token (None), expected_token=None succeeds
        lpath_notok = self.tmp_root / "m6_notok.lock"
        lpath_notok.write_text(json.dumps({"pid": 9999999, "ts": 0.0, "released": False}), encoding="utf-8")
        self.assertTrue(store._reap_lock_file(lpath_notok, expected_token=None))
        self.assertFalse(lpath_notok.exists())

        # M3 + M4: get_observer_state_fingerprint covers all live fields & list_active_wake_swarms honors memory wakeSeq
        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = "conv-m34"
        mgr.store.upsert_meta(sid, title="M3 M4", workspace=str(self.repo_dir), primary_conversation_id=sid)
        rt = mgr.get_runtime(sid)
        try:
            # M4: memory wakeSeq ahead of disk meta.json
            with mgr._lock:
                mgr._wake_seq_by_swarm[sid] = 9
            swarms = mgr.list_active_wake_swarms()
            row = next(s for s in swarms if s["swarmId"] == sid)
            self.assertEqual(row["wakeSeq"], 9)

            # M3: every live field mutates get_observer_state_fingerprint()
            with rt._state_rlock:
                rt._tasks["t-m3"] = {
                    "id": "t-m3",
                    "contextId": "ctx-m3",
                    "status": {"state": models.TaskState.WORKING.value, "timestamp": models.now_iso()},
                    "artifacts": [],
                    "history": [],
                    "metadata": {
                        "createdAt": models.now_iso(),
                        "workerConfig": {"worker_name": "w1", "role": "worker"},
                        "tokens": {"pureInput": 100, "cachedInput": 200, "cacheWrite": 10, "output": 50, "total": 360},
                        "worktreePath": "/wt/a",
                        "worktreeBranch": "br-a",
                        "liveActivity": {
                            "currentTool": {"name": "bash", "preview": "ls", "startedAt": "2026-11-28T10:00:00Z"},
                            "lastTextPreview": "hello",
                            "recentTools": [{"name": "read", "preview": "a.py", "status": "ok", "at": "t1"}],
                            "lastEventAt": "2026-11-28T10:00:01Z",
                            "hermesSessionId": "hs-1",
                        },
                    },
                }

            seen_fps: list[tuple] = [rt.get_observer_state_fingerprint()]
            mutations = [
                lambda t: t["metadata"]["liveActivity"]["currentTool"].__setitem__("preview", "pytest -q"),
                lambda t: t["metadata"]["liveActivity"].__setitem__("lastTextPreview", "updated preview"),
                lambda t: t["metadata"]["liveActivity"]["recentTools"].append({"name": "edit", "preview": "b.py", "status": "ok", "at": "t2"}),
                lambda t: t["metadata"]["liveActivity"].__setitem__("lastEventAt", "2026-11-28T10:00:05Z"),
                lambda t: t["metadata"]["liveActivity"].__setitem__("hermesSessionId", "hs-2"),
                lambda t: t["metadata"].__setitem__("worktreePath", "/wt/b"),
                lambda t: t["metadata"].__setitem__("worktreeBranch", "br-b"),
                lambda t: t["metadata"]["tokens"].__setitem__("pureInput", 120),
                lambda t: t["metadata"]["tokens"].__setitem__("cachedInput", 180),
                lambda t: t["metadata"]["tokens"].__setitem__("cacheWrite", 25),
            ]
            for idx_m, fn in enumerate(mutations):
                with rt._state_rlock:
                    fn(rt._tasks["t-m3"])
                fp = rt.get_observer_state_fingerprint()
                self.assertNotEqual(fp, seen_fps[-1], f"Mutation #{idx_m} did not change observer fingerprint")
                seen_fps.append(fp)

            # M3: active incomplete SQLite meta lookup bounds nextExpiryMono by _TASK_SESSION_META_ACTIVE_TTL_S (15s)
            with patch.object(runtime_mod, "_query_hermes_session_stats", return_value=({}, None, {"model": None, "billingProvider": None})):
                t_before = time.monotonic()
                http_server.build_observer_snapshot(mgr, sid)
            paths = mgr.store.paths(sid)
            with http_server._OBSERVER_ETAG_CACHE_LOCK:
                c_ent = http_server._OBSERVER_ETAG_CACHE.get((str(paths.swarm_dir), sid))
            self.assertIsNotNone(c_ent)
            assert c_ent is not None
            self.assertLessEqual(c_ent["nextExpiryMono"] - t_before, http_server._TASK_SESSION_META_ACTIVE_TTL_S + 2.0)

            # M7: transcript cache key includes st_ino and slower parser cannot evict newer revision
            tpath = self.tmp_root / "m7_transcript.jsonl"
            s1 = {"step_index": 0, "type": "PLANNER_RESPONSE", "status": "DONE", "content": "rev1", "tool_calls": []}
            tpath.write_text(json.dumps(s1) + "\n", encoding="utf-8")
            st1 = tpath.stat()
            older_key = (str(tpath), int(st1.st_mtime_ns), int(st1.st_size), int(getattr(st1, "st_ino", 0)))
            self.assertEqual(len(older_key), 4)

            # Pre-seed a newer revision key in _TRANSCRIPT_PATH_KEYS + _TRANSCRIPT_PARSE_CACHE
            newer_key = (str(tpath), int(st1.st_mtime_ns) + 1_000_000_000, int(st1.st_size) + 50, int(getattr(st1, "st_ino", 0)))
            newer_entry = runtime_mod._empty_parsed_transcript()
            newer_entry["model"] = "newer-rev-model"
            newer_entry["_msg_ids"] = []
            with runtime_mod._TRANSCRIPT_CACHE_LOCK:
                runtime_mod._TRANSCRIPT_PARSE_CACHE[newer_key] = newer_entry
                runtime_mod._TRANSCRIPT_SUMMARY_CACHE[newer_key] = runtime_mod._build_transcript_summary_from_parsed(newer_entry)
                runtime_mod._TRANSCRIPT_PATH_KEYS[str(tpath)] = newer_key
            # Parsing the older disk revision must NOT evict newer_key
            runtime_mod._parse_antigravity_transcript_cached_entry(tpath)
            with runtime_mod._TRANSCRIPT_CACHE_LOCK:
                self.assertEqual(runtime_mod._TRANSCRIPT_PATH_KEYS.get(str(tpath)), newer_key)
                self.assertIn(newer_key, runtime_mod._TRANSCRIPT_PARSE_CACHE)
        finally:
            asyncio.run(mgr.shutdown())


class AntigravityMemberRegistrationAndDiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp_root = Path(self._tmp.name)
        self.state_dir = self.tmp_root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.brain_root = self.tmp_root / "app_data" / "brain"
        self.brain_root.mkdir(parents=True, exist_ok=True)
        self.repo_dir = self.tmp_root / "repo"
        self.repo_dir.mkdir(parents=True, exist_ok=True)

        self.orig_mgr = mcp_srv._swarm_mgr
        self.orig_http = mcp_srv._http_ctrl
        self.orig_wd = mcp_srv._session_workdir
        self.orig_conv = mcp_srv._session_conversation_id
        self.orig_sid = mcp_srv._session_swarm_id

        mcp_srv._swarm_mgr = runtime_mod.SwarmManager(
            state_dir=self.state_dir,
            default_workdir_provider=lambda: str(self.repo_dir),
        )
        mcp_srv._http_ctrl = http_server.EmbeddedA2AHttpController(mcp_srv._swarm_mgr)
        mcp_srv._session_workdir = str(self.repo_dir)
        mcp_srv._session_conversation_id = None
        mcp_srv._session_swarm_id = None

    def tearDown(self) -> None:
        mcp_srv._swarm_mgr = self.orig_mgr
        mcp_srv._http_ctrl = self.orig_http
        mcp_srv._session_workdir = self.orig_wd
        mcp_srv._session_conversation_id = self.orig_conv
        mcp_srv._session_swarm_id = self.orig_sid
        self._tmp.cleanup()

    def test_register_and_remove_member_and_auto_discover_children(self) -> None:
        prim_cid = "11111111-1111-4111-8111-111111111111"
        coord_cid = "22222222-2222-4222-8222-222222222222"
        arch_cid = "33333333-3333-4333-8333-333333333333"
        ver_cid = "44444444-4444-4444-8444-444444444444"

        for cid in (prim_cid, coord_cid, arch_cid, ver_cid):
            (self.brain_root / cid / ".system_generated" / "logs").mkdir(parents=True, exist_ok=True)

        coord_log = self.brain_root / coord_cid / ".system_generated" / "logs" / "transcript.jsonl"
        coord_steps = [
            {
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "model": "gemini-4-hydrogen-medium",
                "input_tokens": 1000,
                "cache_read_tokens": 200,
                "output_tokens": 120,
                "created_at": "2026-11-28T10:00:00Z",
                "tool_calls": [
                    {
                        "id": "tc-1",
                        "name": "invoke_subagent",
                        "args": {
                            "TypeName": "\"self\"",
                            "Role": "\"Systems Kernel Architect\"",
                            "Model": "\"pro\"",
                            "Prompt": "\"Diagnose indexing\"",
                        },
                    },
                    {
                        "id": "tc-2",
                        "name": "invoke_subagent",
                        "args": {
                            "TypeName": "self",
                            "Role": "Adversarial Verifier",
                            "Model": "inherit",
                            "Prompt": "Verify patch",
                        },
                    },
                ],
            },
            {
                "step_index": 1,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-28T10:00:01Z",
                "content": f'Created the following subagent:\n{{\n  "conversationId": "{arch_cid}"\n}}',
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-28T10:00:02Z",
                "content": f'Created the following subagent:\n{{\n  "conversationId": "{ver_cid}"\n}}',
            },
        ]
        coord_log.write_text("\n".join(json.dumps(s) for s in coord_steps) + "\n", encoding="utf-8")

        arch_log = self.brain_root / arch_cid / ".system_generated" / "logs" / "transcript.jsonl"
        arch_steps = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "status": "DONE",
                "created_at": "2026-11-28T10:00:02Z",
                "content": "Diagnose indexing",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "model": "gemini-4-pro-xhigh",
                "input_tokens": 2500,
                "cache_read_tokens": 500,
                "output_tokens": 400,
                "created_at": "2026-11-28T10:00:07Z",
                "tool_calls": [{"id": "a-tc-1", "name": "read_file", "args": {"Path": "/tmp/a.py"}}],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "RUNNING",
                "created_at": "2026-11-28T10:00:08Z",
                "content": "",
            },
        ]
        arch_log.write_text("\n".join(json.dumps(s) for s in arch_steps) + "\n", encoding="utf-8")

        ver_log = self.brain_root / ver_cid / ".system_generated" / "logs" / "transcript.jsonl"
        ver_log.write_text(
            json.dumps({
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "model": "gemini-4-pro-high",
                "input_tokens": 1200,
                "output_tokens": 180,
                "created_at": "2026-11-28T10:00:10Z",
                "content": "[VERIFIER: PASS]",
                "tool_calls": [],
            }) + "\n",
            encoding="utf-8",
        )

        env_patch = {"ANTIGRAVITY_APP_DATA_DIR": str(self.tmp_root / "app_data")}
        with patch.dict(os.environ, env_patch, clear=False):
            async def _run() -> dict[str, Any]:
                init_res = json.loads(await mcp_srv.swarm_manage(
                    "init",
                    primary_conversation_id=prim_cid,
                    title="Bridge Swarm",
                    workspace=str(self.repo_dir),
                ))
                sid = init_res["swarm_id"]

                await mcp_srv.swarm_manage(
                    "bind_coordinator",
                    primary_conversation_id=prim_cid,
                    coordinator_conversation_id=coord_cid,
                )

                # Invalid conversationId on register_member must fail closed.
                with self.assertRaises(ValueError):
                    await mcp_srv.swarm_manage(
                        "register_member",
                        primary_conversation_id=prim_cid,
                        name="bad-arch",
                        role="architect",
                        conversation_id="00000000-0000-4000-8000-000000000000",
                    )

                # Explicit register_member registers arch_cid with preserved architect role, extracted model, and meta.conversationIds.
                reg_res = json.loads(await mcp_srv.swarm_manage(
                    "register_member",
                    primary_conversation_id=prim_cid,
                    name="sys-arch",
                    role="architect",
                    conversation_id=arch_cid,
                    assignment="Diagnose indexing",
                ))
                self.assertEqual(reg_res["member"]["role"], "architect")
                self.assertEqual(reg_res["member"]["runtime"], "antigravity")
                self.assertEqual(reg_res["member"]["model"], "gemini-4-pro-xhigh")
                self.assertIn(arch_cid, reg_res["meta"]["conversationIds"])
                self.assertEqual(mcp_srv._swarm_mgr.store.resolve_swarm_for_conversation(arch_cid), sid)
                return {"sid": sid}

            res = asyncio.run(_run())
            sid = res["sid"]

            # Snapshot surfaces coordinator, explicit sys-arch, AND auto-discovered Adversarial Verifier!
            snap1 = http_server.build_observer_snapshot(mcp_srv._swarm_mgr, sid)
            by_name = {m["name"]: m for m in snap1["members"]}
            self.assertIn("coordinator", by_name)
            self.assertIn("sys-arch", by_name)
            self.assertEqual(len([m for m in snap1["members"] if m.get("conversationId") == arch_cid]), 1)
            ver_mems = [m for m in snap1["members"] if m.get("conversationId") == ver_cid]
            self.assertEqual(len(ver_mems), 1)
            ver_mem = ver_mems[0]
            self.assertEqual(ver_mem["runtime"], "antigravity")
            self.assertEqual(ver_mem["role"], "verifier")
            self.assertEqual(ver_mem["model"], "gemini-4-pro-high")
            self.assertEqual(ver_mem["status"], "waiting")
            self.assertTrue(ver_mem["autoDiscovered"])

            arch_mem = by_name["sys-arch"]
            self.assertEqual(arch_mem["model"], "gemini-4-pro-xhigh")
            self.assertEqual(arch_mem["steps"], 1)
            self.assertEqual(arch_mem["toolCalls"], 1)
            self.assertEqual(arch_mem["tokens"]["total"], 3400)

            # History query on child Antigravity member resolves strictly its OWN transcript, not Coordinator's.
            hist = runtime_mod.query_member_session_history(self.state_dir, sid, "sys-arch")
            self.assertEqual(hist["conversationId"], arch_cid)
            self.assertEqual(hist["model"], "gemini-4-pro-xhigh")
            self.assertTrue(any("Diagnose indexing" in str(m.get("content") or "") for m in hist["messages"]))

            # Fast ETag hit works and invalidates when child transcript updates on disk.
            fast_hit = http_server.try_fast_unchanged_snapshot(
                mcp_srv._swarm_mgr,
                sid,
                conv_id=prim_cid,
                known_etag=snap1["etag"],
            )
            self.assertIsNotNone(fast_hit)

            arch_steps.append({
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "model": "gemini-4-pro-xhigh",
                "input_tokens": 3000,
                "output_tokens": 500,
                "created_at": "2026-11-28T10:00:12Z",
                "content": "Root cause identified.",
                "tool_calls": [],
            })
            arch_log.write_text("\n".join(json.dumps(s) for s in arch_steps) + "\n", encoding="utf-8")
            fast_miss = http_server.try_fast_unchanged_snapshot(
                mcp_srv._swarm_mgr,
                sid,
                conv_id=prim_cid,
                known_etag=snap1["etag"],
            )
            self.assertIsNone(fast_miss)

            # remove_member(state="done") updates board member state cleanly.
            asyncio.run(mcp_srv.swarm_manage(
                "remove_member",
                primary_conversation_id=prim_cid,
                name="sys-arch",
                state="done",
            ))
            snap2 = http_server.build_observer_snapshot(mcp_srv._swarm_mgr, sid)
            arch_after = next(m for m in snap2["members"] if m["name"] == "sys-arch")
            self.assertEqual(arch_after["status"], "waiting")
            self.assertFalse(arch_after["killed"])

    def test_resolve_swarm_for_conversation_discovers_unbound_coordinator_and_child(self) -> None:
        """Even if coordinatorConversationId is null in meta.json, opening Coordinator or its IDE subagent keeps the Swarm UI attached."""
        brain_root = self.tmp_root / "app_data_unbound" / "brain"
        prim_cid = "c6d9002f-4613-4405-8757-988d9f93cf44"
        coord_cid = "fb15139e-eb42-4aaf-8c07-091ccb18bd4a"
        child_cid = "c9f01eb6-930c-42b5-83f4-88fc5db0901f"

        (brain_root / prim_cid / ".system_generated" / "subagents").mkdir(parents=True, exist_ok=True)
        (brain_root / prim_cid / ".system_generated" / "subagents" / f"{coord_cid}.json").write_text(
            json.dumps({"conversationId": coord_cid, "subagentDescriptor": {"role": "Swarm Coordinator"}}),
            encoding="utf-8",
        )
        (brain_root / coord_cid / ".system_generated" / "subagents").mkdir(parents=True, exist_ok=True)
        (brain_root / coord_cid / ".system_generated" / "subagents" / f"{child_cid}.json").write_text(
            json.dumps({"conversationId": child_cid, "subagentDescriptor": {"role": "Architect"}}),
            encoding="utf-8",
        )

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = store.derive_swarm_id(conversation_id=prim_cid)
        mgr.store.upsert_meta(
            sid,
            title="Unbound Coord Swarm",
            workspace=str(self.repo_dir),
            primary_conversation_id=prim_cid,
        )
        with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(self.tmp_root / "app_data_unbound")}, clear=False):
            store._CONV_RESOLVE_CACHE.clear()
            self.assertEqual(mgr.store.resolve_swarm_for_conversation(coord_cid), sid)
            meta_after_coord = mgr.store.read_meta(sid) or {}
            self.assertEqual(meta_after_coord.get("coordinatorConversationId"), coord_cid)
            self.assertIn(coord_cid, meta_after_coord.get("conversationIds") or [])

            store._CONV_RESOLVE_CACHE.clear()
            self.assertEqual(mgr.store.resolve_swarm_for_conversation(child_cid), sid)
            meta_after_child = mgr.store.read_meta(sid) or {}
            self.assertIn(child_cid, meta_after_child.get("conversationIds") or [])

    def test_build_observer_snapshot_persists_discovered_coordinator_and_children(self) -> None:
        """Snapshot auto-persists discovered Coordinator & child IDE subagent conversationIds into meta.json."""
        brain_root = self.tmp_root / "app_data_snap_persist" / "brain"
        prim_cid = "dddd1111-1111-4111-8111-111111111111"
        coord_cid = "eeee2222-2222-4222-8222-222222222222"
        child_cid = "ffff3333-3333-4333-8333-333333333333"

        (brain_root / prim_cid / ".system_generated" / "subagents").mkdir(parents=True, exist_ok=True)
        (brain_root / prim_cid / ".system_generated" / "subagents" / f"{coord_cid}.json").write_text(
            json.dumps({"conversationId": coord_cid, "subagentDescriptor": {"role": "Swarm Coordinator"}}),
            encoding="utf-8",
        )
        coord_logs = brain_root / coord_cid / ".system_generated" / "logs"
        coord_logs.mkdir(parents=True, exist_ok=True)
        (coord_logs / "transcript_full.jsonl").write_text(
            "\n".join([
                json.dumps({
                    "step_index": 0,
                    "type": "PLANNER_RESPONSE",
                    "status": "WAITING",
                    "model": "gemini-4-hydrogen-medium",
                    "created_at": "2026-11-28T11:00:00Z",
                    "tool_calls": [{
                        "name": "invoke_subagent",
                        "args": {"Role": "Research Analyst", "Prompt": "Inspect repo", "Model": "pro"},
                    }],
                }),
                json.dumps({
                    "step_index": 1,
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-28T11:00:01Z",
                    "content": json.dumps({"conversationId": child_cid}),
                }),
            ]) + "\n",
            encoding="utf-8",
        )
        child_logs = brain_root / child_cid / ".system_generated" / "logs"
        child_logs.mkdir(parents=True, exist_ok=True)
        (child_logs / "transcript_full.jsonl").write_text(
            json.dumps({
                "step_index": 0,
                "type": "PLANNER_RESPONSE",
                "status": "DONE",
                "model": "gemini-4-pro-high",
                "created_at": "2026-11-28T11:00:05Z",
                "content": "Done",
            }) + "\n",
            encoding="utf-8",
        )

        mgr = runtime_mod.SwarmManager(state_dir=self.state_dir)
        sid = store.derive_swarm_id(conversation_id=prim_cid)
        mgr.store.upsert_meta(sid, title="Persist Test", workspace=str(self.repo_dir), primary_conversation_id=prim_cid)
        mgr.board_service.upsert_member("coordinator", role="coordinator", tier=2, runtime="antigravity", state="working", swarm_id=sid)

        with patch.dict(os.environ, {"ANTIGRAVITY_APP_DATA_DIR": str(self.tmp_root / "app_data_snap_persist")}, clear=False):
            snap = http_server.build_observer_snapshot(mgr, sid)
            coord_m = next(m for m in snap["members"] if m["name"] == "coordinator")
            self.assertEqual(coord_m["conversationId"], coord_cid)
            meta_now = mgr.store.read_meta(sid) or {}
            self.assertEqual(meta_now.get("coordinatorConversationId"), coord_cid)
            self.assertIn(coord_cid, meta_now.get("conversationIds") or [])
            self.assertIn(child_cid, meta_now.get("conversationIds") or [])
            store._CONV_RESOLVE_CACHE.clear()
            self.assertEqual(mgr.store.resolve_swarm_for_conversation(coord_cid), sid)
            self.assertEqual(mgr.store.resolve_swarm_for_conversation(child_cid), sid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
