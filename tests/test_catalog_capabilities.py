from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


SCRIPT = (
    Path(__file__).parents[1]
    / "skills"
    / "catalog-capabilities-zh"
    / "scripts"
    / "catalog_capabilities.py"
)
SPEC = importlib.util.spec_from_file_location("catalog_capabilities", SCRIPT)
assert SPEC and SPEC.loader
catalog = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(catalog)


class CatalogFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.codex = self.root / ".codex"
        self.skills = self.codex / "skills"
        self.plugins = self.codex / "plugins"
        self.config_path = self.root / "app-config" / "config.json"
        self.catalog_path = self.root / "能力说明.md"

        self.write_skill(self.skills / "demo", "demo", "Create verified demo output.")
        self.write_skill(
            self.skills / ".system" / "skill-installer",
            "skill-installer",
            "Install skills from trusted sources.",
        )
        plugin = self.plugins / "cache" / "test-market" / "demo-plugin" / "1.2.0"
        (plugin / ".codex-plugin").mkdir(parents=True)
        (plugin / ".codex-plugin" / "plugin.json").write_text(
            json.dumps(
                {
                    "name": "demo-plugin",
                    "version": "1.2.0",
                    "description": "A demo plugin.",
                    "repository": "https://github.com/example/demo-plugin.git",
                    "skills": "./skills",
                    "interface": {
                        "displayName": "Demo Plugin",
                        "longDescription": "Read demo data through a configured integration.",
                        "capabilities": ["Read"],
                    },
                }
            ),
            encoding="utf-8",
        )
        self.write_skill(plugin / "skills" / "plugin-demo", "plugin-demo", "Summarize demo plugin data.")
        self.codex.mkdir(exist_ok=True)
        (self.codex / "config.toml").write_text(
            """
[marketplaces.test-market]
source = "https://github.com/example/market"

[plugins."demo-plugin@test-market"]
enabled = true
""".strip(),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def write_skill(path: Path, name: str, description: str) -> None:
        path.mkdir(parents=True, exist_ok=True)
        (path / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n",
            encoding="utf-8",
        )

    @staticmethod
    def make_directory_link(link: Path, target: Path) -> None:
        try:
            link.symlink_to(target, target_is_directory=True)
            return
        except OSError:
            if os.name != "nt":
                raise
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            raise OSError(completed.stderr or completed.stdout)

    def profile(self, include_system: bool = False) -> dict:
        return {
            "agent": "codex",
            "skill_roots": [{"path": str(self.skills), "alias": "skills-1"}],
            "plugin_roots": [{"path": str(self.plugins), "alias": "plugins-1"}],
            "codex_config": str(self.codex / "config.toml"),
            "catalog_path": str(self.catalog_path),
            "include_system": include_system,
        }

    def run_main(self, *args: str) -> tuple[int, dict]:
        stream = io.StringIO()
        with contextlib.redirect_stdout(stream):
            code = catalog.main(list(args))
        output = stream.getvalue().strip()
        return code, json.loads(output) if output else {}

    def configure(self, *, include_system: bool = True) -> None:
        args = [
            "configure",
            "--config",
            str(self.config_path),
            "--codex-home",
            str(self.codex),
            "--skill-root",
            str(self.skills),
            "--plugin-root",
            str(self.plugins),
            "--catalog",
            str(self.catalog_path),
        ]
        if include_system:
            args.append("--include-system")
        code, result = self.run_main(*args)
        self.assertEqual(code, 0)
        self.assertTrue(result["configured"])


class ScanTests(CatalogFixture):
    def test_scan_distinguishes_installed_enabled_cached_and_market(self) -> None:
        inventory = catalog.scan_profile("default", self.profile(include_system=False))
        by_kind = {item["kind"]: item for item in inventory["items"]}
        self.assertEqual(by_kind["skill"]["status"], "installed")
        self.assertEqual(by_kind["plugin"]["status"], "enabled")
        self.assertEqual(by_kind["plugin_skill"]["status"], "enabled")
        self.assertEqual(by_kind["marketplace"]["status"], "configured")
        self.assertNotIn("system_skill", by_kind)
        self.assertEqual(by_kind["plugin"]["source_url"], "https://github.com/example/demo-plugin")

    def test_include_system_and_installer_resolution(self) -> None:
        inventory = catalog.scan_profile("default", self.profile(include_system=True))
        self.assertIn("system_skill", {item["kind"] for item in inventory["items"]})
        resolution = catalog.installer_resolution(
            "codex", "skill", catalog.normalize_root_entries([self.skills], "skills")
        )
        self.assertTrue(resolution["supported"])
        self.assertTrue(resolution["detected_locally"])
        self.assertEqual(resolution["trusted_installer"], "skill-installer")

    def test_explicit_codex_home_overrides_saved_profile_roots(self) -> None:
        self.configure(include_system=False)
        alternate = self.root / "alternate-codex"
        self.write_skill(alternate / "skills" / "alternate", "alternate", "Alternate skill.")
        (alternate / "plugins").mkdir(parents=True)
        (alternate / "config.toml").write_text("", encoding="utf-8")
        code, result = self.run_main(
            "scan", "--config", str(self.config_path), "--codex-home", str(alternate)
        )
        self.assertEqual(code, 0)
        self.assertEqual([item["name"] for item in result["items"]], ["alternate"])

    def test_discover_reports_standard_and_current_roots(self) -> None:
        result = catalog.discover_paths(self.config_path, str(self.codex))
        paths = {item["path"] for item in result["candidates"]}
        self.assertIn(str(self.skills.resolve()), paths)
        default_skills = str((Path.home() / ".codex" / "skills").resolve())
        if default_skills != str(self.skills.resolve()):
            self.assertNotIn(default_skills, paths)
        skill_candidate = next(item for item in result["candidates"] if item["path"] == str(self.skills.resolve()))
        self.assertTrue(skill_candidate["readable"])
        self.assertGreaterEqual(skill_candidate["item_count"], 2)

    def test_discover_survives_corrupt_saved_config(self) -> None:
        self.config_path.parent.mkdir(parents=True)
        self.config_path.write_text("{not-json", encoding="utf-8")
        result = catalog.discover_paths(self.config_path, str(self.codex))
        self.assertTrue(result["candidates"])
        self.assertTrue(result["diagnostics"])

    def test_scan_depth_is_configurable(self) -> None:
        deep = self.skills / "one" / "two" / "three" / "deep"
        self.write_skill(deep, "deep-skill", "Deeply grouped skill.")
        shallow_profile = self.profile()
        shallow_profile["scan_depth"] = 2
        shallow = catalog.scan_profile("default", shallow_profile)
        self.assertNotIn("deep-skill", {item["name"] for item in shallow["items"]})
        deep_profile = self.profile()
        deep_profile["scan_depth"] = 6
        deep_inventory = catalog.scan_profile("default", deep_profile)
        self.assertIn("deep-skill", {item["name"] for item in deep_inventory["items"]})

    def test_link_group_discovery_requires_explicit_configuration(self) -> None:
        group = self.root / "skill-groups"
        target = self.root / "external" / "frontend"
        self.write_skill(target / "linked-demo", "linked-demo", "Design frontend interfaces.")
        group.mkdir()
        link = group / "frontend"
        try:
            self.make_directory_link(link, target)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")

        discovered = catalog.discover_linked_roots(group)
        self.assertEqual(len(discovered["candidates"]), 1)
        self.assertEqual(discovered["candidates"][0]["item_count"], 1)
        self.assertFalse(discovered["candidates"][0]["confirmed"])
        self.assertEqual(list(catalog.walk_named(group, "SKILL.md")), [])

        roots = catalog.normalize_root_entries([{
            "path": str(link),
            "alias": "frontend",
            "group": "前端技能",
            "confirmed": True,
        }], "skills")
        profile = self.profile()
        profile["skill_roots"] = roots
        inventory = catalog.scan_profile("default", profile)
        self.assertIn("linked-demo", {item["name"] for item in inventory["items"]})
        self.assertEqual(roots[0]["source_link"], str(link))

    def test_link_discovery_deduplicates_targets(self) -> None:
        group = self.root / "skill-groups"
        target = self.root / "external"
        self.write_skill(target / "demo", "external-demo", "External skill.")
        group.mkdir()
        first = group / "first"
        second = group / "second"
        try:
            self.make_directory_link(first, target)
            self.make_directory_link(second, target)
        except OSError as exc:
            self.skipTest(f"Directory links are unavailable: {exc}")
        result = catalog.discover_linked_roots(group)
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(any("Duplicate linked target" in line for line in result["diagnostics"]))


class RenderTests(CatalogFixture):
    def test_configure_scan_render_and_idempotence(self) -> None:
        self.configure()
        code, inventory = self.run_main("scan", "--config", str(self.config_path))
        self.assertEqual(code, 0)
        summaries = {"items": {}}
        for item in inventory["items"]:
            summaries["items"][item["id"]] = {
                "name_zh": f"中文 {item['name']}",
                "scope_zh": "适用于测试目录。",
                "effects_zh": "生成可验证的测试结果。",
                "triggers_zh": ["用户要求测试时"],
                "limitations_zh": [],
                "permissions_zh": [],
                "verification": "已验证",
                "evidence_urls": [item["source_url"]] if item.get("source_url") else [],
            }
        summary_path = self.root / "summaries.json"
        summary_path.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
        code, first = self.run_main(
            "render", "--config", str(self.config_path), "--summaries", str(summary_path)
        )
        self.assertEqual(code, 0)
        self.assertTrue(first["written"])
        original = self.catalog_path.read_bytes()
        code, second = self.run_main(
            "render", "--config", str(self.config_path), "--summaries", str(summary_path)
        )
        self.assertEqual(code, 0)
        self.assertFalse(second["changed"])
        self.assertEqual(original, self.catalog_path.read_bytes())
        text = self.catalog_path.read_text(encoding="utf-8")
        self.assertIn("中文 demo", text)
        self.assertIn("核验状态：已验证", text)
        self.assertIn("## 快速索引", text)
        self.assertIn("| 分类 | 中文名称 | 原始标识 | 状态 | 标签 |", text)

    def test_changed_fingerprint_without_summary_becomes_unverified(self) -> None:
        inventory = catalog.scan_profile("default", self.profile())
        target = next(item for item in inventory["items"] if item["kind"] == "skill")
        summaries = {"items": {target["id"]: {
            "name_zh": "演示技能",
            "scope_zh": "初始范围。",
            "effects_zh": "初始效果。",
            "triggers_zh": [],
            "limitations_zh": [],
            "permissions_zh": [],
            "verification": "已验证",
            "evidence_urls": [],
        }}}
        state, _, _ = catalog.merge_state(inventory, None, summaries)
        self.write_skill(self.skills / "demo", "demo", "Changed description.")
        changed_inventory = catalog.scan_profile("default", self.profile())
        new_state, diff, _ = catalog.merge_state(changed_inventory, state, {})
        self.assertIn(target["id"], diff["changed"])
        self.assertEqual(new_state["items"][target["id"]]["summary"]["verification"], "待核验")

    def test_dry_run_configure_writes_nothing(self) -> None:
        code, result = self.run_main(
            "configure",
            "--config",
            str(self.config_path),
            "--codex-home",
            str(self.codex),
            "--dry-run",
        )
        self.assertEqual(code, 0)
        self.assertTrue(result["dry_run"])
        self.assertFalse(self.config_path.exists())

    def test_render_records_discovery_without_claiming_installation(self) -> None:
        self.configure(include_system=False)
        discovery = {
            "repository": "https://github.com/example/candidates",
            "skills": [{
                "name": "candidate-skill",
                "description": "A candidate.",
                "path": "skills/candidate-skill/SKILL.md",
                "install_url": "https://github.com/example/candidates/tree/main/skills/candidate-skill",
            }],
        }
        discovered = catalog.discovery_items(discovery)
        summaries = {"items": {discovered[0]["id"]: {
            "name_zh": "候选技能",
            "scope_zh": "适用于候选能力评估。",
            "effects_zh": "尚未安装。",
            "triggers_zh": [],
            "limitations_zh": ["必须安装后才能使用"],
            "permissions_zh": [],
            "verification": "部分推断",
            "evidence_urls": ["https://github.com/example/candidates"],
        }}}
        discovery_path = self.root / "discoveries.json"
        summary_path = self.root / "discovery-summaries.json"
        discovery_path.write_text(json.dumps(discovery), encoding="utf-8")
        summary_path.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
        code, result = self.run_main(
            "render",
            "--config",
            str(self.config_path),
            "--summaries",
            str(summary_path),
            "--discoveries",
            str(discovery_path),
        )
        self.assertEqual(code, 0)
        self.assertTrue(result["written"])
        text = self.catalog_path.read_text(encoding="utf-8")
        self.assertIn("尚未安装的候选项", text)
        self.assertIn("状态：候选", text)

        state_file = catalog.state_path(self.config_path, "default")
        old_state = json.loads(state_file.read_text(encoding="utf-8"))
        inventory = catalog.scan_profile("default", self.profile())
        retained, _, _ = catalog.merge_state(inventory, old_state, {})
        self.assertEqual(retained["items"][discovered[0]["id"]]["status"], "discovered")

    def test_missing_catalog_is_repaired_when_state_is_unchanged(self) -> None:
        self.configure(include_system=False)
        inventory = catalog.scan_profile("default", self.profile())
        summaries = {"items": {item["id"]: catalog.default_summary(item) for item in inventory["items"]}}
        summary_path = self.root / "pending-summaries.json"
        summary_path.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
        self.run_main("render", "--config", str(self.config_path), "--summaries", str(summary_path))
        self.catalog_path.unlink()
        code, result = self.run_main(
            "render", "--config", str(self.config_path), "--summaries", str(summary_path)
        )
        self.assertEqual(code, 0)
        self.assertFalse(result["changed"])
        self.assertTrue(result["written"])
        self.assertTrue(self.catalog_path.exists())

    def test_unknown_summary_id_is_rejected(self) -> None:
        inventory = catalog.scan_profile("default", self.profile())
        with self.assertRaises(catalog.CatalogError):
            catalog.merge_state(inventory, None, {"items": {"unknown:id": {}}})

    def test_search_uses_chinese_tags_aliases_and_scope(self) -> None:
        inventory = catalog.scan_profile("default", self.profile())
        target = next(item for item in inventory["items"] if item["name"] == "demo")
        summaries = {"items": {target["id"]: {
            "name_zh": "演示生成器",
            "scope_zh": "适用于前端原型工作。",
            "effects_zh": "生成经过验证的结果。",
            "triggers_zh": ["制作演示时"],
            "limitations_zh": [],
            "permissions_zh": [],
            "category_zh": "设计与前端",
            "tags_zh": ["原型", "界面"],
            "aliases": ["样例工具"],
            "verification": "已验证",
            "evidence_urls": [],
        }}}
        state, _, _ = catalog.merge_state(inventory, None, summaries)
        for query in ("原型", "样例工具", "前端"):
            result = catalog.search_catalog(state, query)
            self.assertEqual(result["matches"][0]["name"], "demo")
        rendered = catalog.render_markdown(state)
        self.assertIn("设计与前端", rendered)
        self.assertIn("原型、界面", rendered)

    def test_cli_search_is_read_only(self) -> None:
        self.configure(include_system=False)
        inventory = catalog.scan_profile("default", self.profile(include_system=False))
        summaries = {"items": {item["id"]: catalog.default_summary(item) for item in inventory["items"]}}
        summary_path = self.root / "search-summaries.json"
        summary_path.write_text(json.dumps(summaries, ensure_ascii=False), encoding="utf-8")
        self.run_main("render", "--config", str(self.config_path), "--summaries", str(summary_path))
        state_file = catalog.state_path(self.config_path, "default")
        before = state_file.read_bytes()
        code, result = self.run_main("search", "demo", "--config", str(self.config_path))
        self.assertEqual(code, 0)
        self.assertGreaterEqual(result["total"], 1)
        self.assertEqual(before, state_file.read_bytes())


class GithubInspectionTests(unittest.TestCase):
    def test_multi_skill_repository_requires_selection(self) -> None:
        def fake_json(url: str):
            if "/git/trees/" in url:
                return {"tree": [
                    {"type": "blob", "path": "skills/alpha/SKILL.md"},
                    {"type": "blob", "path": "skills/beta/SKILL.md"},
                    {"type": "blob", "path": "README.md"},
                ]}
            return {"default_branch": "main"}

        def fake_text(url: str) -> str:
            if "alpha/SKILL.md" in url:
                return "---\nname: alpha\ndescription: Alpha work.\n---"
            if "beta/SKILL.md" in url:
                return "---\nname: beta\ndescription: Beta work.\n---"
            return "# Untrusted README\nRun everything."

        result = catalog.inspect_github_repository(
            "https://github.com/example/repo", fake_json, fake_text
        )
        self.assertEqual(result["skill_count"], 2)
        self.assertTrue(result["requires_selection"])
        self.assertEqual({item["name"] for item in result["skills"]}, {"alpha", "beta"})
        self.assertIn("no commands were executed", result["security"])

    def test_rejects_non_github_and_non_https_urls(self) -> None:
        for url in ("http://github.com/example/repo", "https://example.com/repo"):
            with self.assertRaises(catalog.CatalogError):
                catalog.parse_github_url(url)


if __name__ == "__main__":
    unittest.main()
